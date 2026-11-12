"""Loop del bot: comandi Telegram + scansioni periodiche."""

from __future__ import annotations

import html
import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from .amazon import AmazonClient
from .config import Settings
from .scanner import Scanner, humanize_scan
from .store import Store, now_iso, parse_iso
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

# comandi "pesanti": se arrivano in ritardo (PC spento, rete giù) vanno ignorati
COMANDI_PESANTI = {"/scan", "/pausa", "/riprendi", "/soglia", "/folle", "/dose"}

HELP = (
    "<b>Bot sconti caffeina</b>\n\n"
    "Ti avviso quando su Amazon.it compaiono sconti alti su compresse/capsule di caffeina.\n\n"
    "<b>Comandi</b>\n"
    "/stato — soglie, ultima scansione, efficienza\n"
    "/top — i 5 sconti migliori in archivio\n"
    "/info ASIN — dettaglio e storico di un prodotto\n"
    "/report — riepilogo degli ultimi 7 giorni\n"
    "/soglia 30 — sconto minimo (%)\n"
    "/folle 45 — soglia per l'etichetta SCONTO FOLLE\n"
    "/dose 0,025 — tetto € per dose da 200 mg (più basso = più severo)\n"
    "/dose auto — calcola il tetto dal mercato rilevato\n"
    "/pausa — sospendi le notifiche\n"
    "/riprendi — riattiva le notifiche\n"
    "/ultimi — ultimi alert inviati\n"
    "/scan — forza una scansione adesso\n\n"
    "Sugli alert: “Apri su Amazon” e “Ignora 30 giorni”."
)


def _fmt_local(iso: str | None) -> str:
    dt = parse_iso(iso)
    if dt is None:
        return "mai"
    return dt.astimezone().strftime("%d/%m %H:%M")


class BotRunner:
    def __init__(
        self,
        cfg: Settings,
        store: Store,
        client: AmazonClient,
        telegram: TelegramClient,
        scanner: Scanner,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.telegram = telegram
        self.scanner = scanner
        self.offset: int | None = int(store.get_setting("update_offset", 0) or 0) or None
        self._stop = False
        self._scan_lock = threading.Lock()
        self._heartbeat_path = cfg.db_path.parent / "heartbeat.json"

    # ------------------------------------------------------------------ #
    def run(self, annuncia: bool = True) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, AttributeError):
                pass

        log.info("Bot avviato. Prossima scansione tra pochi secondi.")
        prossima = datetime.now(timezone.utc) + timedelta(seconds=15)
        if annuncia:
            try:
                self.telegram.send_message(
                    "🤖 <b>Bot sconti caffeina attivo</b>\n"
                    f"Controllo {len(self.cfg.queries)} ricerche ogni {self.cfg.intervallo_minuti:.0f} minuti.\n"
                    "Scrivi /aiuto per i comandi.",
                    preview=False,
                )
            except TelegramError as exc:
                log.warning("Impossibile inviare il messaggio di avvio: %s", exc)

        errori = 0
        while not self._stop:
            try:
                updates = self.telegram.get_updates(self.offset, timeout=20)
                errori = 0
            except TelegramError as exc:
                errori += 1
                attesa = min(60, 5 * errori)
                log.warning("getUpdates fallito (%s), riprovo tra %ss", exc, attesa)
                self._heartbeat(prossima, stato=f"errore telegram x{errori}")
                time.sleep(attesa)
                continue
            except Exception:  # noqa: BLE001 - il polling non deve mai morire
                log.exception("Errore imprevisto nel polling: continuo")
                time.sleep(5)
                continue

            for update in updates:
                self.offset = int(update.get("update_id", 0)) + 1
                try:
                    self._handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("Errore nel gestire un aggiornamento")
            if updates and self.offset is not None:
                # salvo l'offset solo dopo aver gestito il blocco: al riavvio
                # non vengono rieseguiti comandi già letti
                self.store.set_setting("update_offset", self.offset)

            if datetime.now(timezone.utc) >= prossima and not self._stop:
                self._run_scan()
                jitter = random.uniform(0, max(0.0, self.cfg.jitter_minuti)) if self.cfg.jitter_minuti else 0
                prossima = datetime.now(timezone.utc) + timedelta(
                    minutes=self.cfg.intervallo_minuti + jitter
                )
                log.info("Prossima scansione alle %s", prossima.astimezone().strftime("%H:%M"))

            self._heartbeat(prossima)

        self._heartbeat(prossima, stato="fermo")
        log.info("Bot fermato.")

    def _heartbeat(self, prossima: datetime | None = None, *, stato: str = "attivo") -> None:
        """Segnale di vita letto dal watchdog per capire se il bot è bloccato."""
        payload = {
            "pid": os.getpid(),
            "ts": now_iso(),
            "stato": stato,
            "prossima": prossima.isoformat(timespec="seconds") if prossima else None,
            "offset": self.offset,
        }
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            temporaneo = self._heartbeat_path.with_suffix(".tmp")
            temporaneo.write_text(json.dumps(payload), encoding="utf-8")
            temporaneo.replace(self._heartbeat_path)
        except OSError:
            log.debug("Heartbeat non scritto")

    def _handle_signal(self, signum, frame) -> None:  # noqa: ANN001, ARG002
        log.info("Segnale %s ricevuto, chiusura...", signum)
        self._stop = True

    # ------------------------------------------------------------------ #
    def _run_scan(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            log.info("Scansione già in corso, salto il giro")
            return
        try:
            result = self.scanner.scan()
            if not result.ok and result.note == "captcha":
                log.warning("Scansione bloccata da captcha")
            elif result.alert:
                log.info("Inviati %s alert", result.alert)
        except Exception:  # noqa: BLE001
            log.exception("Errore imprevisto durante la scansione")
            try:
                self.telegram.send_message("⚠️ Errore imprevisto durante la scansione. Controlla i log.", preview=False)
            except TelegramError:
                pass
        finally:
            self._scan_lock.release()

    # ------------------------------------------------------------------ #
    def _handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            self._handle_callback(callback)
            return

        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            if chat_id and text:
                self._reply(chat_id, "Scrivi /aiuto per vedere i comandi disponibili.")
            return

        if str(chat_id) != str(self.cfg.chat_id):
            log.warning("Comando da chat non autorizzata: %s", chat_id)
            self._reply(chat_id, "⛔ Questa chat non è autorizzata.")
            return

        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()

        # comandi rimasti in coda mentre il bot era spento: non rieseguirli
        inviato = message.get("date")
        if inviato and self.cfg.max_comando_vecchio_s > 0 and command in COMANDI_PESANTI:
            eta = time.time() - float(inviato)
            if eta > self.cfg.max_comando_vecchio_s:
                log.info("Comando %s scaduto (%.0f min): ignorato", command, eta / 60)
                self._reply(
                    chat_id,
                    f"⌛ Comando <code>{command}</code> scaduto ({eta / 60:.0f} minuti fa): ignorato.\n"
                    "Riprovalo ora se serve.",
                )
                return

        handlers = {
            "/start": lambda: self._reply(chat_id, HELP),
            "/aiuto": lambda: self._reply(chat_id, HELP),
            "/help": lambda: self._reply(chat_id, HELP),
            "/stato": lambda: self._reply(chat_id, self._status_text()),
            "/soglia": lambda: self._set_threshold(chat_id, "sconto_minimo", argument, 1, 90, "%"),
            "/folle": lambda: self._set_threshold(chat_id, "sconto_folle", argument, 5, 95, "%"),
            "/dose": lambda: self._set_dose(chat_id, argument),
            "/pausa": lambda: self._toggle_pause(chat_id, True),
            "/riprendi": lambda: self._toggle_pause(chat_id, False),
            "/ultimi": lambda: self._reply(chat_id, self._last_alerts_text()),
            "/top": lambda: self._reply(chat_id, self._top_text()),
            "/info": lambda: self._reply(chat_id, self._info_text(argument)),
            "/report": lambda: self._reply(chat_id, self._report_text()),
            "/scan": lambda: self._force_scan(chat_id),
        }
        handler = handlers.get(command)
        if handler is None:
            self._reply(chat_id, "Comando non riconosciuto. Scrivi /aiuto.")
            return
        try:
            handler()
        except Exception:  # noqa: BLE001
            log.exception("Errore nel comando %s", command)
            self._reply(chat_id, "⚠️ Errore nell'esecuzione del comando.")

    # ------------------------------------------------------------------ #
    def _reply(self, chat_id: int | str, text: str) -> None:
        try:
            self.telegram.send_message(text, chat_id=chat_id, preview=False)
        except TelegramError as exc:
            log.warning("Risposta non inviata: %s", exc)

    def _handle_callback(self, callback: dict) -> None:
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        if not chat_id or not callback_id:
            return
        if str(chat_id) != str(self.cfg.chat_id):
            try:
                self.telegram.answer_callback_query(callback_id, "Chat non autorizzata", show_alert=True)
            except TelegramError:
                pass
            return

        if data.startswith("mute:"):
            asin = data.split(":", 1)[1]
            fino = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
            self.store.set_mute(asin, fino)
            try:
                self.telegram.answer_callback_query(callback_id, "Silenziato per 30 giorni")
                message_id = (callback.get("message") or {}).get("message_id")
                if message_id:
                    self.telegram.edit_message_reply_markup(chat_id, message_id, None)
            except TelegramError as exc:
                log.info("Callback non confermata: %s", exc)
            log.info("Prodotto %s silenziato per 30 giorni", asin)
            return

        try:
            self.telegram.answer_callback_query(callback_id, "Azione non riconosciuta")
        except TelegramError:
            pass

    def _status_text(self) -> str:
        t = self.store.thresholds()
        stats = self.store.stats()
        last = self.store.last_scan()
        ultima = "mai"
        esito = ""
        if last is not None:
            ultima = _fmt_local(last["ts"])
            esito = "ok" if last["ok"] else "errore"
        prossima_min = self.cfg.intervallo_minuti
        fonti = self.store.get_setting("fonti_stats", {}) or {}
        sospese = [
            nome
            for nome, voce in fonti.items()
            if parse_iso((voce or {}).get("salta_fino"))
            and parse_iso((voce or {}).get("salta_fino")) > datetime.now(timezone.utc)
        ]
        durata = stats.get("durata_media_s")
        richieste = stats.get("richieste_medie")
        efficienza = ""
        if durata and richieste:
            efficienza = f"⚙️ media: {durata:.0f}s · {richieste:.0f} richieste/giro\n"
        return (
            "<b>Stato bot</b>\n\n"
            f"{'⏸️ in pausa' if t['pausa'] else '▶️ attivo'} · ogni {prossima_min:.0f} min\n"
            f"🔎 sconto minimo: <b>{float(t['sconto_minimo']):.0f}%</b>\n"
            f"🔥 sconto folle: <b>{float(t['sconto_folle']):.0f}%</b>\n"
            f"💊 max {float(t['dose_max_eur']):.3f} €/dose 200 mg"
            f" ({self.store.count_under_dose(float(t['dose_max_eur']))} prodotti sotto soglia)\n"
            f"🥇 calo storico: {float(t['calo_storico_minimo']):.0f}%\n"
            f"🔁 stesso prodotto max 1 volta ogni {float(t['ripeti_dopo_ore']):.0f}h\n"
            f"📬 alert oggi: {self.store.alerts_today()}/{int(t['max_alert_giorno'])}\n\n"
            f"Ultima scansione: {ultima} {esito}\n"
            f"{efficienza}"
            f"Prodotti in archivio: {stats['prodotti']}\n"
            f"Alert totali: {stats['alert_totali']} (7gg: {stats['alert_7gg']})\n"
            f"Fonti sospese: {', '.join(sospese) if sospese else 'nessuna'}\n"
            f"Ricerche: {', '.join(self.cfg.queries)}"
        )

    def _set_threshold(
        self, chat_id: int | str, key: str, argument: str, minimum: float, maximum: float, unit: str
    ) -> None:
        raw = argument.replace(",", ".").replace("%", "").strip()
        if not raw:
            current = self.store.get_setting(key)
            self._reply(chat_id, f"Valore attuale di <code>{key}</code>: {current} {unit}")
            return
        try:
            value = float(raw)
        except ValueError:
            self._reply(chat_id, "Valore non valido: usa un numero, es. <code>35</code>.")
            return
        if not minimum <= value <= maximum:
            self._reply(chat_id, f"Il valore deve stare tra {minimum} e {maximum} {unit}.")
            return
        self.store.set_setting(key, value)
        self.store.set_setting("soglie_personalizzate", True)
        log.info("Impostazione %s = %s", key, value)
        self._reply(chat_id, f"✅ <code>{key}</code> impostato a <b>{value:g} {unit}</b>.")

    def _set_dose(self, chat_id: int | str, argument: str) -> None:
        attuale = float(self.store.thresholds()["dose_max_eur"])
        stats = self.store.dose_stats()
        arg = argument.replace(",", ".").replace("€", "").replace("/dose", "").strip()

        if not arg:
            self._reply(chat_id, self._dose_text(attuale, stats))
            return

        if arg.lower() in {"auto", "consiglia", "calcola"}:
            if int(stats.get("n", 0)) < 3:
                self._reply(chat_id, "Servono almeno 3 prodotti in archivio: lancia /scan.")
                return
            nuovo = round(max(0.008, min(0.04, float(stats["p25"]) * 0.8)), 3)
            self.store.set_setting("dose_max_eur", nuovo)
            self.store.set_setting("soglie_personalizzate", True)
            self._reply(
                chat_id,
                f"✅ Soglia calcolata dal mercato: <b>{nuovo:.3f} €/dose</b>\n"
                f"(25° percentile {float(stats['p25']):.3f} € × 0,8 · mediana {float(stats['mediana']):.3f} €)\n"
                f"Prodotti che passano: {self.store.count_under_dose(nuovo)}/{int(stats['n'])}",
            )
            return

        try:
            valore = float(arg)
        except ValueError:
            self._reply(chat_id, "Valore non valido: usa un numero (es. <code>0,025</code>) o <code>auto</code>.")
            return
        if not 0.005 <= valore <= 0.2:
            self._reply(chat_id, "Il tetto deve stare tra 0,005 e 0,200 € per dose da 200 mg.")
            return

        self.store.set_setting("dose_max_eur", valore)
        self.store.set_setting("soglie_personalizzate", True)
        messaggio = (
            f"✅ Tetto impostato a <b>{valore:.3f} €/dose</b>\n"
            f"Prodotti che passano: {self.store.count_under_dose(valore)}/{int(stats.get('n', 0))}"
        )
        if stats.get("mediana") and valore > float(stats["mediana"]):
            messaggio += (
                f"\n\n⚠️ Sopra la mediana del mercato rilevato ({float(stats['mediana']):.3f} €): "
                "passerebbero anche prodotti poco convenienti."
            )
        self._reply(chat_id, messaggio)

    def _dose_text(self, attuale: float, stats: dict) -> str:
        righe = [
            f"💊 Tetto attuale: <b>{attuale:.3f} €/dose 200 mg</b>",
            f"Prodotti in archivio sotto questa soglia: "
            f"{self.store.count_under_dose(attuale)}/{int(stats.get('n', 0))}",
        ]
        if int(stats.get("n", 0)) >= 3:
            righe += [
                "",
                "<b>Mercato rilevato</b>",
                f"min {float(stats['min']):.3f} € · 25° perc. {float(stats['p25']):.3f} €",
                f"mediana {float(stats['mediana']):.3f} € · max {float(stats['max']):.3f} €",
                "",
                "<code>/dose 0,025</code> imposta il tetto",
                "<code>/dose auto</code> lo calcola dal mercato (25° percentile × 0,8)",
            ]
        return "\n".join(righe)

    def _toggle_pause(self, chat_id: int | str, paused: bool) -> None:
        self.store.set_setting("pausa", paused)
        self._reply(chat_id, "⏸️ Notifiche sospese." if paused else "▶️ Notifiche riattivate.")

    def _last_alerts_text(self) -> str:
        rows = self.store.recent_alerts(5)
        if not rows:
            return "Nessun alert inviato finora."
        lines = ["<b>Ultimi alert</b>"]
        for row in rows:
            prezzo = f"{row['price']:.2f} €".replace(".", ",") if row["price"] else "—"
            sconto = f"−{row['discount']:.0f}%" if row["discount"] else ""
            lines.append(
                f"• {_fmt_local(row['ts'])} · <a href=\"https://www.amazon.it/dp/{row['asin']}\">{row['asin']}</a>"
                f" · {prezzo} {sconto} ({row['kind']})"
            )
        return "\n".join(lines)

    def _force_scan(self, chat_id: int | str) -> None:
        if self.scanner is None:
            self._reply(chat_id, "Scanner non disponibile.")
            return
        if self._scan_lock.locked():
            self._reply(chat_id, "⏳ C'è già una scansione in corso, ti aggiorno appena finisce.")
            return
        self._reply(chat_id, "🔄 Scansione avviata in background, ti aggiorno appena finisco.")

        def lavoro() -> None:
            with self._scan_lock:
                try:
                    result = self.scanner.scan(force=True, salta_invariati=False)  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001
                    log.exception("Scansione forzata fallita")
                    self._reply(chat_id, f"⚠️ Scansione non riuscita: {html.escape(str(exc))}")
                    return
                self._reply(chat_id, humanize_scan(result))

        threading.Thread(target=lavoro, name="scan-forzata", daemon=True).start()

    # ------------------------------------------------------------------ #
    # viste dati
    # ------------------------------------------------------------------ #
    def _top_text(self) -> str:
        righe = ["<b>Top sconti in archivio</b>"]
        prodotti = self.store.top_products(5)
        if not prodotti:
            return "Archivio ancora vuoto: lancia /scan."
        for indice, row in enumerate(prodotti, 1):
            prezzo = f"{row['price']:.2f} €".replace(".", ",") if row["price"] else "—"
            sconto = f"−{row['discount']:.0f}%" if row["discount"] else ""
            dose = f" · {row['dose_eur']:.3f} €/dose".replace(".", ",") if row["dose_eur"] else ""
            righe.append(
                f"{indice}. <a href=\"https://www.amazon.it/dp/{row['asin']}\">"
                f"{(row['title'] or row['asin'])[:60]}</a>\n"
                f"    {prezzo} {sconto}{dose}"
            )
        return "\n".join(righe)

    def _info_text(self, asin: str) -> str:
        codice = asin.strip().upper()
        if not codice:
            return "Uso: <code>/info B0XXXXXXXX</code>"
        row = self.store.product_by_asin(codice)
        if row is None:
            return f"ASIN {codice} non presente in archivio."
        minimo, massimo, campioni = self.store.price_range(codice)
        righe = [
            f"<b>{html.escape((row['title'] or codice)[:90])}</b>",
            f"<a href=\"https://www.amazon.it/dp/{codice}\">amazon.it/dp/{codice}</a>",
            "",
            f"Prezzo attuale: <b>{row['price']:.2f} €</b>" if row["price"] else "Prezzo attuale: —",
        ]
        if row["list_price"]:
            righe.append(f"Prezzo consigliato: {row['list_price']:.2f} €")
        if row["discount"]:
            righe.append(f"Sconto: −{row['discount']:.0f}%")
        if row["dose_eur"]:
            righe.append(f"Costo per dose 200 mg: {row['dose_eur']:.3f} €")
        if row["pills"] and row["mg_per_pill"]:
            righe.append(f"Confezione: {row['pills']} × {row['mg_per_pill']:.0f} mg")
        if minimo is not None:
            righe.append(f"Storico bot: min {minimo:.2f} € · max {massimo:.2f} € · {campioni} rilevazioni")
        if row["last_alert_at"]:
            righe.append(f"Ultimo alert: {_fmt_local(row['last_alert_at'])} ({row['last_alert_kind']})")
        if row["muted_until"]:
            righe.append(f"Silenziato fino al: {_fmt_local(row['muted_until'])}")
        return "\n".join(righe)

    def _report_text(self) -> str:
        righe = self.store.alerts_since(7)
        if not righe:
            return "Nessun alert negli ultimi 7 giorni."
        sconti = [row["discount"] for row in righe if row["discount"]]
        migliore = max(righe, key=lambda row: row["discount"] or 0)
        media = sum(sconti) / len(sconti) if sconti else 0
        testo = [
            "<b>Ultimi 7 giorni</b>",
            f"Alert inviati: {len(righe)}",
            f"Sconto medio: −{media:.0f}%",
            f"Migliore: −{migliore['discount']:.0f}% su "
            f"<a href=\"https://www.amazon.it/dp/{migliore['asin']}\">{migliore['asin']}</a>",
        ]
        return "\n".join(testo)

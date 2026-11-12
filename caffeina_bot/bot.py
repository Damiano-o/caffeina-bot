"""Loop del bot: comandi Telegram + scansioni periodiche."""

from __future__ import annotations

import logging
import random
import signal
import time
from datetime import datetime, timedelta, timezone

from .amazon import AmazonClient
from .config import Settings
from .scanner import Scanner, humanize_scan
from .store import Store, parse_iso
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

HELP = (
    "<b>Bot sconti caffeina</b>\n\n"
    "Ti avviso quando su Amazon.it compaiono sconti alti su compresse/capsule di caffeina.\n\n"
    "<b>Comandi</b>\n"
    "/stato — soglie attuali, ultima scansione, statistiche\n"
    "/soglia 30 — sconto minimo (%)\n"
    "/folle 45 — soglia per l'etichetta SCONTO FOLLE\n"
    "/dose 0,03 — prezzo massimo per dose da 200 mg\n"
    "/pausa — sospendi le notifiche\n"
    "/riprendi — riattiva le notifiche\n"
    "/ultimi — ultimi alert inviati\n"
    "/scan — forza una scansione adesso"
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
        self.offset: int | None = None
        self._stop = False

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

        while not self._stop:
            try:
                updates = self.telegram.get_updates(self.offset, timeout=20)
            except TelegramError as exc:
                log.warning("getUpdates fallito: %s", exc)
                time.sleep(5)
                updates = []

            for update in updates:
                self.offset = int(update.get("update_id", 0)) + 1
                self._handle_update(update)

            if datetime.now(timezone.utc) >= prossima and not self._stop:
                self._run_scan()
                jitter = random.uniform(0, max(0.0, self.cfg.jitter_minuti)) if self.cfg.jitter_minuti else 0
                prossima = datetime.now(timezone.utc) + timedelta(
                    minutes=self.cfg.intervallo_minuti + jitter
                )
                log.info("Prossima scansione alle %s", prossima.astimezone().strftime("%H:%M"))

        log.info("Bot fermato.")

    def _handle_signal(self, signum, frame) -> None:  # noqa: ANN001, ARG002
        log.info("Segnale %s ricevuto, chiusura...", signum)
        self._stop = True

    # ------------------------------------------------------------------ #
    def _run_scan(self) -> None:
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

    # ------------------------------------------------------------------ #
    def _handle_update(self, update: dict) -> None:
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

        handlers = {
            "/start": lambda: self._reply(chat_id, HELP),
            "/aiuto": lambda: self._reply(chat_id, HELP),
            "/help": lambda: self._reply(chat_id, HELP),
            "/stato": lambda: self._reply(chat_id, self._status_text()),
            "/soglia": lambda: self._set_threshold(chat_id, "sconto_minimo", argument, 1, 90, "%"),
            "/folle": lambda: self._set_threshold(chat_id, "sconto_folle", argument, 5, 95, "%"),
            "/dose": lambda: self._set_threshold(chat_id, "dose_max_eur", argument, 0.001, 1.0, "€/dose"),
            "/pausa": lambda: self._toggle_pause(chat_id, True),
            "/riprendi": lambda: self._toggle_pause(chat_id, False),
            "/ultimi": lambda: self._reply(chat_id, self._last_alerts_text()),
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
        return (
            "<b>Stato bot</b>\n\n"
            f"{'⏸️ in pausa' if t['pausa'] else '▶️ attivo'} · ogni {prossima_min:.0f} min\n"
            f"🔎 sconto minimo: <b>{float(t['sconto_minimo']):.0f}%</b>\n"
            f"🔥 sconto folle: <b>{float(t['sconto_folle']):.0f}%</b>\n"
            f"💊 max {float(t['dose_max_eur']):.3f} €/dose 200 mg\n"
            f"🥇 calo storico: {float(t['calo_storico_minimo']):.0f}%\n"
            f"🔁 stesso prodotto max 1 volta ogni {float(t['ripeti_dopo_ore']):.0f}h\n"
            f"📬 alert oggi: {self.store.alerts_today()}/{int(t['max_alert_giorno'])}\n\n"
            f"Ultima scansione: {ultima} {esito}\n"
            f"Prodotti in archivio: {stats['prodotti']}\n"
            f"Alert totali: {stats['alert_totali']} (7gg: {stats['alert_7gg']})\n"
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
        log.info("Impostazione %s = %s", key, value)
        self._reply(chat_id, f"✅ <code>{key}</code> impostato a <b>{value:g} {unit}</b>.")

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
        self._reply(chat_id, "🔄 Scansione avviata, ti aggiorno appena finisco.")
        result = self.scanner.scan(force=True)
        self._reply(chat_id, humanize_scan(result))

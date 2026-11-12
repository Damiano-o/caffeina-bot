"""Scansione Amazon -> valutazione sconti -> invio alert su Telegram.

Ottimizzazioni principali:
- fonti adattive: i filtri Offerte che non rendono vengono sospesi per 6 ore;
- salto dei prodotti il cui prezzo di vetrina non è cambiato (meno richieste);
- cache dettagli con TTL adattivo: prodotti stabili controllati più di rado;
- limite di tempo per scansione (utile sui runner cloud);
- dettagli in parallelo (2 worker) con limite globale di una richiesta ogni ~2,5 s;
- priorità ai prodotti con sconto già visibile in vetrina;
- deduplica per confezione (titolo+dosaggio), non solo per ASIN;
- silenziamento per ASIN e digest quando gli alert sono tanti;
- log di durata e numero richieste per misurare l'efficienza.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import analyze
from .amazon import AmazonClient, BlockedError, Detail, SearchHit
from .config import Settings
from .store import Store, parse_iso
from .telegram import (
    TelegramClient,
    TelegramError,
    alert_keyboard,
    format_alert,
    format_digest,
)

log = logging.getLogger(__name__)

ORE_PAUSA_FONTE = 6
GIRI_VUOTI_PRIMA_DI_SOSPENDERE = 3


@dataclass(slots=True)
class Candidate:
    hit: SearchHit
    detail: Detail | None = None
    info: analyze.TitleInfo | None = None
    verdict: analyze.Verdict | None = None


@dataclass(slots=True)
class ScanResult:
    ok: bool = True
    prodotti_visti: int = 0
    valutati: int = 0
    alert: int = 0
    richieste: int = 0
    saltati: int = 0
    durata_s: float = 0.0
    messaggi: list[str] = field(default_factory=list)
    note: str = ""


class Scanner:
    def __init__(
        self,
        cfg: Settings,
        store: Store,
        client: AmazonClient,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.telegram = telegram

    # ------------------------------------------------------------------ #
    def scan(self, *, dry: bool = False, force: bool = False, salta_invariati: bool = True) -> ScanResult:
        inizio = time.monotonic()
        thresholds = self.store.thresholds()
        if thresholds.get("pausa") and not force:
            log.info("Bot in pausa, scansione saltata")
            return ScanResult(ok=True, note="in pausa")

        log.info(
            "Soglie: sconto >= %.0f%%, folle >= %.0f%%, dose <= %.3f EUR/dose, calo storico >= %.0f%%",
            float(thresholds["sconto_minimo"]),
            float(thresholds["sconto_folle"]),
            float(thresholds["dose_max_eur"]),
            float(thresholds["calo_storico_minimo"]),
        )
        if hasattr(self.client, "richieste"):
            self.client.richieste = 0

        result = ScanResult()
        try:
            candidates = self._raccogli_candidati()
        except BlockedError as exc:
            log.warning("Bloccato da Amazon: %s", exc)
            self._notify_block()
            self._chiudi(result, inizio, ok=False, note="captcha")
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("Errore nella raccolta candidati: %s", exc)
            self._chiudi(result, inizio, ok=False, note=f"errore: {exc}")
            return result

        result.prodotti_visti = len(candidates)
        log.info("Candidati pillole di caffeina: %s", len(candidates))
        if not candidates:
            self._chiudi(result, inizio, note="nessun candidato")
            return result

        soglia = float(thresholds["sconto_minimo"])
        ordinati = self._seleziona(candidates)
        if self.cfg.fast_skip and salta_invariati:
            ordinati, result.saltati = self._filtra_invariati(ordinati, soglia)
        to_fetch = self._applica_budget(ordinati)
        if not to_fetch:
            self._chiudi(result, inizio, note="tutti i prezzi invariati")
            return result

        self._stabili = self.store.stability_map()
        deadline = None
        if self.cfg.max_secondi_scansione > 0:
            deadline = inizio + self.cfg.max_secondi_scansione
        try:
            self._valuta_dettagli(to_fetch, thresholds, result, deadline=deadline)
        except BlockedError as exc:
            log.warning("Bloccato durante i dettagli: %s", exc)
            self._notify_block()
            self._chiudi(result, inizio, ok=False, note="captcha")
            return result

        da_notificare = self._seleziona_alert(to_fetch, thresholds)
        self._invia(da_notificare, result, dry=dry)
        self._chiudi(result, inizio)
        return result

    # ------------------------------------------------------------------ #
    # 1. raccolta candidati
    # ------------------------------------------------------------------ #
    def _fonti(self) -> list[tuple[str, str | None, int]]:
        fonti: list[tuple[str, str | None, int]] = []
        for query in self.cfg.queries:
            for page in range(1, self.cfg.pages + 1):
                fonti.append((query, None, page))
            for filtro in self.cfg.filtri_offerte:
                if self._fonte_in_pausa(filtro):
                    log.info("Fonte '%s' sospesa: resa nulla nelle ultime scansioni", filtro)
                    continue
                fonti.append((query, filtro, 1))
        return fonti

    def _stat_fonti(self) -> dict:
        return self.store.get_setting("fonti_stats", {}) or {}

    def _fonte_in_pausa(self, filtro: str) -> bool:
        voce = self._stat_fonti().get(filtro) or {}
        fino = parse_iso(voce.get("salta_fino"))
        return fino is not None and fino > datetime.now(timezone.utc)

    def _registra_fonti(self, rese: dict[str, int]) -> None:
        stats = self._stat_fonti()
        for etichetta, rilevanti in rese.items():
            if "|" not in etichetta:
                continue
            filtro = etichetta.rsplit("|", 1)[1]
            if filtro.startswith("p"):
                continue
            voce = stats.get(filtro) or {"vuoti": 0, "salta_fino": None}
            if rilevanti == 0:
                voce["vuoti"] = int(voce.get("vuoti", 0)) + 1
                if voce["vuoti"] >= GIRI_VUOTI_PRIMA_DI_SOSPENDERE:
                    voce["salta_fino"] = (
                        datetime.now(timezone.utc) + timedelta(hours=ORE_PAUSA_FONTE)
                    ).isoformat(timespec="seconds")
                    voce["vuoti"] = 0
                    log.info("Sospendo la fonte '%s' per %s ore", filtro, ORE_PAUSA_FONTE)
            else:
                voce["vuoti"] = 0
                voce["salta_fino"] = None
            stats[filtro] = voce
        self.store.set_setting("fonti_stats", stats)

    def _raccogli_candidati(self) -> dict[str, Candidate]:
        candidates: dict[str, Candidate] = {}
        rese: dict[str, int] = {}

        for query, filtro, page in self._fonti():
            etichetta = f"{query}|{filtro or f'p{page}'}"
            try:
                hits = self.client.search(query, page, deal_filter=filtro)
            except BlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Ricerca '%s' fallita: %s", query, exc)
                continue

            rilevanti = 0
            for hit in hits:
                info = analyze.parse_title(hit.title)
                if not info.is_caffeine or info.is_noise:
                    continue
                rilevanti += 1
                existing = candidates.get(hit.asin)
                if existing is None:
                    candidates[hit.asin] = Candidate(hit=hit, info=info)
                    continue
                if hit.card_price and (
                    existing.hit.card_price is None or hit.card_price < existing.hit.card_price
                ):
                    existing.hit.card_price = hit.card_price
                if (hit.card_discount or 0) > (existing.hit.card_discount or 0):
                    existing.hit.card_discount = hit.card_discount
                    existing.hit.card_list_price = hit.card_list_price
                if hit.badge and not existing.hit.badge:
                    existing.hit.badge = hit.badge
            rese[etichetta] = rilevanti

        self._registra_fonti(rese)
        return candidates

    # ------------------------------------------------------------------ #
    # 2. selezione e valutazione
    # ------------------------------------------------------------------ #
    def _seleziona(self, candidates: dict[str, Candidate]) -> list[Candidate]:
        soglia = float(self.store.thresholds()["sconto_minimo"])
        prodotti = self.store.all_products()
        self._prodotti = prodotti

        def priority(item: Candidate) -> tuple:
            row = prodotti.get(item.hit.asin)
            is_new = 0 if row is None else 1
            last_seen = row["last_seen"] if row is not None else ""
            badge = (item.hit.badge or "").lower()
            offerta = 0 if any(
                chiave in badge
                for chiave in ("%", "offerta", "sconto", "deal", "black friday", "prime", "risparmi")
            ) else 1
            vetrina = 0 if (item.hit.card_discount or 0) >= soglia else 1
            # prezzo di vetrina molto sotto il listino già noto
            sotto_listino = 1
            if row is not None and row["list_price"] and item.hit.card_price:
                if item.hit.card_price <= float(row["list_price"]) * 0.75:
                    sotto_listino = 0
            # prodotto già visto scontato: vale la pena ricontrollarlo presto
            storico = 1
            if row is not None and row["discount"] and float(row["discount"]) >= soglia * 0.7:
                storico = 0
            return (
                offerta,
                vetrina,
                sotto_listino,
                storico,
                is_new,
                last_seen,
                item.hit.card_price or 999.0,
            )

        ordered = sorted(candidates.values(), key=priority)
        return ordered

    def _applica_budget(self, ordered: list[Candidate]) -> list[Candidate]:
        budget = max(1, int(self.cfg.max_dettagli_per_scan))
        to_fetch = ordered[:budget]
        if len(ordered) > len(to_fetch):
            log.info(
                "Budget dettagli: %s valutati, %s in coda per i prossimi giri",
                len(to_fetch),
                len(ordered) - len(to_fetch),
            )
        return to_fetch

    def _filtra_invariati(
        self, cands: list[Candidate], soglia: float
    ) -> tuple[list[Candidate], int]:
        """Salta la pagina prodotto quando il prezzo di vetrina è identico a quello noto.

        Se il prezzo non è cambiato non può nascere un nuovo alert (sconto,
        minimo storico e costo per dose restano quelli già valutati): l'unica
        eccezione è un coupon nuovo, quindi i prodotti con coupon noto o con
        badge sconto passano comunque dalla pagina completa.
        """
        tenuti: list[Candidate] = []
        saltati = 0
        adesso = datetime.now(timezone.utc)
        for cand in cands:
            row = getattr(self, "_prodotti", {}).get(cand.hit.asin)
            prezzo_vetrina = cand.hit.card_price
            if row is None or not prezzo_vetrina:
                tenuti.append(cand)
                continue
            # confronto col prezzo di vetrina salvato, non con quello della pagina:
            # sono due superfici diverse e il coupon può cambiare solo il secondo
            riferimento = row["card_price"] or row["price"]
            if not riferimento:
                tenuti.append(cand)
                continue
            if abs(float(prezzo_vetrina) - float(riferimento)) > 0.01:
                tenuti.append(cand)
                continue
            if (cand.hit.card_discount or 0) >= max(5.0, soglia * 0.5):
                tenuti.append(cand)
                continue
            if row["coupon_pct"]:
                tenuti.append(cand)
                continue
            visto = parse_iso(row["last_seen"])
            if visto is None or adesso - visto > timedelta(hours=48):
                tenuti.append(cand)
                continue
            saltati += 1
        if saltati:
            log.info(
                "Prezzo di vetrina invariato: %s pagine prodotto evitate (su %s candidati)",
                saltati,
                len(cands),
            )
        return tenuti, saltati

    def _recupera_dettaglio(self, hit: SearchHit) -> tuple[Detail, bool]:
        detail = self._cached_detail(hit.asin, hit.card_price)
        if detail is not None:
            return detail, True
        return self.client.detail(hit.asin), False

    def _valuta_dettagli(
        self,
        to_fetch: list[Candidate],
        thresholds: dict,
        result: ScanResult,
        *,
        deadline: float | None = None,
    ) -> None:
        parallelismo = max(1, int(self.cfg.parallelismo))
        bloccato = False

        if deadline is not None:
            ritmo = max(0.5, (getattr(self.client, "delay_min", 1.8) + getattr(self.client, "delay_max", 3.6)) / 2)
            spazio = max(0, int((deadline - time.monotonic()) / ritmo))
            if spazio < len(to_fetch):
                log.info(
                    "Limite di tempo: valuto %s dettagli su %s, il resto al prossimo giro",
                    spazio,
                    len(to_fetch),
                )
                to_fetch = to_fetch[:spazio]
        if not to_fetch:
            return

        with ThreadPoolExecutor(max_workers=parallelismo, thread_name_prefix="dettagli") as pool:
            futures = {pool.submit(self._recupera_dettaglio, cand.hit): cand for cand in to_fetch}
            for future in as_completed(futures):
                cand = futures[future]
                try:
                    detail, da_cache = future.result()
                except BlockedError as exc:
                    bloccato = True
                    log.warning("Blocco su %s: %s", cand.hit.asin, exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.info("Dettaglio %s non recuperabile: %s", cand.hit.asin, exc)
                    continue
                if detail is None:
                    continue

                if not da_cache:
                    payload = detail.to_dict()
                    payload["card_price"] = cand.hit.card_price
                    self.store.cache_put(detail.asin, payload)

                self._valuta(cand, detail, thresholds)
                result.valutati += 1

        if bloccato:
            raise BlockedError("captcha durante i dettagli")

    def _valuta(self, cand: Candidate, detail: Detail, thresholds: dict) -> None:
        if not detail.title or len(detail.title) < 3:
            detail.title = cand.hit.title
        cand.detail = detail
        cand.info = analyze.parse_title(detail.title)

        history_min = self.store.history_min(detail.asin)
        if detail.price:
            self.store.add_history(detail.asin, detail.price)

        cand.verdict = analyze.evaluate(
            price=detail.price,
            list_price=detail.list_price,
            discount_pct=detail.discount_pct,
            coupon_pct=detail.coupon_pct,
            mg_per_pill=cand.info.mg_per_pill,
            pills=cand.info.pills,
            available=detail.available,
            history_min=history_min,
            is_caffeine=cand.info.is_caffeine,
            is_noise=cand.info.is_noise,
            sconto_minimo=float(thresholds["sconto_minimo"]),
            sconto_folle=float(thresholds["sconto_folle"]),
            dose_max_eur=float(thresholds["dose_max_eur"]),
            calo_storico_minimo=float(thresholds["calo_storico_minimo"]),
            solo_disponibili=self.cfg.solo_disponibili,
        )

        self.store.upsert_product(
            {
                "asin": detail.asin,
                "title": detail.title,
                "url": detail.url,
                "price": detail.price,
                "list_price": detail.list_price,
                "discount": detail.discount_pct,
                "dose_eur": cand.verdict.dose_eur,
                "mg_per_pill": cand.info.mg_per_pill,
                "pills": cand.info.pills,
                "coupon_pct": detail.coupon_pct,
                "rating": detail.rating,
                "reviews": detail.reviews,
                "available": 1 if detail.available else 0,
                "card_price": cand.hit.card_price,
                "dedupe_key": analyze.dedupe_key(
                    detail.title, cand.info.pills, cand.info.mg_per_pill
                ),
            }
        )

    # ------------------------------------------------------------------ #
    # 3. selezione alert e invio
    # ------------------------------------------------------------------ #
    def _seleziona_alert(self, to_fetch: list[Candidate], thresholds: dict) -> list[Candidate]:
        ore = float(thresholds["ripeti_dopo_ore"])
        adesso = datetime.now(timezone.utc)
        da_notificare: list[Candidate] = []

        for cand in to_fetch:
            verdict, detail = cand.verdict, cand.detail
            if verdict is None or not verdict.alert or detail is None:
                continue
            if self.store.is_muted(detail.asin):
                log.info("%s silenziato dall'utente", detail.asin)
                continue
            if self._in_cooldown(cand):
                log.info("%s in cooldown, salto", detail.asin)
                continue

            chiave = analyze.dedupe_key(detail.title, cand.info.pills, cand.info.mg_per_pill) if cand.info else ""
            if chiave:
                ts, prezzo = self.store.last_alert_for_key(chiave)
                ultimo = parse_iso(ts)
                if ultimo and adesso - ultimo < timedelta(hours=ore):
                    sceso = detail.price and prezzo and detail.price <= prezzo * 0.95
                    if not sceso:
                        log.info("%s già notificato (stessa confezione)", detail.asin)
                        continue
            da_notificare.append(cand)

        da_notificare.sort(key=lambda c: c.verdict.score if c.verdict else 0, reverse=True)
        rimasti = max(0, int(thresholds["max_alert_giorno"]) - self.store.alerts_today())
        return da_notificare[:rimasti]

    def _invia(self, da_notificare: list[Candidate], result: ScanResult, *, dry: bool) -> None:
        if not da_notificare:
            return

        digest_min = max(2, int(self.cfg.digest_min))
        if len(da_notificare) >= digest_min and not dry and self.telegram is not None:
            voci = [(c.detail, c.verdict, c.info) for c in da_notificare]
            testo = format_digest(voci, tag_affiliato=self.cfg.tag_affiliato)
            result.messaggi.append(testo)
            if self._send_text(testo):
                for cand in da_notificare:
                    self._registra(cand)
                result.alert = len(da_notificare)
            return

        for cand in da_notificare:
            detail, verdict = cand.detail, cand.verdict
            if detail is None or verdict is None:
                continue
            testo = format_alert(
                detail,
                verdict,
                cand.info,
                tag_affiliato=self.cfg.tag_affiliato,
                best_ever=self._is_best_ever(detail),
            )
            result.messaggi.append(testo)
            if dry:
                result.alert += 1
                continue
            if self._send(testo, detail):
                self._registra(cand)
                result.alert += 1

    def _registra(self, cand: Candidate) -> None:
        detail, verdict = cand.detail, cand.verdict
        if detail is None or verdict is None:
            return
        self.store.record_alert(
            asin=detail.asin,
            price=detail.price,
            list_price=detail.list_price,
            discount=detail.discount_pct,
            dose_eur=verdict.dose_eur,
            kind=verdict.kind,
            dedupe_key=analyze.dedupe_key(
                detail.title,
                cand.info.pills if cand.info else None,
                cand.info.mg_per_pill if cand.info else None,
            ),
        )

    # ------------------------------------------------------------------ #
    # 4. servizi
    # ------------------------------------------------------------------ #
    def _chiudi(
        self, result: ScanResult, inizio: float, *, ok: bool = True, note: str | None = None
    ) -> None:
        result.durata_s = round(time.monotonic() - inizio, 1)
        result.richieste = int(getattr(self.client, "richieste", 0) or 0)
        if note:
            result.note = note
        if not ok:
            result.ok = False
        self.store.log_scan(
            ok=result.ok,
            found=result.valutati,
            alerted=result.alert,
            note=result.note or f"{result.prodotti_visti} candidati",
            durata_s=result.durata_s,
            richieste=result.richieste,
        )
        log.info(
            "Scansione %s: %s valutati, %s alert, %.1fs, %s richieste%s%s",
            "ok" if result.ok else "fallita",
            result.valutati,
            result.alert,
            result.durata_s,
            result.richieste,
            f", {result.saltati} saltati" if result.saltati else "",
            " (dry run)" if note == "dry" else "",
        )

    def _cached_detail(self, asin: str, card_price: float | None) -> Detail | None:
        stabili = getattr(self, "_stabili", {})
        ttl = self.cfg.cache_stabile_ore if stabili.get(asin) else self.cfg.detail_cache_ore
        if ttl <= 0:
            return None
        payload = self.store.cache_get(asin, ttl)
        if not payload:
            return None
        cached_card = payload.get("card_price")
        if card_price and cached_card and abs(card_price - cached_card) > 0.01:
            return None  # prezzo in vetrina cambiato: ricontrollo la pagina
        values = {slot: payload.get(slot) for slot in Detail.__slots__}  # type: ignore[attr-defined]
        if not values.get("price"):
            return None
        return Detail(**values)

    def _in_cooldown(self, candidate: Candidate) -> bool:
        detail = candidate.detail
        if detail is None:
            return False
        row = self.store.get_product(detail.asin)
        if row is None or not row["last_alert_at"]:
            return False
        last = parse_iso(row["last_alert_at"])
        if last is None:
            return False
        ore = float(self.store.thresholds()["ripeti_dopo_ore"])
        if datetime.now(timezone.utc) - last < timedelta(hours=ore):
            last_price = row["last_alert_price"]
            if detail.price and last_price and detail.price <= last_price * 0.95:
                return False
            return True
        return False

    def _is_best_ever(self, detail: Detail) -> bool:
        if not detail.price:
            return False
        history_min, campioni = self.store.history_stats(detail.asin)
        if history_min is None or campioni < 3:
            return False
        return detail.price <= history_min + 0.001

    def _send(self, text: str, detail: Detail) -> bool:
        if not self.telegram:
            log.warning("Telegram non configurato: messaggio non inviato")
            return False
        keyboard = alert_keyboard(detail, tag_affiliato=self.cfg.tag_affiliato)
        if self.cfg.invia_immagine and detail.image and len(text) <= 1000:
            try:
                self.telegram.send_photo(detail.image, text, keyboard=keyboard)
                return True
            except TelegramError as exc:
                log.info("Invio immagine fallito (%s), ripiego sul testo", exc)
        try:
            self.telegram.send_message(text, keyboard=keyboard)
            return True
        except TelegramError as exc:
            log.error("Invio Telegram fallito: %s", exc)
            return False

    def _send_text(self, text: str) -> bool:
        if not self.telegram:
            log.warning("Telegram non configurato: digest non inviato")
            return False
        try:
            self.telegram.send_message(text, preview=False)
            return True
        except TelegramError as exc:
            log.error("Invio digest fallito: %s", exc)
            return False

    def _notify_block(self) -> None:
        if not self.telegram:
            return
        last = parse_iso(self.store.get_setting("ultimo_avviso_blocco"))
        now = datetime.now(timezone.utc)
        if last and now - last < timedelta(hours=6):
            return
        try:
            self.telegram.send_message(
                "⚠️ <b>Amazon ha bloccato la scansione</b> (captcha).\n"
                "Riprovo al prossimo giro. Se persiste, aumenta i ritardi in "
                "<code>config.toml</code> o avvia il bot da un'altra rete.",
                preview=False,
            )
            self.store.set_setting("ultimo_avviso_blocco", now.isoformat(timespec="seconds"))
        except TelegramError as exc:
            log.warning("Impossibile avvisare del blocco: %s", exc)


def humanize_scan(result: ScanResult) -> str:
    if not result.ok:
        return f"❌ Scansione non riuscita ({result.note or 'errore'})"
    riepilogo = f"{result.valutati} prodotti in {result.durata_s:.0f}s ({result.richieste} richieste)"
    if result.saltati:
        riepilogo += f", {result.saltati} invariati saltati"
    if result.alert:
        return f"✅ {result.alert} alert · {riepilogo}"
    return f"✅ Nessuno sconto sopra soglia · {riepilogo}"

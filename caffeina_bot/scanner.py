"""Scansione Amazon -> valutazione sconti -> invio alert su Telegram."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import analyze
from .amazon import AmazonClient, BlockedError, Detail, SearchHit
from .config import Settings
from .store import Store, parse_iso
from .telegram import TelegramClient, TelegramError, alert_keyboard, format_alert

log = logging.getLogger(__name__)


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
    def scan(self, *, dry: bool = False, force: bool = False) -> ScanResult:
        thresholds = self.store.thresholds()
        if thresholds.get("pausa") and not force:
            log.info("Bot in pausa, scansione saltata")
            return ScanResult(ok=True, note="in pausa")

        result = ScanResult()
        candidates: dict[str, Candidate] = {}

        # 1. ricerca -----------------------------------------------------
        fonti: list[tuple[str, str | None, int]] = []
        for query in self.cfg.queries:
            for page in range(1, self.cfg.pages + 1):
                fonti.append((query, None, page))
            # sezioni "Offerte" di Amazon: più probabilità di sconti veri
            for filtro in self.cfg.filtri_offerte:
                fonti.append((query, filtro, 1))

        for query, filtro, page in fonti:
            try:
                hits = self.client.search(query, page, deal_filter=filtro)
            except BlockedError as exc:
                log.warning("Bloccato da Amazon: %s", exc)
                result.ok = False
                result.note = "captcha"
                self._notify_block()
                self.store.log_scan(ok=False, note="captcha")
                return result
            except Exception as exc:  # noqa: BLE001
                log.warning("Ricerca '%s' fallita: %s", query, exc)
                result.note = f"ricerca fallita: {exc}"
                continue

            for hit in hits:
                info = analyze.parse_title(hit.title)
                if not info.is_caffeine or info.is_noise:
                    continue
                existing = candidates.get(hit.asin)
                if existing is None:
                    candidates[hit.asin] = Candidate(hit=hit, info=info)
                else:
                    if hit.card_price and (
                        existing.hit.card_price is None or hit.card_price < existing.hit.card_price
                    ):
                        existing.hit.card_price = hit.card_price
                    if hit.badge and not existing.hit.badge:
                        existing.hit.badge = hit.badge

        result.prodotti_visti = len(candidates)
        log.info("Candidati pillole di caffeina: %s", len(candidates))
        if not candidates:
            self.store.log_scan(ok=True, found=0, alerted=0, note="nessun candidato")
            return result

        # 2. ordine di valutazione: offerte in vetrina, sconti già noti, nuovi, poi i più vecchi
        def priority(item: Candidate) -> tuple:
            row = self.store.get_product(item.hit.asin)
            is_new = 0 if row is None else 1
            last_seen = row["last_seen"] if row is not None else ""
            badge = (item.hit.badge or "").lower()
            deal = 0 if any(
                key in badge for key in ("%", "offerta", "sconto", "deal", "black friday", "prime", "risparmi")
            ) else 1
            # prezzo di vetrina già sotto il listino noto del 25%: vale un controllo
            sconto_noto = 1
            if row is not None and row["list_price"] and item.hit.card_price:
                if item.hit.card_price <= float(row["list_price"]) * 0.75:
                    sconto_noto = 0
            return (deal, sconto_noto, is_new, last_seen, item.hit.card_price or 999.0)

        ordered = sorted(candidates.values(), key=priority)
        budget = self.cfg.max_dettagli_per_scan
        to_fetch = ordered[:budget]
        if len(ordered) > len(to_fetch):
            log.info(
                "Budget dettagli: %s valutati, %s in coda per i prossimi giri",
                len(to_fetch),
                len(ordered) - len(to_fetch),
            )

        # 3. dettagli ----------------------------------------------------
        for candidate in to_fetch:
            asin = candidate.hit.asin
            detail = self._cached_detail(asin, candidate.hit.card_price)
            if detail is None:
                try:
                    detail = self.client.detail(asin)
                except BlockedError as exc:
                    log.warning("Bloccato durante i dettagli: %s", exc)
                    result.ok = False
                    result.note = "captcha"
                    self._notify_block()
                    self.store.log_scan(ok=False, note="captcha durante i dettagli")
                    return result
                except Exception as exc:  # noqa: BLE001
                    log.info("Dettaglio %s non recuperabile: %s", asin, exc)
                    continue
                payload = detail.to_dict()
                payload["card_price"] = candidate.hit.card_price
                self.store.cache_put(asin, payload)
            candidate.detail = detail

            if not detail.title or len(detail.title) < 3:
                detail.title = candidate.hit.title
            candidate.info = analyze.parse_title(detail.title)

            # storico prezzi (prima di aggiungere il prezzo attuale)
            history_min = self.store.history_min(asin)
            if detail.price:
                self.store.add_history(asin, detail.price)

            candidate.verdict = analyze.evaluate(
                price=detail.price,
                list_price=detail.list_price,
                discount_pct=detail.discount_pct,
                coupon_pct=detail.coupon_pct,
                mg_per_pill=candidate.info.mg_per_pill,
                pills=candidate.info.pills,
                available=detail.available,
                history_min=history_min,
                is_caffeine=candidate.info.is_caffeine,
                is_noise=candidate.info.is_noise,
                sconto_minimo=float(thresholds["sconto_minimo"]),
                sconto_folle=float(thresholds["sconto_folle"]),
                dose_max_eur=float(thresholds["dose_max_eur"]),
                calo_storico_minimo=float(thresholds["calo_storico_minimo"]),
                solo_disponibili=self.cfg.solo_disponibili,
            )
            result.valutati += 1

            self.store.upsert_product(
                {
                    "asin": asin,
                    "title": detail.title,
                    "url": detail.url,
                    "price": detail.price,
                    "list_price": detail.list_price,
                    "discount": detail.discount_pct,
                    "dose_eur": candidate.verdict.dose_eur,
                    "mg_per_pill": candidate.info.mg_per_pill,
                    "pills": candidate.info.pills,
                    "coupon_pct": detail.coupon_pct,
                    "rating": detail.rating,
                    "reviews": detail.reviews,
                    "available": 1 if detail.available else 0,
                    "card_price": candidate.hit.card_price,
                }
            )

        # 4. selezione alert --------------------------------------------
        da_notificare: list[Candidate] = []
        for candidate in to_fetch:
            verdict = candidate.verdict
            if verdict is None or not verdict.alert:
                continue
            if self._in_cooldown(candidate):
                log.info("%s in cooldown, salto", candidate.hit.asin)
                continue
            da_notificare.append(candidate)

        da_notificare.sort(key=lambda c: c.verdict.score if c.verdict else 0, reverse=True)

        rimasti = max(0, int(thresholds["max_alert_giorno"]) - self.store.alerts_today())
        da_notificare = da_notificare[:rimasti]

        # 5. invio -------------------------------------------------------
        for candidate in da_notificare:
            detail = candidate.detail
            verdict = candidate.verdict
            assert detail is not None and verdict is not None
            info = candidate.info
            best_ever = self._is_best_ever(detail)
            text = format_alert(
                detail,
                verdict,
                info,
                tag_affiliato=self.cfg.tag_affiliato,
                best_ever=best_ever,
            )
            result.messaggi.append(text)

            if dry:
                result.alert += 1
                continue

            inviato = self._send(text, detail)
            if inviato:
                self.store.record_alert(
                    asin=detail.asin,
                    price=detail.price,
                    list_price=detail.list_price,
                    discount=detail.discount_pct,
                    dose_eur=verdict.dose_eur,
                    kind=verdict.kind,
                )
                result.alert += 1

        self.store.log_scan(
            ok=result.ok,
            found=result.valutati,
            alerted=result.alert,
            note=result.note or f"{len(candidates)} candidati",
        )
        log.info(
            "Scansione conclusa: %s valutati, %s alert%s",
            result.valutati,
            result.alert,
            " (dry run)" if dry else "",
        )
        return result

    # ------------------------------------------------------------------ #
    def _cached_detail(self, asin: str, card_price: float | None) -> Detail | None:
        if self.cfg.detail_cache_ore <= 0:
            return None
        payload = self.store.cache_get(asin, self.cfg.detail_cache_ore)
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
            # se il prezzo è sceso ancora di almeno il 5%, rinotifico
            if detail.price and last_price and detail.price <= last_price * 0.95:
                return False
            return True
        return False

    def _is_best_ever(self, detail: Detail) -> bool:
        if not detail.price:
            return False
        history_min, samples = self.store.history_stats(detail.asin)
        # servono almeno 3 rilevazioni per parlare di minimo storico
        if history_min is None or samples < 3:
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
                "Il bot riproverà al prossimo giro. Se persiste, aumenta i ritardi "
                "in <code>config.toml</code> o avvia il bot da un'altra rete.",
                preview=False,
            )
            self.store.set_setting("ultimo_avviso_blocco", now.isoformat(timespec="seconds"))
        except TelegramError as exc:
            log.warning("Impossibile avvisare del blocco: %s", exc)


def humanize_scan(result: ScanResult) -> str:
    if not result.ok:
        return f"❌ Scansione non riuscita ({result.note or 'errore'})"
    if result.alert:
        return f"✅ {result.alert} alert da {result.valutati} prodotti valutati"
    return f"✅ Nessuno sconto sopra soglia ({result.valutati} prodotti valutati)"

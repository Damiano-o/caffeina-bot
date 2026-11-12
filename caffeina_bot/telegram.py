"""Client Telegram Bot API e formattazione dei messaggi di alert."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

MAX_LEN = 4096


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, chat_id: str | int | None = None, timeout: int = 25) -> None:
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN mancante")
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    # ------------------------------------------------------------------ #
    def api(self, method: str, params: dict[str, Any] | None = None, *, attempts: int = 3) -> dict:
        payload = dict(params or {})
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.post(
                    f"{self.base}/{method}", json=payload, timeout=self.timeout + 10
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise TelegramError(f"rete: {exc}") from exc
                time.sleep(2 * attempt)
                continue

            try:
                data = response.json()
            except ValueError:
                raise TelegramError(f"risposta non JSON ({response.status_code})")

            if data.get("ok"):
                return data.get("result", {})

            description = str(data.get("description", "errore sconosciuto"))
            code = data.get("error_code", response.status_code)
            if code == 429:
                retry_after = int(data.get("parameters", {}).get("retry_after", 5))
                log.warning("Telegram rate limit, attendo %ss", retry_after)
                time.sleep(retry_after + 1)
                continue
            if code == 400 and "parse" in description.lower():
                raise TelegramError(f"parse: {description}")
            if attempt == attempts:
                raise TelegramError(f"{code}: {description}")
            time.sleep(2 * attempt)
        raise TelegramError("invio fallito")

    # ------------------------------------------------------------------ #
    def get_me(self) -> dict:
        return self.api("getMe")

    def send_message(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        keyboard: list[list[dict]] | None = None,
        preview: bool = True,
    ) -> dict:
        target = str(chat_id or self.chat_id)
        if not target:
            raise TelegramError("chat_id mancante")
        params: dict[str, Any] = {
            "chat_id": target,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            return self.api("sendMessage", params)
        except TelegramError as exc:
            if not str(exc).startswith("parse"):
                raise
            # fallback: testo semplice senza tag
            plain = re.sub(r"<[^>]+>", "", text)
            params["text"] = html.unescape(plain)[:MAX_LEN]
            params.pop("parse_mode", None)
            return self.api("sendMessage", params)

    def send_photo(
        self,
        photo_url: str,
        caption: str,
        *,
        chat_id: str | int | None = None,
        keyboard: list[list[dict]] | None = None,
    ) -> dict:
        target = str(chat_id or self.chat_id)
        params: dict[str, Any] = {
            "chat_id": target,
            "photo": photo_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.api("sendPhoto", params)

    def edit_message_reply_markup(
        self, chat_id: str | int, message_id: int, keyboard: list[list[dict]] | None = None
    ) -> dict:
        params: dict[str, Any] = {"chat_id": str(chat_id), "message_id": message_id}
        params["reply_markup"] = {"inline_keyboard": keyboard or []}
        return self.api("editMessageReplyMarkup", params)

    def answer_callback_query(self, callback_id: str, text: str = "", show_alert: bool = False) -> dict:
        return self.api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text[:200],
                "show_alert": show_alert,
            },
        )

    def get_updates(self, offset: int | None = None, timeout: int = 20) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        result = self.api("getUpdates", params)
        return result if isinstance(result, list) else []


# --------------------------------------------------------------------------- #
# Formattazione
# --------------------------------------------------------------------------- #

KIND_ICON = {"folle": "🔥", "alto": "📉", "calo": "🥇"}


def _eur(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def format_alert(detail: Any, verdict: Any, info: Any, *, tag_affiliato: str = "", best_ever: bool = False) -> str:
    """Costruisce il messaggio HTML dell'alert."""
    icon = KIND_ICON.get(verdict.kind, "🔔")
    discount = verdict.discount_pct
    head = f"{icon} <b>{verdict.label}"
    if discount:
        head += f" −{discount:.0f}%"
    head += "</b>"

    lines = [head, "", f'<a href="{html.escape(detail.url)}">{html.escape(detail.title[:180])}</a>', ""]

    eff = verdict.effective_price or detail.price
    coupon = detail.coupon_pct or 0
    if detail.price and eff and coupon and eff < detail.price - 0.001:
        line = f"💶 <b>{_eur(eff)}</b> con coupon"
        if detail.list_price and detail.list_price > eff:
            line += f"  <s>{_eur(detail.list_price)}</s>"
        lines.append(line)
        lines.append(f"🏷️ in vetrina {_eur(detail.price)} · coupon −{coupon:.0f}%")
    else:
        line = f"💶 <b>{_eur(detail.price)}</b>"
        if detail.list_price and detail.price and detail.list_price > detail.price:
            line += f"  <s>{_eur(detail.list_price)}</s>"
        lines.append(line)

    details = []
    if verdict.dose_eur:
        details.append(f"{verdict.dose_eur:.3f} €/dose 200 mg".replace(".", ","))
    if info and info.pills and info.mg_per_pill:
        details.append(f"{info.pills} × {info.mg_per_pill:.0f} mg")
    if details:
        lines.append("📦 " + " · ".join(details))

    if best_ever:
        lines.append("🥇 prezzo più basso mai registrato da questo bot")

    stars = ""
    if detail.rating:
        stars = f"⭐ {str(detail.rating).replace('.', ',')}"
        if detail.reviews:
            stars += f" · {detail.reviews:,} recensioni".replace(",", ".")
        lines.append(stars)

    if detail.last30_low and detail.price and detail.last30_low > detail.price:
        lines.append(f"📊 minimo ultimi 30 giorni: {_eur(detail.last30_low)}")

    lines.append("")
    link = detail.url + (f"?tag={tag_affiliato}" if tag_affiliato else "")
    lines.append(f'<a href="{html.escape(link)}">👉 Apri su Amazon</a>')

    return "\n".join(lines)


def alert_keyboard(detail: Any, *, tag_affiliato: str = "") -> list[list[dict]]:
    link = detail.url + (f"?tag={tag_affiliato}" if tag_affiliato else "")
    return [
        [{"text": "🛒 Apri su Amazon", "url": link}],
        [{"text": "🔕 Ignora 30 giorni", "callback_data": f"mute:{detail.asin}"}],
    ]


def format_digest(items: list[tuple[Any, Any, Any]], *, tag_affiliato: str = "") -> str:
    """Un solo messaggio quando gli alert sono tanti: meno rumore, stessa informazione."""
    righe = [f"🔥 <b>{len(items)} sconti sopra soglia</b>", ""]
    for indice, (detail, verdict, info) in enumerate(items[:12], 1):
        icona = KIND_ICON.get(verdict.kind, "🔔")
        sconto = verdict.discount_pct or 0
        prezzo = verdict.effective_price or detail.price
        link = detail.url + (f"?tag={tag_affiliato}" if tag_affiliato else "")
        titolo = html.escape((detail.title or "")[:80])
        righe.append(f"<b>{indice}.</b> {icona} <b>−{sconto:.0f}%</b> · <a href=\"{html.escape(link)}\">{titolo}</a>")
        dettagli = [_eur(prezzo)]
        if verdict.dose_eur:
            dettagli.append(f"{verdict.dose_eur:.3f} €/dose".replace(".", ","))
        if info and info.pills and info.mg_per_pill:
            dettagli.append(f"{info.pills} × {info.mg_per_pill:.0f} mg")
        righe.append("   " + " · ".join(dettagli))
        righe.append("")
    return "\n".join(righe)[:MAX_LEN]

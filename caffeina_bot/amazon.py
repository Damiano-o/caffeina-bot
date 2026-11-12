"""Client Amazon.it: ricerca + pagina prodotto, con parsing tollerante e anti-blocco."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}


class AmazonError(Exception):
    """Errore generico di scraping."""


# Filtri "deal type" della ricerca Amazon.it (valori presi dal menu laterale dei risultati).
DEAL_FILTERS = {
    "offerte_e_risparmi": "26901107031",
    "offerte_del_giorno": "210770023031",
    "piu_acquisti_piu_risparmi": "26901109031",
}


def resolve_deal_filter(name: str | None) -> str | None:
    """Traduce un nome leggibile (config) nell'ID del filtro Amazon."""
    if not name:
        return None
    chiave = name.strip().lower().replace(" ", "_")
    return DEAL_FILTERS.get(chiave)


def build_search_url(
    domain: str, query: str, page: int = 1, deal_filter: str | None = None
) -> str:
    """URL di ricerca, con eventuale filtro Offerte (ID o nome)."""
    url = f"https://{domain}/s?k={quote_plus(query)}"
    if page > 1:
        url += f"&page={page}"
    deal_id = resolve_deal_filter(deal_filter) or (deal_filter or "").strip()
    if deal_id:
        url += f"&rh=p_n_deal_type%3A{deal_id}"
    return url


class BlockedError(AmazonError):
    """Amazon ha risposto con captcha / pagina anti-bot."""


class UnavailableError(AmazonError):
    """Pagina non trovata o non recuperabile."""


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def parse_price(text: str | None) -> float | None:
    """'1.234,56 €' -> 1234.56 ; '9,95' -> 9.95 ; '' -> None."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("€", " ").replace("EUR", " ")
    match = _NUM_RE.search(cleaned)
    if not match:
        return None
    raw = match.group(1).replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    else:
        # "1.234" è separatore migliaia, "9.95" è decimale
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _txt(node) -> str | None:
    if node is None:
        return None
    value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return value or None


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        value = _txt(soup.select_one(selector))
        if value:
            return value
    return None


def _first_price(soup: BeautifulSoup, selectors: list[str]) -> float | None:
    for selector in selectors:
        for node in soup.select(selector):
            value = parse_price(_txt(node))
            if value:
                return value
    return None


def _main_price(soup: BeautifulSoup) -> float | None:
    """Prezzo attuale: esclude i prezzi barrati (prezzo consigliato, listino)."""
    value = _first_price(
        soup,
        [
            "#corePriceDisplay_desktop_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
            "#corePrice_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
            "#corePrice_feature_div span.a-offscreen",
            "span.priceToPay span.a-offscreen",
            "#price_inside_buybox",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#tp_price_block_total_price_ww span.a-offscreen",
        ],
    )
    if value:
        return value
    for node in soup.select("span.a-price:not(.a-text-price) span.a-offscreen"):
        if node.find_parent(class_="basisPrice") is not None:
            continue
        value = parse_price(_txt(node))
        if value:
            return value
    return None


def _rating(soup: BeautifulSoup) -> float | None:
    candidates: list[str | None] = []
    popover = soup.select_one("#acrPopover")
    if popover is not None:
        candidates.append(popover.get("title"))
        candidates.append(_txt(popover))
    for selector in ('span[data-hook="rating-out-of-text"]', "span.a-icon-alt", "#averageCustomerReviews"):
        node = soup.select_one(selector)
        if node is not None:
            candidates.append(node.get("title"))
            candidates.append(_txt(node))
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"(\d[.,]\d)", candidate)
        if match:
            return float(match.group(1).replace(",", "."))
    return None


# --------------------------------------------------------------------------- #
# Modelli
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SearchHit:
    asin: str
    title: str
    url: str
    card_price: float | None = None
    sponsored: bool = False
    badge: str | None = None


@dataclass(slots=True)
class Detail:
    asin: str
    title: str
    url: str
    price: float | None = None
    list_price: float | None = None
    discount_pct: float | None = None
    coupon_pct: float | None = None
    per_unit_eur: float | None = None
    last30_low: float | None = None
    rating: float | None = None
    reviews: int | None = None
    available: bool = True
    image: str | None = None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def parse_search(html: str, domain: str = "www.amazon.it") -> list[SearchHit]:
    soup = BeautifulSoup(html, "html.parser")
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = (card.get("data-asin") or "").strip()
        if not asin or asin in seen:
            continue

        title_node = card.select_one("h2 a span") or card.select_one("h2 span") or card.select_one("h2")
        title = _txt(title_node)
        if not title:
            continue

        classes = " ".join(card.get("class") or [])
        sponsored = "AdHolder" in classes or card.select_one(".puis-sponsored-label-text") is not None
        badge = _txt(card.select_one("span.a-badge-label")) or _txt(card.select_one(".a-badge-text"))
        card_price = _first_price(
            card,
            [
                "span.a-price:not(.a-text-price) span.a-offscreen",
                "span.a-price span.a-offscreen",
            ],
        )

        seen.add(asin)
        hits.append(
            SearchHit(
                asin=asin,
                title=title,
                url=f"https://{domain}/dp/{asin}",
                card_price=card_price,
                sponsored=sponsored,
                badge=badge,
            )
        )
    return hits


def parse_detail(html: str, asin: str, domain: str = "www.amazon.it") -> Detail:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(soup, ["#productTitle", "#title", "h1 span#productTitle"]) or asin

    price = _main_price(soup)

    list_price = _first_price(
        soup,
        [
            "#corePriceDisplay_desktop_feature_div .basisPrice span.a-offscreen",
            ".basisPrice span.a-offscreen",
            "#listPrice",
            "#priceblock_listprice",
        ],
    )

    discount_pct: float | None = None
    savings = _txt(soup.select_one(".savingsPercentage"))
    if savings:
        match = re.search(r"(-?\d{1,2})\s*%", savings)
        if match:
            discount_pct = abs(float(match.group(1)))
    if discount_pct is None and list_price and price and list_price > price:
        discount_pct = round((list_price - price) / list_price * 100, 1)

    coupon_pct: float | None = None
    coupon_text = _first_text(
        soup,
        [
            "#promoPriceBlockMessage_feature_div",
            "#couponBadgeRegularVpc",
            ".couponBadge",
            "#vpcButton",
        ],
    )
    if coupon_text:
        match = re.search(r"(\d{1,2})\s*%", coupon_text)
        if match:
            coupon_pct = float(match.group(1))

    core_text = _txt(soup.select_one("#corePriceDisplay_desktop_feature_div")) or ""
    per_unit_eur: float | None = None
    for pattern in (
        r"([\d.,]+)\s*€\s*per\s*unit",
        r"\(\s*([\d.,]+)\s*€?\s*/\s*unit",
    ):
        match = re.search(pattern, core_text, flags=re.IGNORECASE)
        if match:
            per_unit_eur = parse_price(match.group(1))
            if per_unit_eur:
                break

    last30_low = None
    match = re.search(r"ultimi 30\s*(?:gg|giorni)[^0-9]{0,20}([\d.,]+)", core_text, flags=re.IGNORECASE)
    if match:
        last30_low = parse_price(match.group(1))

    rating = _rating(soup)

    reviews = None
    reviews_text = _first_text(soup, ["#acrCustomerReviewText", 'span[data-hook="total-review-count"]'])
    if reviews_text:
        reviews = parse_int(reviews_text)

    availability_text = (_first_text(soup, ["#availability", "#outOfStock"]) or "").lower()
    unavailable_markers = ("non disponibile", "attualmente non disponibile", "esaurito", "non è possibile")
    available = not any(marker in availability_text for marker in unavailable_markers)

    image = None
    img_node = soup.select_one("#landingImage, #imgTagWrapperId img, meta[property='og:image']")
    if img_node is not None:
        image = img_node.get("data-old-hires") or img_node.get("src") or img_node.get("content")

    return Detail(
        asin=asin,
        title=title,
        url=f"https://{domain}/dp/{asin}",
        price=price,
        list_price=list_price,
        discount_pct=discount_pct,
        coupon_pct=coupon_pct,
        per_unit_eur=per_unit_eur,
        last30_low=last30_low,
        rating=rating,
        reviews=reviews,
        available=available,
        image=image,
    )


# --------------------------------------------------------------------------- #
# Client HTTP
# --------------------------------------------------------------------------- #


class AmazonClient:
    def __init__(
        self,
        domain: str = "www.amazon.it",
        delay_min: float = 1.8,
        delay_max: float = 3.6,
        cookies_path: Path | None = None,
        timeout: int = 25,
        browser_fallback: bool = True,
        browser_timeout: int = 45,
    ) -> None:
        self.domain = domain
        self.delay_min = min(delay_min, delay_max)
        self.delay_max = max(delay_min, delay_max)
        self.timeout = timeout
        self.browser_fallback = browser_fallback
        self.browser_timeout = browser_timeout
        self.cookies_path = cookies_path
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.session.headers["User-Agent"] = random.choice(UA_POOL)
        self._load_cookies()
        self._last_request_at = 0.0

    # -- cookie persistence -------------------------------------------------- #
    def _load_cookies(self) -> None:
        if not self.cookies_path or not self.cookies_path.exists():
            return
        try:
            payload = json.loads(self.cookies_path.read_text(encoding="utf-8"))
            self.session.cookies.update(payload)
        except (OSError, ValueError):
            log.debug("Cookie non leggibili, riparto da sessione pulita")

    def _save_cookies(self) -> None:
        if not self.cookies_path:
            return
        try:
            self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
            self.cookies_path.write_text(
                json.dumps(self.session.cookies.get_dict(), ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.debug("Impossibile salvare i cookie")

    # -- HTTP ---------------------------------------------------------------- #
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = random.uniform(self.delay_min, self.delay_max) - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, referer: str | None = None, attempts: int = 3) -> str:
        try:
            return self._get_http(url, referer=referer, attempts=attempts)
        except (BlockedError, AmazonError) as exc:
            if not self.browser_fallback:
                raise
            log.warning("Richiesta HTTP non riuscita (%s), provo col browser headless", exc)
            html = self._get_browser(url)
            if html is None:
                raise
            return html

    def _get_browser(self, url: str) -> str | None:
        from . import browser

        if not browser.available():
            log.info("Playwright non installato: nessun fallback browser")
            return None
        self._throttle()
        try:
            html = browser.fetch_html(url, timeout_ms=self.browser_timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fallback browser fallito: %s", exc)
            return None
        self._last_request_at = time.monotonic()
        if self._is_blocked(html) or len(html) < 5_000:
            log.warning("Anche il browser ha ricevuto una pagina bloccata/vuota")
            return None
        return html

    @staticmethod
    def _is_blocked(html: str) -> bool:
        lowered = html.lower()
        return "/errors/validatecaptcha" in lowered or "inserisci i caratteri" in lowered

    def _get_http(self, url: str, referer: str | None = None, attempts: int = 3) -> str:
        headers = {"Referer": referer} if referer else {}
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            self._throttle()
            if attempt > 1:
                backoff = 2.5 * (attempt - 1) + random.uniform(0, 2)
                log.info("Retry %s/%s tra %.1fs (%s)", attempt, attempts, backoff, url)
                time.sleep(backoff)
            try:
                response = self.session.get(url, headers=headers, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                self._save_cookies()
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Errore rete: %s", exc)
                continue

            if response.status_code in (429, 503) or response.status_code >= 500:
                last_error = AmazonError(f"HTTP {response.status_code}")
                log.warning("HTTP %s su %s", response.status_code, url)
                continue
            if response.status_code == 404:
                raise UnavailableError(f"404 su {url}")
            if response.status_code != 200:
                raise AmazonError(f"HTTP {response.status_code} su {url}")

            html = response.text
            if self._is_blocked(html):
                raise BlockedError("Amazon ha richiesto un captcha")
            if len(html) < 5_000:
                last_error = AmazonError("risposta troppo corta")
                continue
            return html

        raise AmazonError(f"Richiesta fallita: {last_error}")

    # -- API ----------------------------------------------------------------- #
    def search(self, query: str, page: int = 1, deal_filter: str | None = None) -> list[SearchHit]:
        url = build_search_url(self.domain, query, page, deal_filter)
        html = self._get(url, referer=f"https://{self.domain}/")
        hits = parse_search(html, self.domain)
        log.info(
            "Ricerca '%s'%s p.%s -> %s risultati",
            query,
            f" [{deal_filter}]" if deal_filter else "",
            page,
            len(hits),
        )
        return hits

    def detail(self, asin: str) -> Detail:
        url = f"https://{self.domain}/dp/{asin}"
        html = self._get(url, referer=f"https://{self.domain}/s?k=caffeina")
        return parse_detail(html, asin, self.domain)

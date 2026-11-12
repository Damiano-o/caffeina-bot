"""Fallback con browser headless (Playwright) quando Amazon blocca le richieste HTTP.

Il browser viene avviato solo se serve (captcha o pagina vuota) e riusato tra le
richieste successive dello stesso processo.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
SELECTOR_ATTESA = "div[data-component-type='s-search-result'], #productTitle, #dp"

_lock = threading.Lock()
_playwright = None
_browser = None


def available() -> bool:
    """True se Playwright è installato (il browser viene verificato al primo uso)."""
    return importlib.util.find_spec("playwright") is not None


def _ensure_browser():
    global _playwright, _browser
    from playwright.sync_api import sync_playwright

    if _playwright is None:
        _playwright = sync_playwright().start()
    if _browser is None or not _browser.is_connected():
        log.info("Avvio Chromium headless per aggirare il blocco di Amazon")
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        # in container/Docker il processo gira come root: serve --no-sandbox
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
        _browser = _playwright.chromium.launch(headless=True, args=args)
    return _browser


def fetch_html(url: str, timeout_ms: int = 45_000, attesa_ms: int = 8_000) -> str:
    """Scarica l'HTML della pagina con Chromium headless."""
    with _lock:
        browser = _ensure_browser()
        context = browser.new_context(
            locale="it-IT",
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            timezone_id="Europe/Rome",
        )
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector(SELECTOR_ATTESA, timeout=attesa_ms)
            except Exception:  # noqa: BLE001 - la pagina può non avere risultati
                log.debug("Selettore di attesa non trovato su %s", url)
            return page.content()
        finally:
            context.close()


def close() -> None:
    global _playwright, _browser
    with _lock:
        try:
            if _browser is not None:
                _browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if _playwright is not None:
                _playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
        _playwright = None

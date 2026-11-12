"""Caricamento configurazione: config.toml + .env (nessuna dipendenza esterna)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"

QUERIES_DEFAULT = [
    "caffeina compresse",
    "caffeina capsule",
    "caffeina 200 mg",
    "caffeine pills",
]


def load_env(path: Path = ENV_PATH) -> None:
    """Legge KEY=VALUE da .env senza sovrascrivere variabili già presenti."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def save_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Aggiorna .env preservando le altre chiavi."""
    existing: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.strip().startswith("#"):
                k, _, v = raw.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v})
    body = "\n".join(f"{k}={v}" for k, v in existing.items())
    path.write_text(
        "# Creato da 'python run.py setup' - NON committare questo file.\n\n" + body + "\n",
        encoding="utf-8",
    )


@dataclass(slots=True)
class Settings:
    # --- Telegram ---
    telegram_token: str = ""
    chat_id: str = ""

    # --- Amazon ---
    amazon_domain: str = "www.amazon.it"
    queries: list[str] = field(default_factory=lambda: list(QUERIES_DEFAULT))
    pages: int = 1
    tag_affiliato: str = ""
    request_delay_min: float = 1.8
    request_delay_max: float = 3.6
    detail_cache_ore: float = 6.0
    cache_stabile_ore: float = 24.0
    max_dettagli_per_scan: int = 25
    max_secondi_scansione: float = 480.0
    fast_skip: bool = True
    filtri_offerte: list[str] = field(default_factory=lambda: ["offerte_e_risparmi", "offerte_del_giorno"])
    usa_browser_se_captcha: bool = True
    timeout_browser_secondi: int = 45
    invia_immagine: bool = True
    parallelismo: int = 2
    digest_min: int = 4

    # --- Regole "sconto folle" (default: possono essere cambiate anche dal bot) ---
    sconto_minimo: float = 30.0
    sconto_folle: float = 45.0
    dose_max_eur: float = 0.025
    calo_storico_minimo: float = 12.0
    solo_disponibili: bool = True
    max_alert_giorno: int = 8
    ripeti_dopo_ore: float = 48.0

    # --- Scheduling / comandi ---
    intervallo_minuti: float = 30.0
    jitter_minuti: float = 5.0
    max_comando_vecchio_s: int = 900

    # --- Percorsi ---
    db_path: Path = DATA_DIR / "state.db"
    log_path: Path = DATA_DIR / "bot.log"

    @classmethod
    def load(cls) -> "Settings":
        load_env()
        raw: dict = {}
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("rb") as fh:
                raw = tomllib.load(fh)

        telegram = raw.get("telegram", {})
        amazon = raw.get("amazon", {})
        regole = raw.get("regole", {})
        scan = raw.get("scansione", {})

        def pick_env(name: str, default: str = "") -> str:
            return os.environ.get(name, default)

        def num(section: dict, key: str, default: float) -> float:
            value = section.get(key, default)
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        queries = amazon.get("queries") or list(QUERIES_DEFAULT)
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split(",") if q.strip()]

        filtri = amazon.get("filtri_offerte")
        if filtri is None:
            filtri = ["offerte_e_risparmi", "offerte_del_giorno"]
        if isinstance(filtri, str):
            filtri = [f.strip() for f in filtri.split(",") if f.strip()]
        filtri = [str(f).strip() for f in filtri if str(f).strip()]

        return cls(
            telegram_token=pick_env("TELEGRAM_BOT_TOKEN", str(telegram.get("token", ""))).strip(),
            chat_id=pick_env("TELEGRAM_CHAT_ID", str(telegram.get("chat_id", ""))).strip(),
            amazon_domain=str(amazon.get("dominio", "www.amazon.it")).strip(),
            queries=list(queries),
            pages=max(1, int(num(amazon, "pagine", 1))),
            tag_affiliato=str(amazon.get("tag_affiliato", "")).strip(),
            request_delay_min=max(0.8, num(amazon, "ritardo_min_secondi", 1.8)),
            request_delay_max=max(1.2, num(amazon, "ritardo_max_secondi", 3.6)),
            detail_cache_ore=max(0.0, num(amazon, "cache_dettagli_ore", 6.0)),
            cache_stabile_ore=max(0.0, num(amazon, "cache_stabile_ore", 24.0)),
            max_dettagli_per_scan=max(1, int(num(amazon, "max_dettagli_per_scan", 25))),
            max_secondi_scansione=max(0.0, num(amazon, "max_secondi_scansione", 480.0)),
            fast_skip=bool(amazon.get("salta_prezzi_invariati", True)),
            filtri_offerte=list(filtri),
            usa_browser_se_captcha=bool(amazon.get("browser_se_captcha", True)),
            timeout_browser_secondi=max(15, int(num(amazon, "timeout_browser_secondi", 45))),
            invia_immagine=bool(amazon.get("invia_immagine", True)),
            parallelismo=max(1, int(num(amazon, "parallelismo", 2))),
            digest_min=max(2, int(num(amazon, "digest_min", 4))),
            sconto_minimo=num(regole, "sconto_minimo", 30.0),
            sconto_folle=num(regole, "sconto_folle", 45.0),
            dose_max_eur=num(regole, "dose_max_eur", 0.025),
            calo_storico_minimo=num(regole, "calo_storico_minimo", 12.0),
            solo_disponibili=bool(regole.get("solo_disponibili", True)),
            max_alert_giorno=max(1, int(num(regole, "max_alert_giorno", 8))),
            ripeti_dopo_ore=max(0.0, num(regole, "ripeti_dopo_ore", 48.0)),
            intervallo_minuti=max(5.0, num(scan, "intervallo_minuti", 30.0)),
            jitter_minuti=max(0.0, num(scan, "jitter_minuti", 5.0)),
            max_comando_vecchio_s=max(0, int(num(scan, "comando_vecchio_minuti", 15) * 60)),
            db_path=Path(str(scan.get("db_path", DATA_DIR / "state.db"))),
            log_path=Path(str(scan.get("log_path", DATA_DIR / "bot.log"))),
        )

    def regole(self) -> dict[str, float]:
        return {
            "sconto_minimo": self.sconto_minimo,
            "sconto_folle": self.sconto_folle,
            "dose_max_eur": self.dose_max_eur,
            "calo_storico_minimo": self.calo_storico_minimo,
            "ripeti_dopo_ore": self.ripeti_dopo_ore,
            "max_alert_giorno": self.max_alert_giorno,
        }

    def telegram_ok(self) -> bool:
        return bool(self.telegram_token and self.chat_id)

    def as_public_dict(self) -> dict:
        """Dati configurazione senza segreti (per /stato)."""
        data = {}
        for f in fields(self):
            if f.name in {"telegram_token"}:
                continue
            value = getattr(self, f.name)
            data[f.name] = str(value) if isinstance(value, Path) else value
        data["token_presente"] = bool(self.telegram_token)
        return data

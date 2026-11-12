"""Stato persistente in SQLite: prodotti visti, storico prezzi, alert, cache e impostazioni."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    asin TEXT PRIMARY KEY,
    title TEXT,
    url TEXT,
    price REAL,
    list_price REAL,
    discount REAL,
    dose_eur REAL,
    mg_per_pill REAL,
    pills INTEGER,
    coupon_pct REAL,
    rating REAL,
    reviews INTEGER,
    available INTEGER,
    card_price REAL,
    first_seen TEXT,
    last_seen TEXT,
    last_alert_at TEXT,
    last_alert_price REAL,
    last_alert_kind TEXT,
    dedupe_key TEXT,
    muted_until TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_asin ON history(asin, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL,
    list_price REAL,
    discount REAL,
    dose_eur REAL,
    kind TEXT,
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS detail_cache (
    asin TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    durata_s REAL,
    richieste INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Piccole migrazioni per database creati con versioni precedenti."""
        def colonne(tabella: str) -> set[str]:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({tabella})")}

        for tabella, extra in (
            ("products", {"dedupe_key": "TEXT", "muted_until": "TEXT"}),
            ("alerts", {"dedupe_key": "TEXT"}),
            ("scan_log", {"durata_s": "REAL", "richieste": "INTEGER"}),
        ):
            esistenti = colonne(tabella)
            for nome, tipo in extra.items():
                if nome not in esistenti:
                    conn.execute(f"ALTER TABLE {tabella} ADD COLUMN {nome} {tipo}")

        columns = colonne("history")
        if columns and "id" not in columns:
            conn.executescript(
                "ALTER TABLE history RENAME TO history_old;"
                "CREATE TABLE history ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  asin TEXT NOT NULL,"
                "  ts TEXT NOT NULL,"
                "  price REAL NOT NULL"
                ");"
                "INSERT INTO history(asin, ts, price) SELECT asin, ts, price FROM history_old;"
                "DROP TABLE history_old;"
                "CREATE INDEX IF NOT EXISTS idx_history_asin ON history(asin, ts);"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # impostazioni
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def seed_settings(self, defaults: dict[str, Any]) -> None:
        with self._conn() as conn:
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

    def thresholds(self) -> dict[str, Any]:
        keys = {
            "sconto_minimo": 30.0,
            "sconto_folle": 45.0,
            "dose_max_eur": 0.03,
            "calo_storico_minimo": 12.0,
            "ripeti_dopo_ore": 48.0,
            "max_alert_giorno": 8,
            "pausa": False,
        }
        return {key: self.get_setting(key, default) for key, default in keys.items()}

    # ------------------------------------------------------------------ #
    # prodotti e storico
    # ------------------------------------------------------------------ #
    def all_products(self) -> dict[str, sqlite3.Row]:
        """Mappa asin -> riga, per evitare una query per candidato."""
        with self._conn() as conn:
            return {
                row["asin"]: row for row in conn.execute("SELECT * FROM products").fetchall()
            }

    def get_product(self, asin: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()

    def upsert_product(self, data: dict[str, Any]) -> None:
        fields = [
            "asin", "title", "url", "price", "list_price", "discount", "dose_eur",
            "mg_per_pill", "pills", "coupon_pct", "rating", "reviews", "available", "card_price",
            "dedupe_key",
        ]
        values = [data.get(f) for f in fields]
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO products({', '.join(fields)}, first_seen, last_seen) "
                f"VALUES({', '.join('?' for _ in fields)}, ?, ?) "
                "ON CONFLICT(asin) DO UPDATE SET "
                + ", ".join(f"{f} = excluded.{f}" for f in fields if f != "asin")
                + ", last_seen = excluded.last_seen",
                (*values, now_iso(), now_iso()),
            )

    def add_history(self, asin: str, price: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO history(asin, ts, price) VALUES(?, ?, ?)",
                (asin, now_iso(), float(price)),
            )
            # tengo solo gli ultimi 400 campioni per prodotto
            conn.execute(
                "DELETE FROM history WHERE asin = ? AND id NOT IN ("
                "  SELECT id FROM history WHERE asin = ? ORDER BY ts DESC LIMIT 400)",
                (asin, asin),
            )

    def history_min(self, asin: str) -> float | None:
        return self.history_stats(asin)[0]

    def history_stats(self, asin: str) -> tuple[float | None, int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(price) AS m, COUNT(*) AS n FROM history WHERE asin = ?", (asin,)
            ).fetchone()
        if row is None or row["n"] == 0:
            return None, 0
        return row["m"], int(row["n"])

    # ------------------------------------------------------------------ #
    # alert
    # ------------------------------------------------------------------ #
    def record_alert(
        self,
        *,
        asin: str,
        price: float | None,
        list_price: float | None,
        discount: float | None,
        dose_eur: float | None,
        kind: str,
        dedupe_key: str | None = None,
    ) -> None:
        ts = now_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO alerts(asin, ts, price, list_price, discount, dose_eur, kind, dedupe_key) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (asin, ts, price, list_price, discount, dose_eur, kind, dedupe_key),
            )
            conn.execute(
                "UPDATE products SET last_alert_at = ?, last_alert_price = ?, last_alert_kind = ? WHERE asin = ?",
                (ts, price, kind, asin),
            )

    def alerts_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE ts >= ?", (start.isoformat(timespec="seconds"),)
            ).fetchone()
        return int(row["n"]) if row else 0

    def recent_alerts(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            )

    # ------------------------------------------------------------------ #
    # muting e deduplica
    # ------------------------------------------------------------------ #
    def set_mute(self, asin: str, until_iso: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE products SET muted_until = ? WHERE asin = ?", (until_iso, asin))

    def is_muted(self, asin: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT muted_until FROM products WHERE asin = ?", (asin,)).fetchone()
        if row is None or not row["muted_until"]:
            return False
        fino = parse_iso(row["muted_until"])
        return fino is not None and fino > datetime.now(timezone.utc)

    def last_alert_for_key(self, dedupe_key: str) -> tuple[str | None, float | None]:
        """Ultimo alert per lo stesso prodotto (titolo+confezione), anche con ASIN diverso."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ts, price FROM alerts WHERE dedupe_key = ? ORDER BY id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        if row is None:
            return None, None
        return row["ts"], row["price"]

    # ------------------------------------------------------------------ #
    # viste per i comandi
    # ------------------------------------------------------------------ #
    def top_products(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM products WHERE available = 1 AND discount IS NOT NULL "
                    "AND dose_eur IS NOT NULL ORDER BY discount DESC, dose_eur ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            )

    def product_by_asin(self, asin: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()

    def alerts_since(self, giorni: int) -> list[sqlite3.Row]:
        inizio = (datetime.now(timezone.utc) - timedelta(days=giorni)).isoformat(timespec="seconds")
        with self._conn() as conn:
            return list(
                conn.execute("SELECT * FROM alerts WHERE ts >= ? ORDER BY id DESC", (inizio,)).fetchall()
            )

    def price_range(self, asin: str) -> tuple[float | None, float | None, int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(price) AS minimo, MAX(price) AS massimo, COUNT(*) AS n FROM history WHERE asin = ?",
                (asin,),
            ).fetchone()
        if row is None or not row["n"]:
            return None, None, 0
        return row["minimo"], row["massimo"], int(row["n"])

    # ------------------------------------------------------------------ #
    # cache dettagli
    # ------------------------------------------------------------------ #
    def cache_get(self, asin: str, max_age_ore: float) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT fetched_at, payload FROM detail_cache WHERE asin = ?", (asin,)
            ).fetchone()
        if row is None:
            return None
        fetched = parse_iso(row["fetched_at"])
        if fetched is None:
            return None
        if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_ore):
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def cache_put(self, asin: str, payload: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO detail_cache(asin, fetched_at, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(asin) DO UPDATE SET fetched_at = excluded.fetched_at, payload = excluded.payload",
                (asin, now_iso(), json.dumps(payload, ensure_ascii=False)),
            )

    # ------------------------------------------------------------------ #
    # log scansioni
    # ------------------------------------------------------------------ #
    def log_scan(
        self,
        *,
        ok: bool,
        found: int = 0,
        alerted: int = 0,
        note: str = "",
        durata_s: float | None = None,
        richieste: int | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scan_log(ts, ok, found, alerted, note, durata_s, richieste) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), 1 if ok else 0, found, alerted, note[:500], durata_s, richieste),
            )

    def last_scan(self) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            products = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
            alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
            week = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE ts >= ?",
                ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"),),
            ).fetchone()["n"]
            last = conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
            media = conn.execute(
                "SELECT AVG(durata_s) AS d, AVG(richieste) AS r FROM scan_log "
                "WHERE durata_s IS NOT NULL ORDER BY id DESC LIMIT 20"
            ).fetchone()
        return {
            "prodotti": products,
            "alert_totali": alerts,
            "alert_7gg": week,
            "ultima_scansione": dict(last) if last else None,
            "durata_media_s": media["d"] if media else None,
            "richieste_medie": media["r"] if media else None,
        }

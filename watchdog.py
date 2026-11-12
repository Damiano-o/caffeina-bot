#!/usr/bin/env python3
"""Supervisore del bot: lo avvia se è spento, lo riavvia se è bloccato.

Pensato per Windows Task Scheduler (ogni 5 minuti) ma funziona ovunque:

    pythonw watchdog.py            # controlla e, se serve, avvia il bot
    python  watchdog.py --status   # solo diagnostica, nessun avvio

Perché serve: il bot è un processo unico. Se Windows va in sospensione, la
sessione si chiude o Python termina per memoria, il polling Telegram muore e
nessuno se ne accorge. Il watchdog usa due segnali indipendenti:

1. la porta del lock di istanza singola (45999) risponde -> il processo c'è;
2. data/heartbeat.json è recente -> il processo sta lavorando, non è bloccato.

Se la porta è libera avvia il bot; se la porta risponde ma l'heartbeat è
vecchio, termina il processo e lo riavvia.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from caffeina_bot.config import DATA_DIR, Settings  # noqa: E402
from caffeina_bot.single_instance import PORTA  # noqa: E402
from caffeina_bot.store import parse_iso  # noqa: E402

log = logging.getLogger("watchdog")


def setup_logging(verbose: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull  # type: ignore[assignment]
        sys.stderr = sys.stderr or devnull  # type: ignore[assignment]
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handler = logging.handlers.RotatingFileHandler(
        DATA_DIR / "watchdog.log", maxBytes=500_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)


def processo_in_ascolto(porta: int = PORTA) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=1.5):
            return True
    except OSError:
        return False


def heartbeat() -> dict | None:
    percorso = DATA_DIR / "heartbeat.json"
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def eta_heartbeat_minuti(dati: dict | None) -> float | None:
    if not dati:
        return None
    quando = parse_iso(dati.get("ts"))
    if quando is None:
        return None
    return (datetime.now(timezone.utc) - quando).total_seconds() / 60


def tolleranza_minuti(cfg: Settings) -> float:
    # un ciclo di polling dura al massimo ~20s: 15 minuti bastano per capire
    # che il processo è appeso, senza riavvii inutili durante una scansione
    return max(15.0, min(45.0, cfg.intervallo_minuti * 0.75))


def python_per_background() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        senza_finestra = exe.with_name("pythonw.exe")
        if senza_finestra.exists():
            return str(senza_finestra)
    return str(exe)


def avvia_bot(cfg: Settings) -> bool:
    comando = [python_per_background(), str(ROOT / "run.py"), "run"]
    kwargs: dict = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    try:
        processo = subprocess.Popen(comando, **kwargs)  # noqa: S603
    except OSError as exc:
        log.error("Avvio fallito: %s", exc)
        return False
    log.info("Bot avviato (pid %s)", processo.pid)
    return True


def termina(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
        else:
            os.kill(pid, 15)
        log.warning("Processo %s terminato perché bloccato", pid)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Impossibile terminare il pid %s: %s", pid, exc)


def controlla(cfg: Settings, *, avvia: bool = True) -> int:
    dati = heartbeat()
    eta = eta_heartbeat_minuti(dati)
    vivo = processo_in_ascolto()
    limite = tolleranza_minuti(cfg)

    descrizione = f"porta {'occupata' if vivo else 'libera'}"
    descrizione += f", heartbeat {'assente' if eta is None else f'{eta:.1f} min'}"

    if not vivo:
        log.warning("Bot morto (%s): lo riavvio", descrizione)
        return 0 if (avvia and avvia_bot(cfg)) else 1

    if eta is not None and eta > limite:
        pid = int(dati.get("pid") or 0) if dati else 0
        log.warning("Bot bloccato (%s > %.0f min): riavvio", descrizione, limite)
        if pid:
            termina(pid)
            time.sleep(2)
        return 0 if (avvia and avvia_bot(cfg)) else 1

    log.debug("Bot attivo (%s)", descrizione)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog del bot sconti caffeina")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--status", action="store_true", help="diagnostica, non avvia nulla")
    parser.add_argument("--force", action="store_true", help="avvia anche se sembra attivo")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    cfg = Settings.load()

    if args.status:
        dati = heartbeat()
        eta = eta_heartbeat_minuti(dati)
        print(f"porta {PORTA}: {'occupata' if processo_in_ascolto() else 'libera'}")
        print(f"heartbeat: {'assente' if eta is None else f'{eta:.1f} minuti fa'}")
        print(f"tolleranza: {tolleranza_minuti(cfg):.0f} minuti")
        if dati:
            print(f"pid {dati.get('pid')} · stato {dati.get('stato')} · prossima {dati.get('prossima')}")
        return 0

    if args.force:
        return 0 if avvia_bot(cfg) else 1
    return controlla(cfg)


if __name__ == "__main__":
    raise SystemExit(main())

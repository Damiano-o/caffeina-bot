#!/usr/bin/env python3
"""Punto di ingresso del bot.

Uso:
    python run.py setup          configura token Telegram e chat id
    python run.py scan           scansione singola (notifica su Telegram)
    python run.py scan --dry     scansione senza inviare nulla
    python run.py run            bot sempre attivo (comandi + scansioni periodiche)
    python run.py test           invia un messaggio di prova
    python run.py stats          statistiche dall'archivio locale
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time

from caffeina_bot.amazon import AmazonClient
from caffeina_bot.bot import BotRunner
from caffeina_bot.config import DATA_DIR, ENV_PATH, Settings, load_env, save_env
from caffeina_bot.scanner import Scanner, humanize_scan
from caffeina_bot.store import Store
from caffeina_bot.telegram import TelegramClient, TelegramError

log = logging.getLogger("caffeina")


def setup_logging(verbose: bool = False) -> None:
    # pythonw (avvio silenzioso) non ha stdout/stderr: li sostituisco con devnull
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull  # type: ignore[assignment]
        sys.stderr = sys.stderr or devnull  # type: ignore[assignment]
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        DATA_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


REGOLE_VERSIONE = 3
VECCHI_DEFAULT = {"sconto_minimo": 30.0, "sconto_folle": 45.0, "dose_max_eur": 0.03}


def _applica_regole(store: Store, cfg: Settings) -> None:
    """Allinea le soglie ai default del codice, senza toccare quelle scelte dall'utente."""
    versione = int(store.get_setting("regole_versione", 1) or 1)
    if versione >= REGOLE_VERSIONE:
        return
    if store.get_setting("soglie_personalizzate", False):
        store.set_setting("regole_versione", REGOLE_VERSIONE)
        return
    for chiave, _vecchio in VECCHI_DEFAULT.items():
        store.set_setting(chiave, float(getattr(cfg, chiave)))
    store.set_setting("regole_versione", REGOLE_VERSIONE)
    print("Soglie allineate ai nuovi default.")


def build(cfg: Settings, *, telegram: bool = True) -> tuple[Store, AmazonClient, TelegramClient | None, Scanner]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    store.seed_settings(
        {
            "sconto_minimo": cfg.sconto_minimo,
            "sconto_folle": cfg.sconto_folle,
            "dose_max_eur": cfg.dose_max_eur,
            "calo_storico_minimo": cfg.calo_storico_minimo,
            "ripeti_dopo_ore": cfg.ripeti_dopo_ore,
            "max_alert_giorno": cfg.max_alert_giorno,
            "pausa": False,
        }
    )
    _applica_regole(store, cfg)
    client = AmazonClient(
        domain=cfg.amazon_domain,
        delay_min=cfg.request_delay_min,
        delay_max=cfg.request_delay_max,
        cookies_path=DATA_DIR / "cookies.json",
        browser_fallback=cfg.usa_browser_se_captcha,
        browser_timeout=cfg.timeout_browser_secondi,
    )
    tg: TelegramClient | None = None
    if telegram and cfg.telegram_token:
        tg = TelegramClient(cfg.telegram_token, cfg.chat_id or None)
    scanner = Scanner(cfg, store, client, tg)
    return store, client, tg, scanner


# --------------------------------------------------------------------------- #
# Comandi
# --------------------------------------------------------------------------- #


TOKEN_RE = re.compile(r"^\d{6,}:AA[A-Za-z0-9_-]{25,}$")


def read_clipboard() -> str:
    """Legge il testo negli appunti su Windows (utile: copia il token da Telegram)."""
    if os.name != "nt":
        return ""
    try:
        esito = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (esito.stdout or "").strip()


def estrai_token(testo: str) -> str:
    """Estrae un token bot da un testo qualsiasi (es. messaggio di BotFather)."""
    for parola in re.split(r"\s+", testo or ""):
        candidato = parola.strip(".,;:\"'`<>()[]")
        if TOKEN_RE.match(candidato):
            return candidato
    return ""


def cmd_setup(args: argparse.Namespace) -> int:
    load_env()
    cfg = Settings.load()
    token = estrai_token(getattr(args, "token", "") or "")
    if not token and getattr(args, "clipboard", False):
        token = estrai_token(read_clipboard())
        if token:
            print("Token letto dagli appunti.")
        else:
            print("Nessun token valido negli appunti: incollalo qui sotto.")
    if not token:
        token = cfg.telegram_token

    print("=== Configurazione bot sconti caffeina ===")
    if not token:
        if not sys.stdin.isatty():
            print("Token mancante. Usa --token <token> oppure --clipboard.")
            return 1
        print("Serve il token di @BotFather: Telegram > @BotFather > /newbot > copia il token.")
        token = estrai_token(input("Token del bot: ").strip())
    if not token:
        print("Token non valido (formato atteso: 123456789:AA...).")
        return 1

    try:
        tg = TelegramClient(token)
        me = tg.get_me()
    except TelegramError as exc:
        print(f"Token non valido: {exc}")
        return 1
    username = str(me.get("username", ""))
    print(f"Bot trovato: @{username}")

    chat_id = cfg.chat_id
    if chat_id:
        print(f"Chat id già configurato: {chat_id}")
    else:
        print(f"\nApri Telegram, cerca @{username} (link: https://t.me/{username}) e invia /start.")
        print("Attendo il tuo messaggio (max 90 secondi)...")
        offset = None
        trovato = None
        scadenza = time.time() + 90
        while time.time() < scadenza and not trovato:
            try:
                updates = tg.get_updates(offset, timeout=15)
            except TelegramError as exc:
                print(f"Errore in attesa: {exc}")
                time.sleep(3)
                continue
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if chat.get("id"):
                    trovato = str(chat["id"])
                    nome = chat.get("username") or chat.get("first_name") or "utente"
                    print(f"Chat trovata: {trovato} ({nome})")
                    break
        if not trovato:
            print("Nessun messaggio ricevuto. Rilancia `python run.py setup` e invia /start.")
            return 1
        chat_id = trovato

    save_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)})
    print(f"\nSalvato in {ENV_PATH}")

    try:
        TelegramClient(token, chat_id).send_message(
            "✅ <b>Configurazione completata</b>\n"
            "Da ora riceverai qui gli alert sugli sconti alti di caffeina su Amazon.it.\n"
            "Scrivi /aiuto per i comandi.",
            preview=False,
        )
        print("Messaggio di prova inviato su Telegram.")
    except TelegramError as exc:
        print(f"Configurazione salvata, ma l'invio di prova è fallito: {exc}")
        return 1
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    cfg = Settings.load()
    if not cfg.telegram_ok():
        print("Telegram non configurato: lancia prima `python run.py setup`.")
        return 1
    tg = TelegramClient(cfg.telegram_token, cfg.chat_id)
    tg.send_message(
        "🔔 <b>Test</b>\nSe leggi questo messaggio, il bot può inviarti notifiche.",
        preview=False,
    )
    print("Messaggio di test inviato.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = Settings.load()
    if not args.dry and not cfg.telegram_ok():
        print("Telegram non configurato: uso la modalità --dry (nessun invio).")
        args.dry = True

    store, client, tg, scanner = build(cfg, telegram=not args.dry)
    result = scanner.scan(dry=args.dry, force=args.force)
    print(humanize_scan(result))
    if args.dry:
        if not result.messaggi:
            print("\nNessun alert da mostrare con le soglie attuali.")
        for i, messaggio in enumerate(result.messaggi, 1):
            print(f"\n--- alert {i} ---\n{messaggio}")
    return 0 if result.ok else 2


def cmd_run(args: argparse.Namespace) -> int:
    cfg = Settings.load()
    if not cfg.telegram_token:
        print("Token mancante: lancia `python run.py setup` prima di avviare il bot.")
        return 1
    if not cfg.chat_id:
        print("Chat id mancante: invia /start al bot (oppure usa `python auto_avvio.py`).")
        return 1
    from caffeina_bot import single_instance

    with single_instance.lock() as libero:
        if not libero:
            print("Un'altra istanza del bot è già attiva.")
            return 3
        store, client, tg, scanner = build(cfg)
        assert tg is not None
        BotRunner(cfg, store, client, tg, scanner).run()
    return 0


def cmd_cloud(args: argparse.Namespace) -> int:
    """Una passata da cron (GitHub Actions): comandi in coda + scansione se scaduta."""
    from datetime import datetime, timedelta, timezone

    from caffeina_bot.bot import BotRunner
    from caffeina_bot.store import parse_iso

    cfg = Settings.load()
    if not cfg.telegram_token:
        print("Token mancante.")
        return 1

    store, client, tg, scanner = build(cfg)
    if tg is None:
        print("Telegram non inizializzato.")
        return 1

    chat_id = cfg.chat_id or str(store.get_setting("chat_id") or "")
    offset = int(store.get_setting("update_offset", 0) or 0)

    updates = tg.get_updates(offset or None, timeout=0)
    print(f"Comandi in coda: {len(updates)}")

    runner = BotRunner(cfg, store, client, tg, scanner)
    for update in updates:
        update_id = int(update.get("update_id", 0))
        offset = max(offset, update_id + 1)
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat and not chat_id:
            chat_id = str(chat)
            store.set_setting("chat_id", chat_id)
            cfg.chat_id = chat_id
            tg.chat_id = chat_id
            print(f"Chat id registrata: {chat_id}")
            try:
                tg.send_message(
                    "🤖 <b>Bot sconti caffeina attivo</b>\n"
                    "Gira su GitHub Actions: controlla Amazon ogni 15 minuti.\n"
                    "Scrivi /aiuto per i comandi.",
                    chat_id=chat_id,
                    preview=False,
                )
            except TelegramError as exc:
                print(f"Benvenuto non inviato: {exc}")
        if chat_id:
            runner._handle_update(update)

    store.set_setting("update_offset", offset)

    if not chat_id:
        print("Nessuna chat configurata: scansiono comunque, gli alert saranno saltati.")

    ultima = store.last_scan()
    ultima_dt = parse_iso(ultima["ts"]) if ultima is not None else None
    intervallo = timedelta(minutes=max(5.0, cfg.intervallo_minuti))
    adesso = datetime.now(timezone.utc)
    if ultima_dt is not None and adesso - ultima_dt < intervallo:
        print(f"Scansione recente ({ultima['ts']}), salto.")
        return 0

    result = scanner.scan(force=True)
    if not result.ok and result.note == "captcha":
        print("Amazon ha bloccato la prima passata: riprovo tra 60 secondi.")
        time.sleep(60)
        result = scanner.scan(force=True)
    print(humanize_scan(result))
    return 0 if result.ok else 2


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = Settings.load()
    store = Store(cfg.db_path)
    stats = store.stats()
    soglie = store.thresholds()
    print("=== Statistiche ===")
    print(f"Prodotti in archivio : {stats['prodotti']}")
    print(f"Alert totali         : {stats['alert_totali']} (ultimi 7 giorni: {stats['alert_7gg']})")
    print(f"Alert oggi           : {store.alerts_today()}/{int(soglie['max_alert_giorno'])}")
    print(f"In pausa             : {'sì' if soglie['pausa'] else 'no'}")
    print(
        "Soglie               : "
        f"sconto ≥ {float(soglie['sconto_minimo']):.0f}%, "
        f"folle ≥ {float(soglie['sconto_folle']):.0f}%, "
        f"max {float(soglie['dose_max_eur']):.3f} €/dose"
    )
    last = stats["ultima_scansione"]
    if last:
        print(f"Ultima scansione     : {last['ts']} · ok={bool(last['ok'])} · "
              f"valutati={last['found']} · alert={last['alerted']} · {last['note'] or ''}")
    else:
        print("Ultima scansione     : mai")
    print("\nUltimi alert:")
    for row in store.recent_alerts(5):
        prezzo = f"{row['price']:.2f} €" if row["price"] else "—"
        print(f"  {row['ts']} · {row['asin']} · {prezzo} · −{row['discount'] or 0:.0f}% · {row['kind']}")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot Telegram sconti caffeina su Amazon.it")
    parser.add_argument("-v", "--verbose", action="store_true", help="log dettagliato")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="configura token e chat id").add_argument(
        "--token", help="token del bot (altrimenti lo chiede)"
    )
    parser_setup = sub.choices["setup"]
    parser_setup.add_argument(
        "--clipboard", action="store_true", help="legge il token dagli appunti Windows"
    )
    sub.add_parser("test", help="invia un messaggio di prova")
    sub.add_parser("stats", help="mostra statistiche")

    p_scan = sub.add_parser("scan", help="scansione singola")
    p_scan.add_argument("--dry", action="store_true", help="non invia nulla su Telegram")
    p_scan.add_argument("--force", action="store_true", help="ignora la pausa")

    sub.add_parser("run", help="avvia il bot in loop")
    sub.add_parser("cloud", help="una passata da cron (comandi + scansione)")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    handlers = {
        "setup": cmd_setup,
        "test": cmd_test,
        "scan": cmd_scan,
        "run": cmd_run,
        "cloud": cmd_cloud,
        "stats": cmd_stats,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrotto.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

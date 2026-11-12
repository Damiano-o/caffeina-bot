"""Avvio automatico: attende il primo /start, salva la chat id e fa partire il bot."""

from __future__ import annotations

import logging
import time

from caffeina_bot import single_instance
from caffeina_bot.bot import BotRunner
from caffeina_bot.config import Settings, load_env, save_env
from caffeina_bot.telegram import TelegramClient, TelegramError


def attesa_chat(tg: TelegramClient, minuti: int = 1440) -> str:
    """Aspetta che l'utente scriva al bot e restituisce la chat id."""
    log = logging.getLogger("auto_avvio")
    offset: int | None = None
    scadenza = time.time() + minuti * 60
    log.info("In attesa del primo messaggio al bot (max %s minuti)...", minuti)
    while time.time() < scadenza:
        try:
            updates = tg.get_updates(offset, timeout=25)
        except TelegramError as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(5)
            continue
        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
    return ""


def main() -> int:
    from run import build, setup_logging

    setup_logging(False)
    log = logging.getLogger("auto_avvio")

    load_env()
    cfg = Settings.load()
    if not cfg.telegram_token:
        log.error("Token mancante: esegui prima `python run.py setup`.")
        return 1

    with single_instance.lock() as libero:
        if not libero:
            log.warning("Un'altra istanza del bot è già attiva: esco.")
            return 3

        tg = TelegramClient(cfg.telegram_token, cfg.chat_id or None)
        chat_id = cfg.chat_id

        if not chat_id:
            chat_id = attesa_chat(tg)
            if not chat_id:
                log.error("Nessun messaggio ricevuto: bot non avviato.")
                return 2
            save_env({"TELEGRAM_BOT_TOKEN": cfg.telegram_token, "TELEGRAM_CHAT_ID": chat_id})
            cfg.chat_id = chat_id
            log.info("Chat id salvata: %s", chat_id)

        try:
            tg.send_message(
                "🤖 <b>Bot sconti caffeina attivo</b>\n"
                f"Controllo {len(cfg.queries)} ricerche ogni {cfg.intervallo_minuti:.0f} minuti "
                "più le sezioni Offerte di Amazon.\n"
                "Prima scansione tra pochi secondi. Scrivi /aiuto per i comandi.",
                preview=False,
            )
        except TelegramError as exc:
            log.warning("Messaggio di benvenuto non inviato: %s", exc)

        store, client, tg2, scanner = build(cfg)
        if tg2 is None:
            log.error("Telegram non configurato correttamente.")
            return 1
        BotRunner(cfg, store, client, tg2, scanner).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

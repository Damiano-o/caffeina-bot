"""Test dei comandi del bot con Telegram simulato (nessuna rete)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from caffeina_bot.bot import BotRunner
from caffeina_bot.config import Settings
from caffeina_bot.store import Store


class FakeTelegram:
    def __init__(self) -> None:
        self.messaggi: list[tuple[str, str | int | None]] = []

    def send_message(self, text: str, *, chat_id=None, keyboard=None, preview=True):  # noqa: ANN001, ARG002
        self.messaggi.append((str(chat_id), text))
        return {"message_id": len(self.messaggi)}

    def get_updates(self, offset=None, timeout=20):  # noqa: ANN001, ARG002
        return []


def make_runner(tmpdir: str, chat_id: str = "123") -> tuple[BotRunner, Store, FakeTelegram]:
    cfg = Settings(telegram_token="token", chat_id=chat_id, db_path=Path(tmpdir) / "state.db")
    store = Store(cfg.db_path)
    store.seed_settings({"sconto_minimo": 30.0, "sconto_folle": 45.0, "dose_max_eur": 0.03})
    fake = FakeTelegram()
    runner = BotRunner(cfg, store, client=None, telegram=fake, scanner=None)  # type: ignore[arg-type]
    return runner, store, fake


class TestComandi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runner, self.store, self.fake = make_runner(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invia(self, testo: str, chat_id: int = 123) -> None:
        self.runner._handle_update({"message": {"chat": {"id": chat_id}, "text": testo}})

    def test_stato(self) -> None:
        self._invia("/stato")
        self.assertEqual(len(self.fake.messaggi), 1)
        self.assertIn("sconto minimo", self.fake.messaggi[0][1])
        self.assertIn("30%", self.fake.messaggi[0][1])

    def test_soglia_valida(self) -> None:
        self._invia("/soglia 40")
        self.assertEqual(self.store.get_setting("sconto_minimo"), 40.0)
        self.assertIn("40", self.fake.messaggi[-1][1])

    def test_soglia_fuori_range(self) -> None:
        self._invia("/soglia 300")
        self.assertEqual(self.store.get_setting("sconto_minimo"), 30.0)
        self.assertIn("tra", self.fake.messaggi[-1][1])

    def test_dose_con_virgola(self) -> None:
        self._invia("/dose 0,025")
        self.assertEqual(self.store.get_setting("dose_max_eur"), 0.025)

    def test_pausa_e_riprendi(self) -> None:
        self._invia("/pausa")
        self.assertTrue(self.store.get_setting("pausa"))
        self._invia("/riprendi")
        self.assertFalse(self.store.get_setting("pausa"))

    def test_chat_non_autorizzata(self) -> None:
        self._invia("/pausa", chat_id=999)
        self.assertFalse(self.store.get_setting("pausa"))
        self.assertIn("non è autorizzata", self.fake.messaggi[-1][1])

    def test_comando_sconosciuto(self) -> None:
        self._invia("/pippo")
        self.assertIn("non riconosciuto", self.fake.messaggi[-1][1])

    def test_testo_senza_comando(self) -> None:
        self._invia("ciao")
        self.assertIn("/aiuto", self.fake.messaggi[-1][1])


if __name__ == "__main__":
    unittest.main()

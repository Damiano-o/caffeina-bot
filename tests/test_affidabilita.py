"""Test di affidabilità: watchdog, comandi scaduti, fast-skip, storico compatto."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from caffeina_bot.amazon import Detail, SearchHit
from caffeina_bot.bot import BotRunner
from caffeina_bot.config import Settings
from caffeina_bot.scanner import Scanner
from caffeina_bot.store import Store


class FakeTelegram:
    def __init__(self) -> None:
        self.messaggi: list[tuple[str, str]] = []

    def send_message(self, text, *, chat_id=None, keyboard=None, preview=True):  # noqa: ANN001, ARG002
        self.messaggi.append((str(chat_id), text))
        return {"message_id": len(self.messaggi)}


class PrezziFissi:
    """Client finto: prezzo di vetrina e prezzo pagina sempre uguali."""

    def __init__(self) -> None:
        self.richieste = 0
        self.chiamate = 0

    def search(self, query, page=1, deal_filter=None):  # noqa: ANN001, ARG002
        self.chiamate += 1
        return [
            SearchHit(
                asin=f"B{i:09d}",
                title="Caffeina Pura 200 compresse 200 mg",
                url=f"https://www.amazon.it/dp/B{i:09d}",
                card_price=9.95,
            )
            for i in range(2)
        ]

    def detail(self, asin):  # noqa: ANN001
        self.richieste += 1
        return Detail(
            asin=asin,
            title="Caffeina Pura 200 compresse 200 mg",
            url=f"https://www.amazon.it/dp/{asin}",
            price=9.95,
            list_price=None,
            discount_pct=None,
            available=True,
        )


def scanner_per(tmp: str, client) -> tuple[Scanner, Store]:  # noqa: ANN001
    cfg = Settings(
        queries=["caffeina"],
        pages=1,
        filtri_offerte=[],
        db_path=Path(tmp) / "state.db",
        parallelismo=1,
        detail_cache_ore=0.0,
        cache_stabile_ore=0.0,
    )
    store = Store(cfg.db_path)
    return Scanner(cfg, store, client, telegram=None), store


class TestFastSkip(unittest.TestCase):
    def test_secondo_giro_salta_i_prezzi_invariati(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = PrezziFissi()
            scanner, _ = scanner_per(tmp, client)
            primo = scanner.scan(dry=True)
            self.assertEqual(primo.valutati, 2)
            self.assertEqual(primo.saltati, 0)

            client.richieste = 0
            secondo = scanner.scan(dry=True)
            self.assertEqual(secondo.saltati, 2)
            self.assertEqual(secondo.valutati, 0)
            self.assertEqual(client.richieste, 0, "nessuna pagina prodotto richiesta")

    def test_scansione_forzata_non_salta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = PrezziFissi()
            scanner, _ = scanner_per(tmp, client)
            scanner.scan(dry=True)
            forzata = scanner.scan(dry=True, force=True, salta_invariati=False)
            self.assertEqual(forzata.saltati, 0)
            self.assertEqual(forzata.valutati, 2)


class TestComandiScaduti(unittest.TestCase):
    def _runner(self, tmp: str) -> tuple[BotRunner, Store, FakeTelegram]:
        cfg = Settings(
            telegram_token="token",
            chat_id="123",
            db_path=Path(tmp) / "state.db",
            max_comando_vecchio_s=900,
        )
        store = Store(cfg.db_path)
        fake = FakeTelegram()
        return BotRunner(cfg, store, client=None, telegram=fake, scanner=None), store, fake  # type: ignore[arg-type]

    def _invia(self, runner, testo: str, *, secondi_fa: int = 0) -> None:  # noqa: ANN001
        runner._handle_update(
            {
                "message": {
                    "chat": {"id": 123},
                    "text": testo,
                    "date": int(time.time()) - secondi_fa,
                }
            }
        )

    def test_scan_vecchio_ignorato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, fake = self._runner(tmp)
            self._invia(runner, "/scan", secondi_fa=3600)
            self.assertIn("scaduto", fake.messaggi[-1][1])

    def test_scan_recente_accettato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, fake = self._runner(tmp)
            self._invia(runner, "/scan", secondi_fa=5)
            self.assertIn("Scanner non disponibile", fake.messaggi[-1][1])

    def test_stato_risponde_anche_se_vecchio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, fake = self._runner(tmp)
            self._invia(runner, "/stato", secondi_fa=3600)
            self.assertIn("sconto minimo", fake.messaggi[-1][1])

    def test_offset_ripreso_dal_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(telegram_token="t", chat_id="123", db_path=Path(tmp) / "state.db")
            store = Store(cfg.db_path)
            store.set_setting("update_offset", 4321)
            runner = BotRunner(cfg, store, client=None, telegram=FakeTelegram(), scanner=None)  # type: ignore[arg-type]
            self.assertEqual(runner.offset, 4321)


class TestStoricoCompatto(unittest.TestCase):
    def test_prezzo_ripetuto_non_crea_campioni(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            self.assertTrue(store.add_history("B1", 9.95))
            self.assertFalse(store.add_history("B1", 9.95))
            self.assertTrue(store.add_history("B1", 8.50))
            minimo, campioni = store.history_stats("B1")
            self.assertEqual(campioni, 2)
            self.assertAlmostEqual(minimo or 0, 8.50)

    def test_stability_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            for prezzo in (10.0, 10.0, 10.05):
                store.add_history("STABILE", prezzo, ore_minime=0)
            for prezzo in (10.0, 14.0, 9.0):
                store.add_history("MOSSO", prezzo, ore_minime=0)
            mappa = store.stability_map()
            self.assertTrue(mappa["STABILE"])
            self.assertFalse(mappa["MOSSO"])


if __name__ == "__main__":
    unittest.main()

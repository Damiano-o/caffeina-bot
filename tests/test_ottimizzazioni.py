"""Test delle ottimizzazioni: deduplica, fonti adattive, digest, callback, comandi dati."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from caffeina_bot.amazon import Detail, SearchHit
from caffeina_bot.analyze import dedupe_key
from caffeina_bot.bot import BotRunner
from caffeina_bot.config import Settings
from caffeina_bot.scanner import Scanner
from caffeina_bot.store import Store
from caffeina_bot.telegram import format_digest


class TestDedupeKey(unittest.TestCase):
    def test_varianti_stessa_confezione(self) -> None:
        a = dedupe_key("Caffeina Pura – 400 compresse – 200 mg", 400, 200)
        b = dedupe_key("Caffeina Pura 400 compresse 200mg (venditore X)", 400, 200)
        self.assertEqual(a, b)

    def test_forma_diversa(self) -> None:
        compresse = dedupe_key("Caffeina Pura 400 compresse 200 mg", 400, 200)
        capsule = dedupe_key("Caffeina Pura 400 capsule 200 mg", 400, 200)
        self.assertNotEqual(compresse, capsule)

    def test_confezione_diversa(self) -> None:
        a = dedupe_key("Caffeina Pura 400 compresse 200 mg", 400, 200)
        b = dedupe_key("Caffeina Pura 180 compresse 200 mg", 180, 200)
        self.assertNotEqual(a, b)


class FakeTelegram:
    def __init__(self) -> None:
        self.messaggi: list[tuple[str, str]] = []
        self.callback: list[tuple[str, str]] = []
        self.markup: list[tuple] = []

    def send_message(self, text, *, chat_id=None, keyboard=None, preview=True):  # noqa: ANN001, ARG002
        self.messaggi.append((str(chat_id), text))
        return {"message_id": len(self.messaggi)}

    def send_photo(self, photo, caption, *, chat_id=None, keyboard=None):  # noqa: ANN001, ARG002
        self.messaggi.append((str(chat_id), caption))
        return {}

    def answer_callback_query(self, callback_id, text="", show_alert=False):  # noqa: ANN001, ARG002
        self.callback.append((callback_id, text))
        return {}

    def edit_message_reply_markup(self, chat_id, message_id, keyboard=None):  # noqa: ANN001, ARG002
        self.markup.append((chat_id, message_id))
        return {}


class FakeClient:
    def __init__(self, hit_per_fonte: int = 3, dettagli: dict | None = None) -> None:
        self.hit_per_fonte = hit_per_fonte
        self.dettagli = dettagli or {}
        self.richieste = 0
        self.chiamate: list[tuple] = []

    def search(self, query, page=1, deal_filter=None):  # noqa: ANN001
        self.chiamate.append((query, page, deal_filter))
        if deal_filter and self.hit_per_fonte == 0:
            return []
        return [
            SearchHit(
                asin=f"{deal_filter or 'p'}{i}",
                title="Caffeina Pura 400 compresse 200 mg",
                url=f"https://www.amazon.it/dp/{deal_filter or 'p'}{i}",
                card_price=9.95,
            )
            for i in range(self.hit_per_fonte)
        ]

    def detail(self, asin):  # noqa: ANN001
        self.richieste += 1
        return Detail(
            asin=asin,
            title="Caffeina Pura 400 compresse 200 mg",
            url=f"https://www.amazon.it/dp/{asin}",
            price=9.95,
            list_price=12.95,
            discount_pct=23.0,
            coupon_pct=20.0,
            available=True,
            image=None,
        )


class TestFontiAdattive(unittest.TestCase):
    def _scanner(self, tmp: str, client: FakeClient) -> tuple[Scanner, Store]:
        cfg = Settings(
            queries=["caffeina"],
            pages=1,
            filtri_offerte=["offerte_del_giorno"],
            db_path=Path(tmp) / "state.db",
            parallelismo=2,
        )
        store = Store(cfg.db_path)
        return Scanner(cfg, store, client, telegram=None), store

    def test_fonte_sospesa_dopo_tre_giri_vuoti(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(hit_per_fonte=0)
            scanner, store = self._scanner(tmp, client)
            for _ in range(3):
                scanner.scan(dry=True)
            # al quarto giro la fonte deve essere saltata
            client.chiamate.clear()
            scanner.scan(dry=True)
            filtri = [c[2] for c in client.chiamate]
            self.assertNotIn("offerte_del_giorno", filtri)
            self.assertEqual(len(filtri), 1)

    def test_fonte_riattivata_con_resa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(hit_per_fonte=2)
            scanner, store = self._scanner(tmp, client)
            scanner.scan(dry=True)
            stats = store.get_setting("fonti_stats", {})
            self.assertEqual(stats["offerte_del_giorno"]["vuoti"], 0)
            self.assertIsNone(stats["offerte_del_giorno"]["salta_fino"])


class TestDettagliParalleli(unittest.TestCase):
    def test_valuta_tutti_i_candidati(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(hit_per_fonte=3)
            cfg = Settings(
                queries=["caffeina"],
                pages=1,
                filtri_offerte=[],
                db_path=Path(tmp) / "state.db",
                parallelismo=2,
                max_dettagli_per_scan=3,
            )
            store = Store(cfg.db_path)
            scanner = Scanner(cfg, store, client, telegram=None)
            result = scanner.scan(dry=True)
            self.assertEqual(result.valutati, 3)
            self.assertGreater(result.durata_s, 0)


class TestDigest(unittest.TestCase):
    def test_formato(self) -> None:
        detail = Detail(
            asin="A1",
            title="Caffeina Pura 400 compresse 200 mg",
            url="https://www.amazon.it/dp/A1",
            price=9.95,
            list_price=12.95,
            discount_pct=47.0,
        )
        from caffeina_bot.analyze import Verdict, parse_title

        verdict = Verdict(alert=True, kind="folle", discount_pct=47.0, effective_price=9.95, dose_eur=0.0249)
        testo = format_digest([(detail, verdict, parse_title(detail.title))] * 3)
        self.assertIn("3 sconti sopra soglia", testo)
        self.assertIn("−47%", testo)
        self.assertIn("€/dose", testo)


class TestCallbackSilenzia(unittest.TestCase):
    def test_mute_30_giorni(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(telegram_token="t", chat_id="123", db_path=Path(tmp) / "state.db")
            store = Store(cfg.db_path)
            store.upsert_product({"asin": "A1", "title": "t"})
            tg = FakeTelegram()
            runner = BotRunner(cfg, store, client=None, telegram=tg, scanner=None)  # type: ignore[arg-type]
            runner._handle_update(
                {
                    "callback_query": {
                        "id": "cb1",
                        "data": "mute:A1",
                        "message": {"chat": {"id": 123}, "message_id": 10},
                    }
                }
            )
            self.assertTrue(store.is_muted("A1"))
            self.assertEqual(tg.callback[0][1], "Silenziato per 30 giorni")
            self.assertEqual(len(tg.markup), 1)
            self.assertEqual(int(tg.markup[0][0]), 123)
            self.assertEqual(tg.markup[0][1], 10)

    def test_callback_chat_non_autorizzata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(telegram_token="t", chat_id="123", db_path=Path(tmp) / "state.db")
            store = Store(cfg.db_path)
            tg = FakeTelegram()
            runner = BotRunner(cfg, store, client=None, telegram=tg, scanner=None)  # type: ignore[arg-type]
            runner._handle_update(
                {
                    "callback_query": {
                        "id": "cb1",
                        "data": "mute:A1",
                        "message": {"chat": {"id": 999}, "message_id": 10},
                    }
                }
            )
            self.assertFalse(store.is_muted("A1"))
            self.assertIn("non autorizzata", tg.callback[0][1])


class TestComandiDati(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Settings(telegram_token="t", chat_id="123", db_path=Path(self.tmp.name) / "state.db")
        self.store = Store(cfg.db_path)
        self.store.upsert_product(
            {
                "asin": "A1",
                "title": "Caffeina Pura 400 compresse 200 mg",
                "price": 9.95,
                "list_price": 12.95,
                "discount": 47.0,
                "dose_eur": 0.0199,
                "pills": 400,
                "mg_per_pill": 200.0,
                "available": 1,
            }
        )
        self.store.add_history("A1", 9.95)
        self.store.record_alert(
            asin="A1", price=9.95, list_price=12.95, discount=47.0, dose_eur=0.0199, kind="folle"
        )
        self.tg = FakeTelegram()
        self.runner = BotRunner(cfg, self.store, client=None, telegram=self.tg, scanner=None)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_top(self) -> None:
        self.runner._handle_update({"message": {"chat": {"id": 123}, "text": "/top"}})
        self.assertIn("Top sconti", self.tg.messaggi[-1][1])
        self.assertIn("−47%", self.tg.messaggi[-1][1])

    def test_info(self) -> None:
        self.runner._handle_update({"message": {"chat": {"id": 123}, "text": "/info A1"}})
        testo = self.tg.messaggi[-1][1]
        self.assertIn("A1", testo)
        self.assertIn("0.020", testo)

    def test_info_sconosciuto(self) -> None:
        self.runner._handle_update({"message": {"chat": {"id": 123}, "text": "/info B0ZZZ"}})
        self.assertIn("non presente", self.tg.messaggi[-1][1])

    def test_report(self) -> None:
        self.runner._handle_update({"message": {"chat": {"id": 123}, "text": "/report"}})
        testo = self.tg.messaggi[-1][1]
        self.assertIn("Ultimi 7 giorni", testo)
        self.assertIn("Alert inviati: 1", testo)


class TestSogliaDose(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Settings(telegram_token="t", chat_id="123", db_path=Path(self.tmp.name) / "state.db")
        self.store = Store(cfg.db_path)
        self.store.seed_settings({"dose_max_eur": 0.025})
        for i, dose in enumerate([0.02, 0.022, 0.0249, 0.09, 0.11]):
            self.store.upsert_product(
                {"asin": f"D{i}", "title": f"prodotto {i}", "dose_eur": dose, "available": 1}
            )
        self.tg = FakeTelegram()
        self.runner = BotRunner(cfg, self.store, client=None, telegram=self.tg, scanner=None)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invia(self, testo: str) -> str:
        self.runner._handle_update({"message": {"chat": {"id": 123}, "text": testo}})
        return self.tg.messaggi[-1][1]

    def test_senza_argomento_mostra_mercato(self) -> None:
        testo = self._invia("/dose")
        self.assertIn("Tetto attuale", testo)
        self.assertIn("mediana", testo)
        self.assertIn("25° perc.", testo)

    def test_auto_calcola_dal_mercato(self) -> None:
        testo = self._invia("/dose auto")
        valore = self.store.get_setting("dose_max_eur")
        self.assertAlmostEqual(valore, 0.018, places=3)
        self.assertIn("calcolata dal mercato", testo)

    def test_valore_fuori_range(self) -> None:
        testo = self._invia("/dose 0,5")
        self.assertIn("0,005", testo)
        self.assertEqual(self.store.get_setting("dose_max_eur"), 0.025)

    def test_valore_sopra_mediana_avvisa(self) -> None:
        testo = self._invia("/dose 0,05")
        self.assertIn("Sopra la mediana", testo)
        self.assertEqual(self.store.get_setting("dose_max_eur"), 0.05)


if __name__ == "__main__":
    unittest.main()

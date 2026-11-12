"""Test delle fonti di ricerca (filtri Offerte) e della scelta immagine/testo."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from caffeina_bot.amazon import build_search_url, resolve_deal_filter
from caffeina_bot.config import Settings
from caffeina_bot.scanner import Scanner
from caffeina_bot.store import Store


class TestUrlRicerca(unittest.TestCase):
    def test_semplice(self) -> None:
        url = build_search_url("www.amazon.it", "caffeina compresse")
        self.assertEqual(url, "https://www.amazon.it/s?k=caffeina+compresse")

    def test_pagina(self) -> None:
        url = build_search_url("www.amazon.it", "caffeina", page=2)
        self.assertTrue(url.endswith("&page=2"))

    def test_filtro_per_nome(self) -> None:
        url = build_search_url("www.amazon.it", "caffeina", deal_filter="offerte_del_giorno")
        self.assertIn("rh=p_n_deal_type%3A210770023031", url)

    def test_filtro_per_id(self) -> None:
        url = build_search_url("www.amazon.it", "caffeina", deal_filter="26901107031")
        self.assertIn("rh=p_n_deal_type%3A26901107031", url)

    def test_nome_sconosciuto_ignorato(self) -> None:
        self.assertIsNone(resolve_deal_filter("filtro_inventato"))
        self.assertIsNone(resolve_deal_filter(""))
        url = build_search_url("www.amazon.it", "caffeina", deal_filter=None)
        self.assertNotIn("p_n_deal_type", url)


class FakeClient:
    def __init__(self) -> None:
        self.chiamate: list[tuple] = []

    def search(self, query: str, page: int = 1, deal_filter=None):  # noqa: ANN001
        self.chiamate.append((query, page, deal_filter))
        return []

    def detail(self, asin: str):  # noqa: ANN201
        raise AssertionError("nessun dettaglio atteso")


class FakeTelegram:
    def __init__(self, fail_photo: bool = False) -> None:
        self.foto: list[tuple] = []
        self.messaggi: list[str] = []
        self.fail_photo = fail_photo

    def send_photo(self, photo_url, caption, *, chat_id=None, keyboard=None):  # noqa: ANN001, ARG002
        if self.fail_photo:
            raise RuntimeError("invio foto non disponibile")
        self.foto.append((photo_url, caption))
        return {}

    def send_message(self, text, *, chat_id=None, keyboard=None, preview=True):  # noqa: ANN001, ARG002
        self.messaggi.append(text)
        return {}


class TestFontiRicerca(unittest.TestCase):
    def test_numero_di_ricerche(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(
                queries=["caffeina compresse", "caffeina capsule"],
                pages=1,
                filtri_offerte=["offerte_e_risparmi", "offerte_del_giorno"],
                db_path=Path(tmp) / "state.db",
                max_dettagli_per_scan=1,
            )
            store = Store(cfg.db_path)
            fake = FakeClient()
            scanner = Scanner(cfg, store, fake, telegram=None)  # type: ignore[arg-type]
            scanner.scan(dry=True)
            # 2 query x (1 pagina + 2 filtri)
            self.assertEqual(len(fake.chiamate), 6)
            filtri = [c[2] for c in fake.chiamate if c[2]]
            self.assertEqual(filtri, ["offerte_e_risparmi", "offerte_del_giorno"] * 2)

    def test_senza_filtri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(
                queries=["caffeina compresse"],
                pages=2,
                filtri_offerte=[],
                db_path=Path(tmp) / "state.db",
            )
            store = Store(cfg.db_path)
            fake = FakeClient()
            scanner = Scanner(cfg, store, fake, telegram=None)  # type: ignore[arg-type]
            scanner.scan(dry=True)
            self.assertEqual(len(fake.chiamate), 2)
            self.assertTrue(all(c[2] is None for c in fake.chiamate))


class TestInvioAlert(unittest.TestCase):
    def _scanner(self, tmp: str, telegram, invia_immagine: bool = True) -> Scanner:
        cfg = Settings(
            queries=["caffeina"],
            filtri_offerte=[],
            db_path=Path(tmp) / "state.db",
            invia_immagine=invia_immagine,
        )
        store = Store(cfg.db_path)
        return Scanner(cfg, store, client=None, telegram=telegram)  # type: ignore[arg-type]

    def test_foto_con_testo_breve(self) -> None:
        from caffeina_bot.amazon import Detail

        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTelegram()
            scanner = self._scanner(tmp, tg)
            detail = Detail(asin="X1", title="T", url="https://amazon.it/dp/X1", image="https://img/x.jpg")
            self.assertTrue(scanner._send("testo breve", detail))
            self.assertEqual(len(tg.foto), 1)
            self.assertEqual(tg.messaggi, [])

    def test_testo_lungo_resta_messaggio(self) -> None:
        from caffeina_bot.amazon import Detail

        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTelegram()
            scanner = self._scanner(tmp, tg)
            detail = Detail(asin="X1", title="T", url="https://amazon.it/dp/X1", image="https://img/x.jpg")
            self.assertTrue(scanner._send("x" * 1200, detail))
            self.assertEqual(tg.foto, [])
            self.assertEqual(len(tg.messaggi), 1)

    def test_immagine_disattivata(self) -> None:
        from caffeina_bot.amazon import Detail

        with tempfile.TemporaryDirectory() as tmp:
            tg = FakeTelegram()
            scanner = self._scanner(tmp, tg, invia_immagine=False)
            detail = Detail(asin="X1", title="T", url="https://amazon.it/dp/X1", image="https://img/x.jpg")
            scanner._send("testo breve", detail)
            self.assertEqual(tg.foto, [])
            self.assertEqual(len(tg.messaggi), 1)


if __name__ == "__main__":
    unittest.main()

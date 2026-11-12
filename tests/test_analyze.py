"""Test delle regole di valutazione (nessuna rete)."""

from __future__ import annotations

import unittest

from caffeina_bot.amazon import parse_price
from caffeina_bot.analyze import dose_cost, evaluate, parse_title

REGOLE = dict(
    sconto_minimo=30.0,
    sconto_folle=45.0,
    dose_max_eur=0.03,
    calo_storico_minimo=12.0,
    solo_disponibili=True,
)


class TestParsePrice(unittest.TestCase):
    def test_formati_italiani(self) -> None:
        self.assertEqual(parse_price("9,95 €"), 9.95)
        self.assertEqual(parse_price("1.234,56 €"), 1234.56)
        self.assertEqual(parse_price("12,95"), 12.95)
        self.assertEqual(parse_price("0,02 € / unità"), 0.02)
        self.assertIsNone(parse_price(""))
        self.assertIsNone(parse_price("non disponibile"))


class TestParseTitle(unittest.TestCase):
    def test_compresse(self) -> None:
        info = parse_title("Caffeina Pura – 400 compresse – 200 mg per compressa – Alto Dosaggio")
        self.assertTrue(info.is_caffeine)
        self.assertFalse(info.is_noise)
        self.assertEqual(info.mg_per_pill, 200.0)
        self.assertEqual(info.pills, 400)

    def test_capsule_con_mg_lontano(self) -> None:
        info = parse_title("vit4ever Compresse di Caffeina da 200 mg ciascuna – 500 Compresse")
        self.assertEqual(info.mg_per_pill, 200.0)
        self.assertEqual(info.pills, 500)

    def test_caffe_non_e_prodotto(self) -> None:
        info = parse_title("Cialde di caffè compatibili Nespresso, confezione 100 pezzi")
        self.assertFalse(info.is_caffeine)
        self.assertTrue(info.is_noise)

    def test_decaffeinato_non_e_prodotto(self) -> None:
        info = parse_title("Bialetti Caffè d'Italia, Box 16 Capsule, Decaffeinato")
        self.assertFalse(info.is_caffeine)
        self.assertTrue(info.is_noise)

    def test_caffeina_pura_non_e_rumore(self) -> None:
        info = parse_title("Caffeina Pura 200 mg, 400 compresse, senza eccipienti")
        self.assertTrue(info.is_caffeine)
        self.assertFalse(info.is_noise)

    def test_titolo_senza_dati(self) -> None:
        info = parse_title("Caffeina in polvere")
        self.assertTrue(info.is_caffeine)
        self.assertIsNone(info.mg_per_pill)
        self.assertIsNone(info.pills)


class TestDoseCost(unittest.TestCase):
    def test_dose_200mg(self) -> None:
        # 9,95 € / (400 compresse da 200 mg) = 0,0249 €/dose
        self.assertAlmostEqual(dose_cost(9.95, 200, 400), 0.024875, places=6)

    def test_dosaggio_100mg(self) -> None:
        # 100 mg per compressa: servono 2 compresse per dose da 200 mg
        self.assertAlmostEqual(dose_cost(10.0, 100, 100), 0.2, places=6)

    def test_dati_mancanti(self) -> None:
        self.assertIsNone(dose_cost(10.0, None, 100))
        self.assertIsNone(dose_cost(None, 200, 100))


class TestEvaluate(unittest.TestCase):
    def _eval(self, **kwargs):
        base = dict(
            price=9.95,
            list_price=18.90,
            discount_pct=47.0,
            coupon_pct=None,
            mg_per_pill=200.0,
            pills=400,
            available=True,
            history_min=None,
            is_caffeine=True,
            is_noise=False,
            **REGOLE,
        )
        base.update(kwargs)
        return evaluate(**base)

    def test_sconto_folle(self) -> None:
        verdict = self._eval()
        self.assertTrue(verdict.alert)
        self.assertEqual(verdict.kind, "folle")
        self.assertEqual(verdict.label, "SCONTO FOLLE")

    def test_sconto_alto_ma_caro_per_dose(self) -> None:
        # 32% di sconto ma 0,05 €/dose: non è un vero affare
        verdict = self._eval(price=20.0, list_price=29.4, discount_pct=32.0, pills=200)
        self.assertFalse(verdict.alert)
        self.assertIn("oltre il limite", " ".join(verdict.reasons))

    def test_sconto_sotto_soglia(self) -> None:
        verdict = self._eval(price=12.0, list_price=13.0, discount_pct=8.0)
        self.assertFalse(verdict.alert)

    def test_calo_sotto_minimo_storico(self) -> None:
        verdict = self._eval(
            price=12.0, list_price=None, discount_pct=0.0, history_min=16.0, pills=400
        )
        self.assertTrue(verdict.alert)
        self.assertEqual(verdict.kind, "calo")

    def test_non_disponibile(self) -> None:
        verdict = self._eval(available=False)
        self.assertFalse(verdict.alert)
        self.assertIn("non disponibile", verdict.reasons)

    def test_prodotto_non_pertinente(self) -> None:
        verdict = self._eval(is_caffeine=False, is_noise=True)
        self.assertFalse(verdict.alert)

    def test_coupon_migliora_la_dose(self) -> None:
        senza = self._eval(price=13.0, list_price=18.0, discount_pct=28.0, pills=400)
        con = self._eval(price=13.0, list_price=18.0, discount_pct=28.0, pills=400, coupon_pct=20.0)
        self.assertFalse(senza.alert)
        self.assertTrue(con.alert)

    def test_prezzo_sospetto(self) -> None:
        verdict = self._eval(price=1.2, list_price=10.0, discount_pct=88.0)
        self.assertFalse(verdict.alert)
        self.assertIn("sospetto", " ".join(verdict.reasons))


if __name__ == "__main__":
    unittest.main()

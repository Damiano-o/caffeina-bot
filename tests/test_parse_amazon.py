"""Test del parser HTML su frammenti rappresentativi di Amazon.it."""

from __future__ import annotations

import unittest

from caffeina_bot.amazon import parse_detail, parse_search

SEARCH_HTML = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0TEST0001" class="s-result-item s-asin">
  <h2><a href="/dp/B0TEST0001"><span>Caffeina Pura 400 compresse 200 mg</span></a></h2>
  <span class="a-price"><span class="a-offscreen">9,95 €</span></span>
</div>
<div data-component-type="s-search-result" data-asin="B0TEST0001" class="s-result-item s-asin AdHolder">
  <h2><a href="/dp/B0TEST0001"><span>Caffeina Pura 400 compresse 200 mg (sponsorizzato)</span></a></h2>
  <span class="a-price"><span class="a-offscreen">9,95 €</span></span>
</div>
<div data-component-type="s-search-result" data-asin="B0TEST0002" class="s-result-item s-asin">
  <h2><a href="/dp/B0TEST0002"><span>Caffeina capsule 180 capsule 200 mg</span></a></h2>
  <span class="a-price"><span class="a-offscreen">16,99 €</span></span>
</div>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<span id="productTitle">Caffeina Pura - 400 compresse - 200 mg per compressa</span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">9,95 €</span></span>
  <span>0,02&nbsp;&euro; per unit&agrave; (0,02 € / unità)</span>
  <span class="savingsPercentage">-23%</span>
  <span class="basisPrice"><span class="a-price a-text-price"><span class="a-offscreen">12,95 €</span></span></span>
  Prezzo più basso ultimi 30gg: 12,95 €
</div>
<div id="promoPriceBlockMessage_feature_div">Coupon: Risparmia 20%: applica coupon</div>
<div id="availability">Disponibilità immediata</div>
<span id="acrPopover" title="4,5 su 5 stelle"></span>
<span id="acrCustomerReviewText">1.234 valutazioni</span>
<img id="landingImage" src="https://m.media-amazon.com/images/I/test.jpg" />
</body></html>
"""


class TestParseSearch(unittest.TestCase):
    def test_deduplica_asin_sponsorizzati(self) -> None:
        hits = parse_search(SEARCH_HTML)
        self.assertEqual(len(hits), 2)
        asin = [h.asin for h in hits]
        self.assertEqual(asin, ["B0TEST0001", "B0TEST0002"])
        self.assertEqual(hits[0].card_price, 9.95)
        self.assertFalse(hits[0].sponsored)
        self.assertEqual(hits[0].url, "https://www.amazon.it/dp/B0TEST0001")

    def test_titoli(self) -> None:
        hits = parse_search(SEARCH_HTML)
        self.assertIn("Caffeina Pura", hits[0].title)


class TestParseDetail(unittest.TestCase):
    def test_campi_principali(self) -> None:
        detail = parse_detail(DETAIL_HTML, "B0TEST0001")
        self.assertEqual(detail.price, 9.95)
        self.assertEqual(detail.list_price, 12.95)
        self.assertEqual(detail.discount_pct, 23.0)
        self.assertEqual(detail.coupon_pct, 20.0)
        self.assertEqual(detail.per_unit_eur, 0.02)
        self.assertEqual(detail.last30_low, 12.95)
        self.assertEqual(detail.rating, 4.5)
        self.assertEqual(detail.reviews, 1234)
        self.assertTrue(detail.available)
        self.assertIn("m.media-amazon.com", detail.image or "")

    def test_non_disponibile(self) -> None:
        html = DETAIL_HTML.replace("Disponibilità immediata", "Attualmente non disponibile")
        detail = parse_detail(html, "B0TEST0001")
        self.assertFalse(detail.available)

    def test_senza_prezzo(self) -> None:
        html = DETAIL_HTML.replace("9,95 €", "")
        detail = parse_detail(html, "B0TEST0001")
        self.assertIsNone(detail.price)


if __name__ == "__main__":
    unittest.main()

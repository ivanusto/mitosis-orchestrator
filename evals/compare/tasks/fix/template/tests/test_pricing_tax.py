import unittest

from shop import pricing, promotions, tax
from shop.errors import UnknownRegion
from helpers import PRODUCTS, make_shop


class PricingTest(unittest.TestCase):
    def setUp(self):
        self.mug = [p for p in PRODUCTS if p.sku == "P-MUG"][0]

    def test_tier_prices(self):
        self.assertEqual(pricing.unit_price(self.mug, "standard"), 1050)
        self.assertEqual(pricing.unit_price(self.mug, "silver"), 1018)   # 31.5 rounds up
        self.assertEqual(pricing.unit_price(self.mug, "gold"), 997)      # 52.5 rounds up
        self.assertEqual(pricing.line_amount(self.mug, 3, "gold"), 2991)

    def test_price_list_and_cheapest(self):
        shop = make_shop()
        self.assertEqual(pricing.price_list(shop.catalog)["P-TEE"], 2499)
        self.assertEqual(pricing.cheapest(shop.catalog).sku, "P-SOCK")
        self.assertEqual(pricing.cheapest(shop.catalog, "home").sku, "P-MUG")


class TaxTest(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(tax.tax_for_amount(10000, "NL"), 2100)
        self.assertEqual(tax.tax_for_amount(2000, "US-CA"), 145)
        self.assertEqual(tax.tax_for_amount(5000, "US-OR"), 0)
        self.assertEqual(tax.tax_for_amount(5000, "NL", taxable=False), 0)
        self.assertEqual(tax.tax_for_amount(1050, "NL"), 221)
        self.assertEqual(tax.gross(1050, "NL"), 1271)
        with self.assertRaises(UnknownRegion):
            tax.rate_for("XX")

    def test_net_from_gross(self):
        self.assertEqual(tax.net_from_gross(1210, "NL"), 1000)


class PromotionTest(unittest.TestCase):
    def test_bulk_and_seasonal(self):
        self.assertEqual(promotions.bulk_bps(9), 0)
        self.assertEqual(promotions.bulk_bps(10), 500)
        self.assertEqual(promotions.bulk_bps(30), 1000)
        self.assertEqual(promotions.bulk_discount(8325, 25), 833)
        self.assertEqual(promotions.line_promotion(1700, 1, "seasonal"), 255)
        self.assertEqual(promotions.line_promotion(10000, 10, "seasonal"), 1500)   # best single promotion wins
        self.assertEqual(promotions.line_promotion(1700, 1, "home"), 0)


if __name__ == "__main__":
    unittest.main()

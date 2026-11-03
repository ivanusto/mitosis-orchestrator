import datetime
import unittest

from shop import shipping
from shop.errors import OverweightError


class ShippingTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(shipping.tier_for("EU", 300), (500, 495))
        self.assertEqual(shipping.tier_for("EU", 1200), (2000, 795))
        self.assertEqual(shipping.tier_for("US", 4000), (5000, 1895))
        self.assertEqual(shipping.max_weight("UK"), 10000)

    def test_overweight(self):
        with self.assertRaises(OverweightError):
            shipping.tier_for("EU", 10001)

    def test_quote_and_free_shipping(self):
        self.assertEqual(shipping.quote("EU", 1200, 5000), 795)
        self.assertEqual(shipping.quote("EU", 1200, 10000), 0)
        self.assertEqual(shipping.quote("UK", 1200, 11999), 895)

    def test_delivery_date(self):
        self.assertEqual(shipping.delivery_date("EU", datetime.date(2026, 3, 2)), datetime.date(2026, 3, 5))
        self.assertEqual(shipping.delivery_date("US", datetime.date(2026, 3, 2)), datetime.date(2026, 3, 10))

    def test_split_parcels(self):
        parcels = shipping.split_parcels("EU", [6000, 5000, 3000, 2000])
        self.assertEqual(sorted(sum(p) for p in parcels), [7000, 9000])
        with self.assertRaises(OverweightError):
            shipping.split_parcels("EU", [10001])


if __name__ == "__main__":
    unittest.main()

import unittest

from billing import discounts


class DiscountTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(discounts.discount_cents(10000), 0)
        self.assertEqual(discounts.discount_cents(60000), 3000)
        self.assertEqual(discounts.discount_cents(200000), 20000)

    def test_tier_half_cent(self):
        self.assertEqual(discounts.discount_cents(50010), 2501)

    def test_coupon(self):
        self.assertEqual(discounts.apply_coupon(2000, 500), 100)
        self.assertEqual(discounts.apply_coupon(1050, 500), 53)
        self.assertEqual(discounts.apply_coupon(1050, 0), 0)


if __name__ == "__main__":
    unittest.main()

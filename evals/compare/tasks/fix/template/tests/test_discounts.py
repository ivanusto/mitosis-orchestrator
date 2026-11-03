import unittest

from shop import discounts
from shop.discounts import Coupon
from shop.errors import ValidationError


class CouponTest(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(discounts.coupon_amount(Coupon("A", "percent", 1000), 3333), 333)
        self.assertEqual(discounts.coupon_amount(Coupon("B", "fixed", 500), 300), 300)
        self.assertEqual(discounts.coupon_amount(Coupon("C", "fixed", 500, min_subtotal=1000), 999), 0)
        with self.assertRaises(ValidationError):
            discounts.coupon_amount(Coupon("D", "weird", 1), 100)

    def test_spread_proportionally(self):
        self.assertEqual(discounts.spread([1000, 1000], 100), [50, 50])
        self.assertEqual(discounts.spread([300, 100], 100), [75, 25])
        self.assertEqual(discounts.spread([0, 0], 100), [0, 0])

    def test_spread_gives_spare_cents_to_the_largest_remainders(self):
        shares = discounts.apply_coupon([1999, 4550, 333], Coupon("FIVE", "fixed", 500))
        self.assertEqual(shares, [145, 331, 24])
        self.assertEqual(sum(shares), 500)

    def test_book(self):
        book = discounts.CouponBook()
        book.add(Coupon("Spring", "percent", 500))
        self.assertEqual(book.get("SPRING").value, 500)
        self.assertIsNone(book.get(None))
        with self.assertRaises(ValidationError):
            book.get("nope")


if __name__ == "__main__":
    unittest.main()

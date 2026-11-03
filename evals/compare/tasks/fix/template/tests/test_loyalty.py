import unittest

from shop.errors import InvalidState
from shop.loyalty import LoyaltyBook, points_for


class LoyaltyTest(unittest.TestCase):
    def test_points(self):
        self.assertEqual(points_for(8099, "standard"), 80)
        self.assertEqual(points_for(8099, "gold"), 240)
        self.assertEqual(points_for(-5, "silver"), 0)

    def test_earn_redeem_reverse(self):
        book = LoyaltyBook()
        self.assertEqual(book.earn("C1", 5000, "silver", "ORD-1"), 100)
        self.assertEqual(book.redeem("C1", 30), 60)
        self.assertEqual(book.balance("C1"), 70)
        with self.assertRaises(InvalidState):
            book.redeem("C1", 71)
        self.assertEqual(book.reverse("C1", "ORD-1"), 100)
        self.assertEqual(book.balance("C1"), 0)
        self.assertEqual(book.history("C1")[0], ("ORD-1", 100))


if __name__ == "__main__":
    unittest.main()

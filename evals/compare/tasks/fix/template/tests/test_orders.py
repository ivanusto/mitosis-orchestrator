import unittest

from shop.errors import InvalidState, NotFound, ValidationError
from helpers import ShopCase


class OrderPricingTest(ShopCase):
    def test_plain_order(self):
        order = self.shop.orders.create("C1", [("P-MUG", 2)])
        line = order.lines[0]
        self.assertEqual((line.unit_cents, line.amount_cents, line.discount_cents, line.tax_cents), (1050, 2100, 0, 441))
        self.assertEqual(order.shipping_cents, 795)  # 600 g, EU tier up to 2000 g
        self.assertEqual(order.total_cents, 2100 + 441 + 795)
        self.assertEqual(order.status, "new")

    def test_duplicate_skus_merge_and_lines_sort(self):
        order = self.shop.orders.create("C1", [("P-TEE", 1), ("P-MUG", 1), ("P-TEE", 1)])
        self.assertEqual([(l.sku, l.qty) for l in order.lines], [("P-MUG", 1), ("P-TEE", 2)])

    def test_untaxed_book_and_us_tax(self):
        order = self.shop.orders.create("C3", [("P-LAMP", 2), ("P-MUG", 1)], "BIG")
        self.assertEqual([(l.unit_cents, l.amount_cents, l.discount_cents, l.tax_cents) for l in order.lines], [(4413, 8826, 0, 640), (1018, 1018, 0, 74)])
        self.assertEqual(order.shipping_cents, 1895)
        self.assertEqual(order.total_cents, 12453)
        book = self.shop.orders.create("C1", [("P-BOOK", 1)])
        self.assertEqual(book.lines[0].tax_cents, 0)

    def test_coupon_is_spread_over_lines(self):
        order = self.shop.orders.create("C1", [("P-BOOK", 1), ("P-LAMP", 1), ("P-SOCK", 1)], "FIVE")
        self.assertEqual([l.discount_cents for l in order.lines], [145, 331, 24])
        self.assertEqual([l.tax_cents for l in order.lines], [0, 886, 65])
        self.assertEqual((order.subtotal_cents, order.discount_cents, order.tax_cents), (6882, 500, 951))
        self.assertEqual(order.total_cents, 8128)
        self.assertEqual(order.coupon_code, "FIVE")

    def test_promotions_and_gold_tier(self):
        socks = self.shop.orders.create("C1", [("P-SOCK", 25)])
        self.assertEqual((socks.lines[0].amount_cents, socks.lines[0].discount_cents, socks.lines[0].tax_cents), (8325, 833, 1573))
        self.assertEqual(socks.total_cents, 9860)
        gold = self.shop.orders.create("C2", [("P-MUG", 3), ("P-TEE", 2), ("P-SEAS", 1)], "TENPCT")
        self.assertEqual([(l.sku, l.unit_cents, l.amount_cents, l.discount_cents, l.tax_cents) for l in gold.lines],
                         [("P-MUG", 997, 2991, 299, 511), ("P-SEAS", 1615, 1615, 379, 235), ("P-TEE", 2374, 4748, 475, 812)])
        self.assertEqual((gold.shipping_cents, gold.total_cents), (1295, 11054))

    def test_free_shipping_over_threshold(self):
        order = self.shop.orders.create("C1", [("P-LAMP", 3)])
        self.assertEqual(order.shipping_cents, 0)

    def test_heaviest_parcel_pays_the_top_tier(self):
        order = self.shop.orders.create("C1", [("P-KIT", 4)])
        self.assertEqual(order.weight_g, 10000)
        self.assertEqual(order.shipping_cents, 1995)
        self.assertEqual(order.total_cents, 8000 + 1680 + 1995)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.shop.orders.create("C1", [])
        with self.assertRaises(ValidationError):
            self.shop.orders.create("C1", [("P-MUG", 0)])
        with self.assertRaises(NotFound):
            self.shop.orders.create("C1", [("P-NOPE", 1)])
        with self.assertRaises(ValidationError):
            self.shop.orders.create("C1", [("P-MUG", 1)], "NOSUCH")


class OrderPaymentTest(ShopCase):
    def test_payments_must_add_up(self):
        order = self.shop.orders.create("C1", [("P-MUG", 1)])
        with self.assertRaises(ValidationError):
            self.shop.orders.mark_paid(order.id, [("card", 100)])
        self.shop.orders.mark_paid(order.id, [("card", 1000), ("gift", order.total_cents - 1000)])
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.paid_at, self.shop.clock.now())
        with self.assertRaises(InvalidState):
            self.shop.orders.mark_paid(order.id)

    def test_listing(self):
        a = self.shop.orders.create("C1", [("P-MUG", 1)])
        b = self.shop.orders.create("C2", [("P-MUG", 1)])
        self.assertEqual([o.id for o in self.shop.orders.all()], [a.id, b.id])
        self.assertEqual([o.id for o in self.shop.orders.for_customer("C2")], [b.id])


if __name__ == "__main__":
    unittest.main()

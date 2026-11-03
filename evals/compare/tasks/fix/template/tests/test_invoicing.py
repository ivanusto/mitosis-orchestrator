import unittest

from shop import invoicing
from helpers import ShopCase


class InvoiceTest(ShopCase):
    def make(self, items, customer="C1", coupon=None):
        order = self.shop.orders.create(customer, items, coupon)
        self.shop.orders.mark_paid(order.id)
        return order, self.shop.invoices.build(order.id)

    def test_lines_and_total_match_the_order(self):
        order, invoice = self.make([("P-MUG", 2), ("P-TEE", 1)])
        self.assertEqual(invoice.number, "INV-0001")
        self.assertEqual(invoice.total, order.total_cents)
        self.assertEqual([l.sku for l in invoice.lines], ["P-MUG", "P-TEE"])

    def test_shipping_is_shared_in_proportion_to_net_amounts(self):
        order, invoice = self.make([("P-BOOK", 1), ("P-LAMP", 1), ("P-MUG", 1)])
        self.assertEqual([l.shipping for l in invoice.lines], [209, 476, 110])
        self.assertEqual([l.total for l in invoice.lines], [2208, 5982, 1381])
        self.assertEqual(invoice.total, 9571)
        self.assertEqual(invoice.shipping, order.shipping_cents)

    def test_free_shipping_has_no_shares(self):
        _, invoice = self.make([("P-LAMP", 3)])
        self.assertEqual([l.shipping for l in invoice.lines], [0])

    def test_installments(self):
        self.assertEqual(invoicing.installments(10000, 3), [3334, 3333, 3333])
        self.assertEqual(invoicing.installments(1001, 4), [251, 250, 250, 250])

    def test_render_and_listing(self):
        order, invoice = self.make([("P-MUG", 1)])
        text = invoicing.render(invoice)
        self.assertIn("INV-0001 for order %s" % order.id, text)
        self.assertIn("P-MUG", text)
        self.assertTrue(text.endswith("total 17.66"))
        self.assertEqual(self.shop.invoices.for_order(order.id), invoice)
        self.assertEqual(self.shop.invoices.all(), [invoice])


if __name__ == "__main__":
    unittest.main()

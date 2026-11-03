import datetime
import unittest

from shop.errors import InvalidState, ReturnWindowClosed, ValidationError
from helpers import ShopCase


class ReturnTest(ShopCase):
    def setUp(self):
        super().setUp()
        order = self.shop.orders.create("C1", [("P-BOOK", 1), ("P-LAMP", 1), ("P-MUG", 1)])
        self.shop.orders.mark_paid(order.id, [("card", 2500), ("gift", order.total_cents - 2500)])
        self.shop.fulfilment.reserve(order.id)
        self.shop.invoices.build(order.id)
        self.order = order

    def ship(self):
        self.shop.ship(self.order.id)

    def test_window_is_ten_business_days_from_the_last_shipment(self):
        self.ship()
        self.assertEqual(self.shop.returns.deadline(self.order.id), datetime.date(2026, 3, 16))

    def test_refund_is_the_invoice_line_and_goes_back_in_proportion_to_payments(self):
        self.ship()
        record = self.shop.return_item(self.order.id, "P-LAMP", 1)
        self.assertEqual(record.refund_cents, 5982)
        self.assertEqual(record.refunds, [("card", -1563), ("gift", -4419)])
        self.assertEqual(sum(c for _, c in record.refunds), -record.refund_cents)

    def test_returned_goods_go_back_on_the_shelf(self):
        self.ship()
        self.assertEqual(self.shop.inventory.on_hand("P-LAMP"), 9)
        self.shop.return_item(self.order.id, "P-LAMP", 1)
        self.assertEqual(self.shop.inventory.on_hand("P-LAMP"), 10)
        self.assertEqual(self.shop.returns.total_refunded(), 5982)

    def test_cannot_return_more_than_was_shipped_or_after_the_window(self):
        with self.assertRaises(InvalidState):
            self.shop.returns.deadline(self.order.id)
        self.ship()
        with self.assertRaises(ValidationError):
            self.shop.return_item(self.order.id, "P-MUG", 2)
        self.shop.clock.set(datetime.datetime(2026, 3, 17, 9, 0))
        with self.assertRaises(ReturnWindowClosed):
            self.shop.return_item(self.order.id, "P-MUG", 1)

    def test_refund_notification(self):
        self.ship()
        self.shop.return_item(self.order.id, "P-MUG", 1)
        message = self.shop.outbox.messages("refund")[0]
        self.assertEqual(message["to"], "ann@example.org")
        self.assertIn("13.81", message["body"])


if __name__ == "__main__":
    unittest.main()

import datetime
import unittest

from shop.errors import InvalidState, OutOfStock, ValidationError
from helpers import ShopCase


class ReserveAndShipTest(ShopCase):
    def pay(self, customer, items):
        order = self.shop.orders.create(customer, items)
        self.shop.orders.mark_paid(order.id)
        return order

    def test_reserve_moves_stock_to_reserved(self):
        order = self.pay("C1", [("P-MUG", 2), ("P-TEE", 1)])
        self.shop.fulfilment.reserve(order.id)
        self.assertEqual(order.status, "allocated")
        self.assertEqual((self.shop.inventory.reserved("P-MUG"), self.shop.inventory.reserved("P-TEE")), (2, 1))

    def test_reserve_is_all_or_nothing(self):
        first = self.pay("C1", [("P-SEAS", 3)])
        self.shop.fulfilment.reserve(first.id)
        second = self.pay("C5", [("P-MUG", 1), ("P-SEAS", 3)])
        with self.assertRaises(OutOfStock):
            self.shop.fulfilment.reserve(second.id)
        self.assertEqual(self.shop.inventory.reserved("P-MUG"), 0)
        self.assertEqual(self.shop.inventory.reserved("P-SEAS"), 3)
        self.assertEqual(second.status, "paid")

    def test_reserve_needs_a_paid_order(self):
        order = self.shop.orders.create("C1", [("P-MUG", 1)])
        with self.assertRaises(InvalidState):
            self.shop.fulfilment.reserve(order.id)

    def test_ship_everything(self):
        order = self.pay("C1", [("P-MUG", 2)])
        self.shop.fulfilment.reserve(order.id)
        shipment = self.shop.fulfilment.ship(order.id)
        self.assertEqual((shipment["number"], shipment["weight_g"], shipment["tier_price_cents"]), (1, 600, 795))
        self.assertEqual(shipment["date"], datetime.date(2026, 3, 2))
        self.assertEqual(order.status, "shipped")
        self.assertEqual(self.shop.inventory.snapshot()["P-MUG"], (28, 0))
        with self.assertRaises(InvalidState):
            self.shop.fulfilment.cancel(order.id)

    def test_ship_checks_quantities(self):
        order = self.pay("C1", [("P-MUG", 2)])
        self.shop.fulfilment.reserve(order.id)
        with self.assertRaises(ValidationError):
            self.shop.fulfilment.ship(order.id, {"P-MUG": 3})

    def test_ship_by_skips_the_weekend(self):
        order = self.pay("C1", [("P-MUG", 1)])
        self.assertEqual(self.shop.fulfilment.ship_by(order.id), datetime.date(2026, 3, 4))
        self.shop.clock.set(datetime.datetime(2026, 3, 5, 9, 0))
        late = self.pay("C1", [("P-MUG", 1)])
        self.assertEqual(self.shop.fulfilment.ship_by(late.id), datetime.date(2026, 3, 9))


class HeavyOrdersTest(ShopCase):
    """Two 10 kg pallet-kit orders compete for the same stock."""

    def setUp(self):
        super().setUp()
        self.a = self.shop.place_order("C1", [("P-KIT", 4)])
        self.b = self.shop.place_order("C5", [("P-KIT", 4)])

    def test_both_orders_are_allocated(self):
        self.assertEqual(self.a.shipping_cents, 1995)
        self.assertEqual((self.shop.inventory.reserved("P-KIT"), self.shop.inventory.on_hand("P-KIT")), (8, 12))

    def test_partial_shipment_commits_only_what_shipped(self):
        shipment = self.shop.ship(self.a.id, {"P-KIT": 2})
        self.assertEqual((shipment["weight_g"], shipment["tier_limit_g"], shipment["tier_price_cents"]), (5000, 5000, 1295))
        self.assertEqual(self.a.status, "partial")
        self.assertEqual((self.shop.inventory.reserved("P-KIT"), self.shop.inventory.on_hand("P-KIT")), (6, 10))

    def test_cancelling_after_a_partial_shipment_only_releases_the_rest(self):
        self.shop.ship(self.a.id, {"P-KIT": 2})
        self.shop.cancel(self.a.id)
        self.assertEqual(self.a.status, "cancelled")
        self.assertEqual(self.shop.inventory.reserved("P-KIT"), 4)      # order b still holds its four
        self.assertEqual(self.shop.inventory.available("P-KIT"), 6)

    def test_cancelling_a_fresh_order_releases_everything(self):
        self.shop.cancel(self.b.id)
        self.assertEqual(self.shop.inventory.reserved("P-KIT"), 4)
        self.assertEqual([o.id for o in self.shop.fulfilment.backlog()], [self.a.id])

    def test_second_partial_shipment_finishes_the_order(self):
        self.shop.ship(self.a.id, {"P-KIT": 3})
        self.shop.ship(self.a.id)
        self.assertEqual(self.a.status, "shipped")
        self.assertEqual([s["number"] for s in self.a.shipments], [1, 2])
        self.assertEqual(self.a.shipments[1]["weight_g"], 2500)


if __name__ == "__main__":
    unittest.main()

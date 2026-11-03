import io
import json
import os
import tempfile
import unittest

from shop import cli
from helpers import ShopCase


class ApiFlowTest(ShopCase):
    def test_place_ship_and_notify(self):
        order = self.shop.place_order("C1", [("P-MUG", 2)])
        self.assertEqual(order.status, "allocated")
        self.assertEqual(self.shop.loyalty.balance("C1"), 21)
        self.shop.ship(order.id)
        sent = self.shop.outbox.messages("order_shipped")[0]
        self.assertIn("expected 2026-03-05", sent["body"])
        self.assertEqual(self.shop.audit.count_by_kind(), {"placed": 1, "shipped": 1})

    def test_cancel_reverses_loyalty_and_releases_stock(self):
        order = self.shop.place_order("C2", [("P-TEE", 2)])
        self.assertGreater(self.shop.loyalty.balance("C2"), 0)
        self.shop.cancel(order.id)
        self.assertEqual(self.shop.loyalty.balance("C2"), 0)
        self.assertEqual(self.shop.inventory.snapshot()["P-TEE"], (40, 0))
        self.assertEqual(self.shop.snapshot()["orders"], {order.id: "cancelled"})

    def test_full_cycle_with_return(self):
        order = self.shop.place_order("C1", [("P-BOOK", 1), ("P-LAMP", 1), ("P-MUG", 1)], payments=None)
        self.shop.ship(order.id)
        record = self.shop.return_item(order.id, "P-LAMP", 1)
        self.assertEqual(record.refund_cents, 5982)
        self.assertEqual(self.shop.returns.total_refunded(), 5982)


class CliTest(unittest.TestCase):
    SPEC = {
        "products": [{"sku": "A", "name": "Alpha", "price_cents": 1000, "weight_g": 100}],
        "customers": [{"id": "C1", "name": "Ann", "email": "ann@example.org", "region": "NL"}],
        "stock": {"A": 5},
        "orders": [{"customer": "C1", "items": [["A", 2]]}],
    }

    def test_scenario_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.SPEC, fh)
            out = io.StringIO()
            self.assertEqual(cli.main([path], out=out), 0)
        text = out.getvalue()
        self.assertIn("orders    1", text)
        self.assertIn("total     29.15", text)
        self.assertIn("Alpha", text)


if __name__ == "__main__":
    unittest.main()

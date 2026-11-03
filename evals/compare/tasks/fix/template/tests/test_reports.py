import datetime
import json
import unittest

from shop import exports, forecast, reports
from shop.audit import AuditLog
from shop.search import search
from helpers import ShopCase


class ReportTest(ShopCase):
    def setUp(self):
        super().setUp()
        self.o1 = self.shop.place_order("C1", [("P-MUG", 2), ("P-TEE", 1)])
        self.o2 = self.shop.place_order("C2", [("P-MUG", 1), ("P-SOCK", 3)])
        self.o3 = self.shop.place_order("C1", [("P-SOCK", 10)])
        self.shop.cancel(self.o3.id)

    def test_sales_by_region_ignores_cancelled(self):
        sales = reports.sales_by_region(self.shop.orders.all())
        self.assertEqual(sales, {"DE": self.o2.total_cents, "NL": self.o1.total_cents})

    def test_top_products(self):
        self.assertEqual(reports.top_products(self.shop.orders.all(), 2), [("P-MUG", 3), ("P-SOCK", 3)])

    def test_revenue_summary_adds_up(self):
        s = reports.revenue_summary(self.shop.orders.all())
        self.assertEqual(s["orders"], 2)
        self.assertEqual(s["total"], s["subtotal"] - s["discounts"] + s["tax"] + s["shipping"])
        self.assertEqual(s["total"], self.o1.total_cents + self.o2.total_cents)
        self.assertIn("orders    2", reports.money_lines(s))

    def test_stock_report_and_backlog(self):
        text = reports.stock_report(self.shop.inventory, self.shop.catalog)
        self.assertIn("P-MUG", text)
        backlog = reports.backlog_report(self.shop.fulfilment)
        self.assertEqual(len(backlog.splitlines()), 4)
        self.assertIn("2026-03-04", backlog)

    def test_aging_buckets(self):
        today = datetime.date(2026, 3, 13)
        buckets = reports.receivable_aging(self.shop.invoices.all(), today)
        self.assertEqual(sum(buckets.values()), sum(i.total for i in self.shop.invoices.all()))
        self.assertEqual(buckets["0-5"], 0)
        self.assertEqual(buckets["11+"], 0)
        self.assertGreater(buckets["6-10"], 0)


class ExportTest(ShopCase):
    def test_invoice_csv_round_trip(self):
        order = self.shop.place_order("C1", [("P-MUG", 2), ("P-TEE", 1)])
        text = exports.invoices_csv(self.shop.invoices.all())
        rows = exports.read_invoice_csv(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["total"] for r in rows), order.total_cents)
        self.assertTrue(text.startswith("invoice,order,sku,qty"))

    def test_orders_json(self):
        order = self.shop.place_order("C1", [("P-MUG", 1)])
        data = json.loads(exports.orders_json([order]))
        self.assertEqual(data[0]["id"], order.id)
        self.assertEqual(data[0]["lines"][0]["sku"], "P-MUG")


class MiscTest(ShopCase):
    def test_search_ranking(self):
        self.assertEqual([p.sku for p in search(self.shop.catalog, "mug")], ["P-MUG"])
        self.assertEqual([p.sku for p in search(self.shop.catalog, "apparel")], ["P-SOCK", "P-TEE"])
        self.assertEqual(search(self.shop.catalog, ""), [])

    def test_forecast(self):
        self.shop.place_order("C1", [("P-SEAS", 4)])
        self.assertEqual(forecast.units_sold(self.shop.orders.all()), {"P-SEAS": 4})
        self.assertEqual(forecast.suggestions(self.shop.inventory, self.shop.orders.all(), days=2, cover_days=7), {"P-SEAS": 13})
        with self.assertRaises(ValueError):
            forecast.daily_rate(1, 0)

    def test_audit_log(self):
        log = AuditLog(self.shop.clock)
        log.record("placed", "ORD-1", "x")
        log.record("shipped", "ORD-1")
        log.record("placed", "ORD-2")
        self.assertEqual(log.count_by_kind(), {"placed": 2, "shipped": 1})
        self.assertEqual(log.last("placed")["subject"], "ORD-2")
        self.assertEqual(len(log.entries(subject="ORD-1")), 2)


if __name__ == "__main__":
    unittest.main()

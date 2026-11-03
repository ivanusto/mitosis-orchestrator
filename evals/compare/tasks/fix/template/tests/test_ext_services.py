import datetime
import unittest

from shop import analytics, dashboard, exports, importers, invoicing, loyalty, money, picking, reports, tax, textutil, tracking
from shop.catalog import VariantMatrix
from shop.clock import FixedClock
from shop.customers import segment, segments, suggested_tier
from shop.dunning import Dunning, level_for
from shop.errors import InvalidState, OutOfStock, ValidationError
from shop.fulfilment import WavePlanner, hold, release_hold
from shop.inventory import Inventory, LotInventory
from shop.ledger import Ledger
from shop.orders import reprice
from shop.subscriptions import Plan, SubscriptionService
from shop.suppliers import Supplier
from shop.warehouse import Warehouse
from helpers import MONDAY, ShopCase

DATE = datetime.date
D = datetime.datetime


class SubscriptionCase(unittest.TestCase):
    """Plans that started on the 31st: the interesting part is what the shorter months do."""

    def setUp(self):
        self.clock = FixedClock(MONDAY)
        self.ledger = Ledger()
        self.service = SubscriptionService(self.ledger, self.clock)
        self.service.add_plan(Plan("PRO", "Pro monthly", 1000, 1))
        self.service.add_plan(Plan("YEAR", "Pro yearly", 9600, 12))
        self.sub = self.service.subscribe("C1", "PRO", DATE(2026, 1, 31))


class SubscriptionTest(SubscriptionCase):
    def test_renewals_are_counted_from_the_start_date(self):
        dates = [self.service.renewal_date(self.sub, n) for n in (1, 2, 3, 4)]
        self.assertEqual(dates, [DATE(2026, 2, 28), DATE(2026, 3, 31), DATE(2026, 4, 30), DATE(2026, 5, 31)])
        self.assertEqual(self.service.next_renewal(self.sub), DATE(2026, 2, 28))

    def test_renewing_opens_invoices_due_fourteen_days_after_the_renewal_date(self):
        first = self.service.renew(self.sub.id)
        second = self.service.renew(self.sub.id)
        self.assertEqual((first, second), ("SIN-00001", "SIN-00002"))
        self.assertEqual(self.ledger.get(first).due, DATE(2026, 3, 14))
        self.assertEqual(self.ledger.get(second).due, DATE(2026, 4, 14))
        self.assertEqual(self.service.next_renewal(self.sub), DATE(2026, 4, 30))

    def test_yearly_plan_and_due_list(self):
        yearly = self.service.subscribe("C2", "YEAR", DATE(2028, 2, 29))
        self.assertEqual(self.service.renewal_date(yearly, 1), DATE(2029, 2, 28))
        self.assertEqual(self.service.renewal_date(yearly, 4), DATE(2032, 2, 29))
        self.assertEqual(self.service.due_for_renewal(DATE(2026, 3, 1)), [self.sub.id])
        self.assertEqual(self.service.monthly_recurring_cents(), 1000 + 800)

    def test_cancelling_credits_the_unused_days(self):
        self.assertEqual(self.service.cancel(self.sub.id, DATE(2026, 2, 14)), 500)
        with self.assertRaises(InvalidState):
            self.service.renew(self.sub.id)
        with self.assertRaises(InvalidState):
            self.service.cancel(self.sub.id, DATE(2026, 2, 20))

    def test_payments_settle_the_oldest_invoice_first(self):
        self.service.renew(self.sub.id)
        self.service.renew(self.sub.id)
        self.assertEqual(self.service.pay("C1", 300), [("SIN-00001", 300)])
        self.assertEqual(self.service.pay("C1", 1200), [("SIN-00001", 700), ("SIN-00002", 500)])
        self.assertEqual((self.ledger.get("SIN-00001").balance, self.ledger.get("SIN-00002").balance), (0, 500))
        self.assertEqual(self.ledger.balance("C1"), 500)

    def test_overpayment_becomes_credit_and_is_used_on_the_next_invoice(self):
        self.service.renew(self.sub.id)
        self.assertEqual(self.service.pay("C1", 1400), [("SIN-00001", 1000)])
        self.assertEqual(self.ledger.credit("C1"), 400)
        self.service.renew(self.sub.id)
        self.assertEqual(self.service.apply_credit("C1"), [("SIN-00002", 400)])
        self.assertEqual(self.ledger.balance("C1"), 600)

    def test_ageing_and_statement(self):
        self.service.renew(self.sub.id)
        self.service.renew(self.sub.id)
        aging = self.ledger.aging(DATE(2026, 4, 20))
        self.assertEqual(aging, {"current": 0, "1-30": 1000, "31-60": 1000, "61+": 0})
        self.service.pay("C1", 250)
        statement = self.ledger.statement("C1")
        self.assertEqual(statement["balance"], 1750)
        self.assertEqual(statement["rows"][0], ("SIN-00001", "2026-03-14", 1000, 250, 750))


class DunningTest(SubscriptionCase):
    def test_levels_follow_business_days_overdue(self):
        self.assertEqual(level_for(4), (None, 0))
        self.assertEqual(level_for(5), ("first", 0))
        self.assertEqual(level_for(14), ("second", 500))
        self.assertEqual(level_for(31), ("final", 1500))

    def test_reminders_fees_and_interest(self):
        self.service.renew(self.sub.id)
        self.service.renew(self.sub.id)
        dunning = Dunning(self.ledger)
        today = DATE(2026, 4, 20)
        self.assertEqual(dunning.reminders("C1", today), [("SIN-00001", "second", 500)])
        self.assertEqual(dunning.fees_total("C1", today), 500)
        self.assertEqual(dunning.interest("C1", today), 8 + 1)  # 37 days on the first invoice, 6 on the second
        self.assertEqual(dunning.worklist(["C1", "C9"], today), [("C1", "SIN-00001", "second", 500)])


class ShopWiringTest(ShopCase):
    def test_subscriptions_through_the_facade(self):
        from shop.subscriptions import Plan as P
        self.shop.subscriptions.add_plan(P("PRO", "Pro", 1000, 1))
        self.shop.subscribe("C1", "PRO", DATE(2026, 1, 31))
        self.assertEqual(self.shop.bill_due_subscriptions(DATE(2026, 3, 1)), ["SIN-00001"])
        self.assertEqual(self.shop.bill_due_subscriptions(DATE(2026, 3, 31)), ["SIN-00002"])
        self.assertEqual(self.shop.receive_payment("C1", 1500, "card"), [("SIN-00001", 1000), ("SIN-00002", 500)])
        self.assertEqual(self.shop.events.history("subscribed")[0][1]["customer"], "C1")
        self.assertEqual(self.shop.collections(["C1"], DATE(2026, 6, 30)), [("C1", "SIN-00002", "final", 1500)])


class PurchasingTest(unittest.TestCase):
    def setUp(self):
        from shop.suppliers import SupplierBook
        from shop.purchasing import Purchasing
        self.clock = FixedClock(MONDAY)
        self.inventory = Inventory()
        self.book = SupplierBook()
        self.book.add(Supplier("S1", "Alpha", 5, 10, ("P-MUG", "P-TEE"), {"P-MUG": 400, "P-TEE": 900}))
        self.purchasing = Purchasing(self.book, self.inventory, self.clock)

    def test_order_receipt_and_close(self):
        po = self.purchasing.create("S1", [("P-MUG", 8), ("P-TEE", 4)])
        self.assertEqual(po.expected_on, DATE(2026, 3, 9))
        self.assertEqual(self.purchasing.value_cents(po.id), 8 * 400 + 4 * 900)
        self.assertEqual(self.purchasing.receive(po.id, "P-MUG", 5), "open")
        self.assertEqual(self.inventory.on_hand("P-MUG"), 5)
        self.assertEqual(self.purchasing.open_value_cents(po.id), 3 * 400 + 4 * 900)
        self.purchasing.receive(po.id, "P-MUG", 3)
        self.assertEqual(self.purchasing.receive(po.id, "P-TEE", 4), "closed")
        with self.assertRaises(InvalidState):
            self.purchasing.receive(po.id, "P-TEE", 1)
        self.assertEqual(self.purchasing.open_orders(), [])

    def test_rules(self):
        with self.assertRaises(InvalidState):
            self.purchasing.create("S1", [("P-MUG", 2)])
        with self.assertRaises(InvalidState):
            self.purchasing.create("S1", [("P-KIT", 20)])
        po = self.purchasing.create("S1", [("P-MUG", 10)])
        with self.assertRaises(InvalidState):
            self.purchasing.receive(po.id, "P-MUG", 11)
        self.assertEqual(self.purchasing.late(DATE(2026, 3, 10)), [po.id])
        self.assertEqual(self.purchasing.late(DATE(2026, 3, 9)), [])
        self.assertEqual(self.purchasing.reorder_plan({"P-MUG": 4, "P-KIT": 1}), [("S1", "P-MUG", 10)])


class PickingAndTrackingTest(ShopCase):
    def setUp(self):
        super().setUp()
        self.wh = Warehouse()
        for code in ("A-02-1", "A-03-1", "B-01-1"):
            self.wh.add_bin(code, 50)
        self.wh.place("P-MUG", "A-03-1", 5)
        self.wh.place("P-MUG", "A-02-1", 2)
        self.wh.place("P-TEE", "B-01-1", 9)

    def test_pick_list_follows_the_route(self):
        order = self.shop.orders.create("C1", [("P-MUG", 6), ("P-TEE", 1)])
        self.assertEqual(picking.pick_lines(self.wh, order), [("A-02-1", "P-MUG", 2), ("A-03-1", "P-MUG", 4), ("B-01-1", "P-TEE", 1)])
        big = self.shop.orders.create("C1", [("P-MUG", 10)])
        with self.assertRaises(OutOfStock):
            picking.pick_lines(self.wh, big)

    def test_batches_and_distance(self):
        a = self.shop.orders.create("C1", [("P-MUG", 3)])
        b = self.shop.orders.create("C2", [("P-MUG", 1), ("P-TEE", 2)])
        merged = picking.batch(self.wh, [a, b])
        self.assertEqual([(c, s, q) for c, s, q, _ in merged], [("A-02-1", "P-MUG", 3), ("A-03-1", "P-MUG", 1), ("B-01-1", "P-TEE", 2)])
        self.assertEqual(merged[0][3], [a.id, b.id])
        self.assertEqual(picking.walking_distance(["A-02-1", "A-03-1", "B-01-1"]), 1 + 10 + 2)

    def test_tracking(self):
        clock = FixedClock(MONDAY)
        t = tracking.Tracker(clock)
        t.record("T1", "label_created")
        clock.advance(hours=3)
        t.record("T1", "picked_up")
        clock.advance(hours=30)
        t.record("T1", "delivered")
        self.assertEqual((t.status("T1"), t.transit_hours("T1"), t.progress_bps("T1")), ("delivered", 30, 10000))
        with self.assertRaises(InvalidState):
            t.record("T1", "in_transit")
        t.record("T2", "picked_up")
        clock.advance(hours=50)
        self.assertEqual(t.stuck(48), ["T2"])
        self.assertEqual(t.progress_bps("T2"), 2500)
        with self.assertRaises(InvalidState):
            t.record("T2", "teleported")


class InventoryLotsTest(unittest.TestCase):
    def test_first_expiry_first_out(self):
        lots = LotInventory()
        lots.receive("P-MUG", 5, "L2", DATE(2026, 9, 1))
        lots.receive("P-MUG", 5, "L1", DATE(2026, 6, 1))
        lots.receive("P-MUG", 5, "L0")
        self.assertEqual([l for l, _, _ in lots.lots("P-MUG")], ["L1", "L2", "L0"])
        self.assertEqual(lots.take("P-MUG", 7), [("L1", 5), ("L2", 2)])
        self.assertEqual(lots.on_hand("P-MUG"), 8)
        with self.assertRaises(OutOfStock):
            lots.take("P-MUG", 20)
        self.assertEqual(lots.on_hand("P-MUG"), 8)

    def test_expired_lots_are_skipped_and_written_off(self):
        lots = LotInventory()
        lots.receive("P-MUG", 4, "OLD", DATE(2026, 1, 1))
        lots.receive("P-MUG", 4, "NEW", DATE(2026, 12, 1))
        self.assertEqual(lots.take("P-MUG", 3, today=DATE(2026, 3, 2)), [("NEW", 3)])
        self.assertEqual(lots.expired(DATE(2026, 3, 2)), [("P-MUG", "OLD", 4)])
        self.assertEqual(lots.write_off(DATE(2026, 3, 2)), 4)
        self.assertEqual(lots.on_hand("P-MUG"), 1)


class CatalogAndCustomerExtrasTest(ShopCase):
    def test_variants(self):
        base = self.shop.catalog.get("P-TEE")
        matrix = VariantMatrix(base)
        matrix.add_option("colour", ["red", "blue"])
        matrix.add_option("size", ["S", "XL"])
        self.assertEqual(matrix.register(self.shop.catalog, {"XL": 200}), 4)
        self.assertEqual(self.shop.catalog.get("P-TEE-BLU-XL").price_cents, 2699)
        self.assertEqual(self.shop.catalog.get("P-TEE-RED-S").price_cents, 2499)
        with self.assertRaises(ValueError):
            matrix.add_option("empty", [])

    def test_segments(self):
        self.assertEqual(segment(0, 0), "prospect")
        self.assertEqual(segment(500, 1), "new")
        self.assertEqual(segment(500, 3), "regular")
        self.assertEqual(segment(100000, 1), "vip")
        self.assertEqual(suggested_tier(120000), "silver")
        self.shop.place_order("C1", [("P-MUG", 1)])
        self.assertEqual(segments(self.shop.orders.all()), {"C1": "new"})


class FulfilmentExtrasTest(ShopCase):
    def test_hold_and_release(self):
        order = self.shop.place_order("C1", [("P-MUG", 1)])
        hold(self.shop.fulfilment, order.id, "address check")
        self.assertEqual(order.status, "held")
        self.assertEqual(self.shop.fulfilment.backlog(), [])
        release_hold(self.shop.fulfilment, order.id)
        self.assertEqual(order.status, "allocated")
        with self.assertRaises(InvalidState):
            release_hold(self.shop.fulfilment, order.id)

    def test_wave_plan(self):
        a = self.shop.place_order("C1", [("P-MUG", 1)])
        b = self.shop.place_order("C3", [("P-LAMP", 2)])
        c = self.shop.place_order("C5", [("P-TEE", 1)])
        waves = WavePlanner(self.shop.fulfilment, max_orders_per_wave=1).plan()
        self.assertEqual([(w["zone"], w["orders"]) for w in waves], [("US", [b.id]), ("EU", [a.id]), ("EU", [c.id])])
        self.assertEqual(WavePlanner(self.shop.fulfilment).urgent(DATE(2026, 3, 3)), [])
        self.assertEqual(WavePlanner(self.shop.fulfilment).urgent(DATE(2026, 3, 4)), [a.id, b.id, c.id])


class MoneyFreeReportExtrasTest(ShopCase):
    def setUp(self):
        super().setUp()
        self.o1 = self.shop.place_order("C1", [("P-MUG", 2), ("P-TEE", 1)], "FIVE")
        self.o2 = self.shop.place_order("C2", [("P-BOOK", 1)])
        self.orders = self.shop.orders.all()

    def test_category_sales_ranking_and_rates(self):
        cats = reports.category_sales(self.orders, self.shop.catalog)
        self.assertEqual(sorted(cats), ["apparel", "books", "home"])
        self.assertEqual(sum(cats.values()), sum(o.subtotal_cents - o.discount_cents for o in self.orders))
        self.assertEqual(reports.customer_ranking(self.orders, 1)[0][0], "C1")
        self.assertEqual(reports.shipping_income(self.orders), self.o1.shipping_cents + self.o2.shipping_cents)
        self.assertEqual(reports.discount_rate_bps(self.orders), money.round_div(500 * 10000, sum(o.subtotal_cents for o in self.orders)))

    def test_exports_and_documents(self):
        ledger = Ledger()
        ledger.open_invoice("X1", "C1", 1000, DATE(2026, 3, 20))
        ledger.apply_payment("C1", 250)
        text = exports.ledger_csv(ledger, ["C1", "C2"])
        self.assertEqual(text.splitlines(), ["customer,invoice,due,amount,paid,balance", "C1,X1,2026-03-20,10.00,2.50,7.50"])
        invoice = self.shop.invoices.for_order(self.o1.id)
        note = invoicing.credit_note(invoice, "P-MUG", 1)
        self.assertEqual(note.amount, -1050)
        self.assertEqual(note.qty, 1)
        self.assertEqual(note.tax, -money.round_div(invoice.lines[0].tax, 2))
        self.assertEqual(invoicing.summary_line(invoice), "%s %s %s" % (invoice.number, self.o1.id, money.format_cents(invoice.total)))
        schedule = invoicing.payment_schedule(invoice, 3, DATE(2026, 3, 15))
        self.assertEqual([d for d, _ in schedule], [DATE(2026, 3, 15), DATE(2026, 4, 15), DATE(2026, 5, 15)])
        self.assertEqual(sum(c for _, c in schedule), invoice.total)

    def test_dashboard_importers_and_text(self):
        ledger = Ledger()
        ledger.open_invoice("X1", "C1", 1000, DATE(2026, 3, 1))
        text = dashboard.render(self.shop.inventory, self.shop.catalog, ledger, self.shop.fulfilment, DATE(2026, 3, 20), threshold=19)
        self.assertIn("LOW STOCK", text)
        self.assertIn("P-BOOK", text)
        self.assertIn("1-30     10.00", text)
        self.assertIn(self.o1.id + " allocated", text)
        csv_text = "sku,name,price,weight_g,category,taxable\nN-1,New thing,12.50,100,home,no\nP-MUG,Dup,1.00,1,home,yes\n"
        added, skipped = importers.import_products(self.shop.catalog, csv_text)
        self.assertEqual((added, skipped), (["N-1"], ["P-MUG"]))
        self.assertFalse(self.shop.catalog.get("N-1").taxable)
        self.assertEqual(importers.import_stock(self.shop.inventory, "sku,qty\nN-1,4\nN-1,6\n"), 10)
        with self.assertRaises(ValidationError):
            importers.parse_products("sku,name\nA,B\n")
        with self.assertRaises(ValidationError):
            importers.parse_products("sku,name,price,weight_g\nA,B,1.00,x\n")
        self.assertEqual(textutil.wrap_words("one two three four", 9), ["one two", "three", "four"])
        self.assertEqual([textutil.ordinal(n) for n in (1, 2, 3, 4, 11, 22)], ["1st", "2nd", "3rd", "4th", "11th", "22nd"])

    def test_reprice_tax_and_loyalty_extras(self):
        fresh = self.shop.orders.create("C1", [("P-MUG", 1)], "FIVE")
        reprice(self.shop.orders, fresh.id, [("P-TEE", 2)])
        self.assertEqual([l.sku for l in fresh.lines], ["P-TEE"])
        self.assertEqual(fresh.coupon_code, "FIVE")
        with self.assertRaises(InvalidState):
            reprice(self.shop.orders, self.o1.id, [("P-MUG", 1)])
        summary = tax.summarize_collected([("NL", 10000, 2100), ("NL", 5000, 1050), ("DE", 1000, 190)])
        self.assertEqual(summary["NL"], (15000, 3150, 2100))
        self.assertEqual(tax.expected_vs_charged([("NL", 10000, 2000), ("DE", 1000, 190)]), {"DE": 0, "NL": 100})
        self.assertEqual(loyalty.expiring_points([(10, 50), (400, 30), (500, -5)], 450), 50)
        self.assertEqual(loyalty.tier_progress_bps(25000, 100000), 2500)

    def test_analytics_revenue_in_another_currency(self):
        usd = analytics.revenue_in({"EUR": [self.o1], "USD": [self.o2]}, "EUR")
        self.assertEqual(usd, self.o1.total_cents + analytics.currency.convert(self.o2.total_cents, "USD", "EUR"))


if __name__ == "__main__":
    unittest.main()

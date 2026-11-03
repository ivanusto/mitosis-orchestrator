"""Hidden regression tests; they run only at grading, against the package in the working directory."""

import datetime
import itertools
import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "template", "tests"))

from helpers import ShopCase, make_shop  # noqa: E402
from shop import calendar, currency, gift_cards, invoicing, money, pricelists, shipping  # noqa: E402
from shop.clock import FixedClock  # noqa: E402
from shop.ledger import Ledger  # noqa: E402
from shop.subscriptions import Plan, SubscriptionService  # noqa: E402
from shop.errors import OverweightError  # noqa: E402


class AllocateProperties(unittest.TestCase):
    def test_largest_remainder(self):
        self.assertEqual(money.allocate(10, [3, 3, 1]), [4, 4, 2])
        self.assertEqual(money.allocate(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(money.allocate(5, [1, 1]), [3, 2])
        self.assertEqual(money.allocate(7, [0, 3, 4]), [0, 3, 4])

    def test_negative_totals_mirror_positive_ones(self):
        self.assertEqual(money.allocate(-10, [3, 3, 1]), [-4, -4, -2])
        self.assertEqual(money.allocate(-5982, [2500, 7071]), [-1563, -4419])
        for total, weights in ((1001, [5, 3, 2]), (777, [1000, 7128]), (95, [1, 2, 3, 4])):
            self.assertEqual(money.allocate(-total, weights), [-s for s in money.allocate(total, weights)])

    def test_exactness_over_many_inputs(self):
        for total in range(-60, 61, 3):
            for weights in ([1, 2, 3], [5, 5, 1], [9, 1], [2, 7, 7, 4]):
                shares = money.allocate(total, weights)
                self.assertEqual(sum(shares), total)
                exact = [Fraction(total * w, sum(weights)) for w in weights]
                for share, ex in zip(shares, exact):
                    self.assertLess(abs(share - ex), 1)
                # a part with a strictly bigger remainder never gets less than one with a smaller remainder
                mag = abs(total)
                rem = [(mag * w) % sum(weights) for w in weights]
                base = [mag * w // sum(weights) for w in weights]
                for i, j in itertools.permutations(range(len(weights)), 2):
                    if rem[i] > rem[j]:
                        self.assertGreaterEqual(abs(shares[i]) - base[i], abs(shares[j]) - base[j])


class TierBoundaries(unittest.TestCase):
    def test_exact_limits_belong_to_the_lighter_tier(self):
        self.assertEqual(shipping.tier_for("EU", 0), (500, 495))
        self.assertEqual(shipping.tier_for("EU", 500), (500, 495))
        self.assertEqual(shipping.tier_for("EU", 501), (2000, 795))
        self.assertEqual(shipping.tier_for("EU", 2000), (2000, 795))
        self.assertEqual(shipping.tier_for("EU", 5000), (5000, 1295))
        self.assertEqual(shipping.tier_for("EU", 10000), (10000, 1995))
        self.assertEqual(shipping.tier_for("US", 5000), (5000, 1895))
        self.assertEqual(shipping.tier_for("UK", 10000), (10000, 2295))
        with self.assertRaises(OverweightError):
            shipping.tier_for("UK", 10001)

    def test_quotes_at_the_limits(self):
        self.assertEqual(shipping.quote("EU", 500, 100), 495)
        self.assertEqual(shipping.quote("US", 2000, 100), 1195)
        self.assertEqual(shipping.quote("UK", 10000, 100), 2295)


DATE = datetime.date


class MonthArithmetic(unittest.TestCase):
    def test_add_months_clamps_to_the_last_day(self):
        self.assertEqual(calendar.add_months(DATE(2026, 1, 31), 1), DATE(2026, 2, 28))
        self.assertEqual(calendar.add_months(DATE(2028, 1, 31), 1), DATE(2028, 2, 29))
        self.assertEqual(calendar.add_months(DATE(2026, 3, 31), -1), DATE(2026, 2, 28))
        self.assertEqual(calendar.add_months(DATE(2026, 8, 31), 1), DATE(2026, 9, 30))
        self.assertEqual(calendar.add_months(DATE(2026, 1, 31), 13), DATE(2027, 2, 28))
        self.assertEqual(calendar.add_months(DATE(2026, 12, 31), 2), DATE(2027, 2, 28))
        self.assertEqual(calendar.add_months(DATE(2026, 5, 15), 0), DATE(2026, 5, 15))

    def test_gift_card_issued_on_a_leap_day(self):
        book = gift_cards.GiftCardBook(FixedClock(datetime.datetime(2028, 2, 29, 9, 0)))
        self.assertEqual(book.issue("G", 1000), DATE(2029, 2, 28))

    def test_subscription_anchored_on_the_thirtieth_and_thirty_first(self):
        service = SubscriptionService(Ledger(), FixedClock(datetime.datetime(2026, 3, 2, 9, 0)))
        service.add_plan(Plan("M", "Monthly", 1000, 1))
        service.add_plan(Plan("Q", "Quarterly", 2500, 3))
        autumn = service.subscribe("C1", "M", DATE(2026, 10, 31))
        self.assertEqual([service.renewal_date(autumn, n) for n in (1, 2, 3, 4)], [DATE(2026, 11, 30), DATE(2026, 12, 31), DATE(2027, 1, 31), DATE(2027, 2, 28)])
        quarterly = service.subscribe("C1", "Q", DATE(2026, 11, 30))
        self.assertEqual([service.renewal_date(quarterly, n) for n in (1, 2)], [DATE(2027, 2, 28), DATE(2027, 5, 30)])


class ReceivablesRules(unittest.TestCase):
    def ledger(self):
        ledger = Ledger()
        ledger.open_invoice("I1", "C1", 1000, DATE(2026, 3, 10))
        ledger.open_invoice("I2", "C1", 800, DATE(2026, 4, 10))
        return ledger

    def test_a_payment_never_takes_more_than_the_balance(self):
        ledger = self.ledger()
        self.assertEqual(ledger.apply_payment("C1", 300), [("I1", 300)])
        self.assertEqual(ledger.apply_payment("C1", 900), [("I1", 700), ("I2", 200)])
        self.assertEqual((ledger.get("I1").balance, ledger.get("I2").balance, ledger.credit("C1")), (0, 600, 0))

    def test_leftover_becomes_credit_and_ties_go_to_the_lower_number(self):
        ledger = Ledger()
        ledger.open_invoice("B2", "C1", 500, DATE(2026, 3, 10))
        ledger.open_invoice("A9", "C1", 500, DATE(2026, 3, 10))
        self.assertEqual(ledger.apply_payment("C1", 700), [("A9", 500), ("B2", 200)])
        self.assertEqual(ledger.apply_payment("C1", 1000), [("B2", 300)])
        self.assertEqual(ledger.credit("C1"), 700)

    def test_stored_credit_respects_balances(self):
        ledger = self.ledger()
        ledger.apply_payment("C1", 2500)
        self.assertEqual(ledger.credit("C1"), 700)
        ledger.open_invoice("I3", "C1", 300, DATE(2026, 5, 10))
        ledger.open_invoice("I4", "C1", 900, DATE(2026, 6, 10))
        self.assertEqual(ledger.use_credit("C1"), [("I3", 300), ("I4", 400)])
        self.assertEqual((ledger.balance("C1"), ledger.credit("C1")), (500, 0))


class CurrencyRounding(unittest.TestCase):
    def test_convert_rounds_to_the_nearest_cent_away_from_zero(self):
        self.assertEqual(currency.convert(999, "EUR", "USD"), 1084)
        self.assertEqual(currency.convert(-999, "EUR", "USD"), -1084)
        self.assertEqual(currency.convert(1, "EUR", "SEK"), 11)
        self.assertEqual(currency.convert(4, "EUR", "SEK"), 45)
        self.assertEqual(currency.convert(500, "GBP", "USD"), 634)
        self.assertEqual(currency.convert(1085, "USD", "EUR"), 1000)

    def test_totals_and_price_lists_in_other_currencies(self):
        self.assertEqual(currency.total_in({"USD": 999, "GBP": 999}, "EUR"), 921 + 1167)
        shop = make_shop()
        sek = pricelists.PriceList(shop.catalog, "SEK")
        self.assertEqual((sek.price("P-SOCK"), sek.price("P-BOOK")), (3740, 22449))


class ThroughTheServices(ShopCase):
    def test_order_of_exactly_two_kilos(self):
        order = self.shop.orders.create("C1", [("P-LAMP", 1), ("P-SEAS", 1)])
        self.assertEqual(order.weight_g, 2000)
        self.assertEqual(order.shipping_cents, 795)
        light = self.shop.orders.create("C1", [("P-MUG", 1), ("P-TEE", 1)])
        self.assertEqual(light.weight_g, 500)
        self.assertEqual(light.shipping_cents, 495)

    def test_coupon_spread_other_baskets(self):
        order = self.shop.orders.create("C1", [("P-MUG", 1), ("P-SOCK", 1), ("P-TEE", 1)], "FIVE")
        self.assertEqual([l.discount_cents for l in order.lines], [135, 43, 322])
        self.assertEqual(sum(l.discount_cents for l in order.lines), 500)
        order2 = self.shop.orders.create("C4", [("P-LAMP", 2), ("P-BOOK", 3), ("P-SOCK", 4)], "TENPCT")
        self.assertEqual(order2.discount_cents, sum(l.discount_cents for l in order2.lines))

    def test_invoice_shares_other_baskets(self):
        for cust, items in (("C4", [("P-BOOK", 1), ("P-LAMP", 1), ("P-MUG", 1)]), ("C1", [("P-MUG", 1), ("P-SOCK", 1), ("P-TEE", 1)]),
                            ("C4", [("P-LAMP", 1), ("P-SOCK", 1), ("P-TEE", 1)])):
            order = self.shop.orders.create(cust, items)
            invoice = self.shop.invoices.build(order.id)
            nets = [l.net_cents for l in order.lines]
            self.assertEqual([l.shipping for l in invoice.lines], money.allocate(order.shipping_cents, nets))
            self.assertEqual(invoice.total, order.total_cents)
        order = self.shop.orders.create("C4", [("P-BOOK", 1), ("P-LAMP", 1), ("P-MUG", 1)])
        self.assertEqual([l.shipping for l in self.shop.invoices.build(order.id).lines], [235, 536, 124])

    def test_installments_and_refund_splits_elsewhere(self):
        self.assertEqual(invoicing.installments(-1001, 4), [-251, -250, -250, -250])
        shop = make_shop()
        order = shop.orders.create("C4", [("P-BOOK", 2), ("P-LAMP", 1), ("P-MUG", 2)])
        shop.orders.mark_paid(order.id, [("card", 2500), ("gift", 1000), ("card2", order.total_cents - 3500)])
        shop.fulfilment.reserve(order.id)
        shop.invoices.build(order.id)
        shop.ship(order.id)
        record = shop.return_item(order.id, "P-MUG", 2)
        self.assertEqual(sum(c for _, c in record.refunds), -record.refund_cents)
        self.assertEqual([c for _, c in record.refunds], money.allocate(-record.refund_cents, [2500, 1000, order.total_cents - 3500]))

    def test_cancel_after_partial_shipment_of_light_orders(self):
        a = self.shop.place_order("C1", [("P-MUG", 3)])
        b = self.shop.place_order("C5", [("P-MUG", 4)])
        self.shop.ship(a.id, {"P-MUG": 1})
        self.shop.cancel(a.id)
        self.assertEqual(self.shop.inventory.reserved("P-MUG"), 4)
        self.assertEqual(self.shop.inventory.on_hand("P-MUG"), 29)
        self.shop.cancel(b.id)
        self.assertEqual(self.shop.inventory.snapshot()["P-MUG"], (29, 0))

    def test_cancel_directly_on_the_service(self):
        a = self.shop.place_order("C1", [("P-TEE", 5)])
        b = self.shop.place_order("C2", [("P-TEE", 5)])
        self.shop.fulfilment.ship(a.id, {"P-TEE": 2})
        self.shop.fulfilment.cancel(a.id)
        self.assertEqual(self.shop.inventory.reserved("P-TEE"), 5)
        self.assertEqual(b.status, "allocated")

    def test_heavy_cancel_cycle(self):
        a = self.shop.place_order("C1", [("P-KIT", 4)])
        self.shop.ship(a.id, {"P-KIT": 1})
        self.shop.ship(a.id, {"P-KIT": 1})
        self.shop.cancel(a.id)
        self.assertEqual(self.shop.inventory.snapshot()["P-KIT"], (10, 0))
        self.assertEqual(self.shop.snapshot()["orders"][a.id], "cancelled")

    def test_dates_still_work(self):
        order = self.shop.place_order("C1", [("P-MUG", 1)])
        self.shop.ship(order.id)
        self.assertEqual(self.shop.returns.deadline(order.id), datetime.date(2026, 3, 16))


if __name__ == "__main__":
    unittest.main()

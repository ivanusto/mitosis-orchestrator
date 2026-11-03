import datetime
import unittest

from shop import analytics, bundles, carriers, discounts, gift_cards, pricelists, promotions, recommendations, returns_policy, shipping, sla
from shop.clock import FixedClock
from shop.discounts import Coupon
from shop.errors import InvalidState, NotFound, ValidationError
from shop.tax_rules import TaxProfile, TaxRules
from helpers import MONDAY, ShopCase

D = datetime.datetime
DATE = datetime.date


class PriceListTest(ShopCase):
    def test_usd_list_rounds_to_the_nearest_cent(self):
        usd = pricelists.PriceList(self.shop.catalog, "USD")
        self.assertEqual({s: usd.price(s) for s in ("P-BOOK", "P-KIT", "P-LAMP", "P-MUG", "P-SOCK")},
                         {"P-BOOK": 2169, "P-KIT": 2170, "P-LAMP": 4937, "P-MUG": 1139, "P-SOCK": 361})
        self.assertEqual(usd.basket_total([("P-LAMP", 2), ("P-BOOK", 1)]), 12043)

    def test_markup_and_back_to_euro(self):
        gbp = pricelists.PriceList(self.shop.catalog, "GBP", markup_bps=250)
        self.assertEqual({s: gbp.price(s) for s in ("P-BOOK", "P-LAMP", "P-MUG")}, {"P-BOOK": 1754, "P-LAMP": 3992, "P-MUG": 921})
        self.assertEqual(pricelists.PriceList(self.shop.catalog, "USD").in_euro(4937), 4550)

    def test_book_of_lists(self):
        book = pricelists.PriceListBook(self.shop.catalog)
        book.add("USD")
        self.assertEqual(book.get("EUR").price("P-MUG"), 1050)
        self.assertEqual(book.codes(), ["EUR", "USD"])
        with self.assertRaises(ValidationError):
            book.get("NOK")
        with self.assertRaises(ValidationError):
            book.get("USD").line("P-MUG", 0)


class TaxRulesTest(unittest.TestCase):
    def test_treatments(self):
        rules = TaxRules()
        rules.set_profile(TaxProfile("B1", business_vat_id="DE123"))
        rules.set_profile(TaxProfile("X1", exempt=True))
        rules.set_profile(TaxProfile("M1", region_override="DE"))
        self.assertEqual(rules.tax_for_line("C1", 10000, "NL"), 2100)
        self.assertEqual(rules.tax_for_line("X1", 10000, "NL"), 0)
        self.assertEqual(rules.tax_for_line("C1", 10000, "NL", category="books"), 0)
        self.assertEqual(rules.tax_for_line("M1", 10000, "NL"), 1900)
        self.assertEqual(rules.tax_for_line("B1", 10000, "DE", seller_region="NL"), 0)
        self.assertEqual(rules.tax_for_line("B1", 10000, "NL", seller_region="NL"), 2100)
        self.assertEqual(rules.exempt_customers(), ["X1"])


class CarrierTest(unittest.TestCase):
    def test_cutoffs_and_estimates(self):
        std = carriers.find("PostNL", "standard")
        self.assertEqual(carriers.delivery_estimate(std, D(2026, 3, 2, 15, 0)), DATE(2026, 3, 5))
        self.assertEqual(carriers.delivery_estimate(std, D(2026, 3, 2, 17, 0)), DATE(2026, 3, 6))
        self.assertEqual(carriers.delivery_estimate(carriers.find("DHL", "express"), D(2026, 3, 6, 14, 0)), DATE(2026, 3, 10))
        self.assertEqual(carriers.handover_date(std, D(2026, 3, 7, 9, 0)), DATE(2026, 3, 9))

    def test_choice_by_deadline(self):
        pick = carriers.cheapest_by("EU", DATE(2026, 3, 5), D(2026, 3, 2, 10, 0))
        self.assertEqual((pick.carrier, pick.level), ("PostNL", "standard"))
        rush = carriers.cheapest_by("EU", DATE(2026, 3, 3), D(2026, 3, 2, 10, 0))
        self.assertEqual((rush.carrier, rush.level), ("PostNL", "express"))
        self.assertIsNone(carriers.cheapest_by("US", DATE(2026, 3, 3), D(2026, 3, 2, 10, 0)))
        self.assertEqual([s.carrier for s in carriers.services_for("US")], ["DHL", "UPS"])
        with self.assertRaises(ValidationError):
            carriers.find("FedEx", "standard")


class SlaTest(unittest.TestCase):
    def test_promise_and_compensation(self):
        self.assertEqual(sla.promised_date("EU", DATE(2026, 3, 2)), DATE(2026, 3, 9))
        result = sla.evaluate("EU", DATE(2026, 3, 2), DATE(2026, 3, 12))
        self.assertEqual((result["late_days"], result["compensation_bps"]), (3, 1000))
        self.assertEqual(sla.evaluate("EU", DATE(2026, 3, 2), DATE(2026, 3, 9))["compensation_bps"], 0)
        self.assertEqual(sla.compensation_bps(9), 2000)
        summary = sla.summarize([{"late_days": 0}, {"late_days": 0}, {"late_days": 4}])
        self.assertEqual((summary["on_time_bps"], summary["worst"]), (6666, 4))


class BundleTest(ShopCase):
    def setUp(self):
        super().setUp()
        self.book = bundles.BundleBook(self.shop.catalog)
        self.book.add(bundles.Bundle("HOME", "Home set", (("P-MUG", 2), ("P-LAMP", 1)), 1000))

    def test_prices(self):
        self.assertEqual((self.book.list_price("HOME"), self.book.price("HOME")), (6650, 5985))
        self.assertEqual(self.book.component_shares("HOME"), [("P-MUG", 1890), ("P-LAMP", 4095)])

    def test_applicable_sets(self):
        self.assertEqual(self.book.applicable({"P-MUG": 5, "P-LAMP": 1}), [("HOME", 1)])
        self.assertEqual(self.book.applicable({"P-MUG": 1, "P-LAMP": 3}), [])
        with self.assertRaises(NotFound):
            self.book.add(bundles.Bundle("BAD", "Bad", (("P-NOPE", 1),), 0))


class GiftCardTest(unittest.TestCase):
    def test_lifecycle(self):
        clock = FixedClock(MONDAY)
        book = gift_cards.GiftCardBook(clock)
        self.assertEqual(book.issue("G1", 5000), DATE(2027, 3, 2))
        self.assertEqual(book.redeem("G1", 1200), 1200)
        self.assertEqual(book.balance("G1"), 3800)
        with self.assertRaises(InvalidState):
            book.redeem("G1", 4000)
        self.assertEqual(book.liability(), 3800)
        clock.set(D(2027, 3, 3, 9, 0))
        self.assertEqual(book.balance("G1"), 0)
        self.assertEqual(book.liability(), 0)
        self.assertEqual(book.expiring_before(DATE(2027, 12, 31)), ["G1"])
        self.assertEqual(book.top_up("G1", 100), DATE(2028, 3, 3))
        with self.assertRaises(InvalidState):
            book.issue("G1", 1)


class ReturnPolicyTest(unittest.TestCase):
    def test_fees(self):
        self.assertEqual(returns_policy.restocking_fee("changed_mind", 5982), 598)
        self.assertEqual(returns_policy.net_refund("changed_mind", 5000, 695), 3805)
        self.assertEqual(returns_policy.net_refund("defective", 5000, 695), 5000)
        self.assertEqual(returns_policy.classify(10), "changed_mind")
        self.assertEqual(returns_policy.classify(30), "too_late")
        self.assertEqual(returns_policy.classify(30, defective=True), "defective")
        with self.assertRaises(ValidationError):
            returns_policy.restocking_fee("whim", 1)


class RecommendationTest(ShopCase):
    def test_co_purchase(self):
        for items in ([("P-MUG", 1), ("P-TEE", 1)], [("P-MUG", 1), ("P-TEE", 2), ("P-SOCK", 1)], [("P-MUG", 1), ("P-SOCK", 1)], [("P-LAMP", 1)]):
            self.shop.orders.create("C1", items)
        rec = recommendations.CoPurchase()
        rec.learn(self.shop.orders.all())
        self.assertEqual(rec.together("P-TEE", "P-MUG"), 2)
        self.assertEqual(rec.suggest("P-MUG", 2), ["P-SOCK", "P-TEE"])
        self.assertEqual(rec.lift_bps("P-MUG", "P-TEE", 4), 2 * 4 * 10000 // (3 * 2))
        self.assertEqual(rec.suggest("P-LAMP"), [])


class CampaignAndDiscountTest(unittest.TestCase):
    def test_campaigns(self):
        spring = promotions.Campaign("spring", DATE(2026, 3, 1), DATE(2026, 3, 31), 1500, ["apparel"])
        sale = promotions.Campaign("sale", DATE(2026, 3, 10), DATE(2026, 3, 12), 2500)
        self.assertEqual(promotions.best_campaign_bps([spring, sale], DATE(2026, 3, 11), "apparel"), 2500)
        self.assertEqual(promotions.best_campaign_bps([spring, sale], DATE(2026, 3, 11), "home"), 2500)
        self.assertEqual(promotions.best_campaign_bps([spring, sale], DATE(2026, 3, 20), "home"), 0)
        self.assertEqual(promotions.campaign_discount(2000, [spring], DATE(2026, 3, 2), "apparel"), 300)
        self.assertEqual([c[0] for c in promotions.calendar_of([sale, spring])], ["spring", "sale"])
        with self.assertRaises(ValueError):
            promotions.Campaign("bad", DATE(2026, 3, 2), DATE(2026, 3, 1), 1)

    def test_coupon_stacking(self):
        a, b = Coupon("A", "percent", 2000), Coupon("B", "fixed", 700)
        self.assertEqual(discounts.stack([a, b], 5000), 1700)
        self.assertEqual(discounts.stack([a, b], 5000, limit_bps=2500), 1250)
        self.assertEqual(discounts.best_coupon([a, b], 5000).code, "A")
        self.assertEqual(discounts.best_coupon([a, b], 3000).code, "B")
        self.assertIsNone(discounts.best_coupon([], 100))


class ShippingExtrasTest(unittest.TestCase):
    def test_dimensional_weight_and_surcharges(self):
        self.assertEqual(shipping.dimensional_weight_g(50, 40, 30), 12000)
        self.assertEqual(shipping.billable_weight_g(800, (30, 20, 10)), 1200)
        self.assertEqual(shipping.billable_weight_g(2000, (10, 10, 10)), 2000)
        self.assertEqual(shipping.quote_with_surcharge("US", "US-OR", 1200, 5000), 1195 + 250)
        self.assertEqual(shipping.quote_with_surcharge("US", "US-CA", 1200, 5000), 1195)
        self.assertEqual(shipping.quote_with_surcharge("US", "US-OR", 1200, 20000), 0)
        self.assertEqual(shipping.parcels_needed("EU", [6000, 5000, 3000, 2000]), 2)


class AnalyticsMoneyFreeTest(ShopCase):
    def test_periods_and_growth(self):
        self.shop.place_order("C1", [("P-MUG", 1)])
        self.shop.clock.set(D(2026, 4, 6, 9, 0))
        self.shop.place_order("C1", [("P-TEE", 1)])
        self.shop.place_order("C2", [("P-MUG", 2)])
        orders = self.shop.orders.all()
        monthly = analytics.monthly_revenue(orders)
        self.assertEqual(sorted(monthly), [(2026, 3), (2026, 4)])
        self.assertEqual(analytics.quarterly_revenue(orders), {(2026, 1): monthly[(2026, 3)], (2026, 2): monthly[(2026, 4)]})
        self.assertEqual(analytics.repeat_customers(orders), ["C1"])
        self.assertEqual(analytics.growth_bps(1000, 1250), 2500)
        self.assertIsNone(analytics.growth_bps(0, 5))
        self.assertEqual(analytics.cohort_retention(orders, (2026, 3)), 10000)
        self.assertEqual(analytics.cohort_retention(orders, (2026, 4)), 0)
        self.assertEqual(analytics.average_order_value([]), 0)


if __name__ == "__main__":
    unittest.main()

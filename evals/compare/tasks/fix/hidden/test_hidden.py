"""Hidden regression tests for the rounding fix; they run only at grading."""

import unittest

from billing import discounts, invoice, money, report, tax
from billing.models import Line


class HiddenRounding(unittest.TestCase):
    def test_round_div_ties_away_from_zero(self):
        self.assertEqual(money.round_div(5, 2), 3)
        self.assertEqual(money.round_div(-5, 2), -3)
        self.assertEqual(money.round_div(5, -2), -3)
        self.assertEqual(money.round_div(-5, -2), 3)
        self.assertEqual(money.round_div(1, 2), 1)
        self.assertEqual(money.round_div(0, 7), 0)

    def test_round_div_non_ties_unchanged(self):
        self.assertEqual(money.round_div(4, 10), 0)
        self.assertEqual(money.round_div(6, 10), 1)
        self.assertEqual(money.round_div(-4, 10), 0)
        self.assertEqual(money.round_div(-6, 10), -1)

    def test_tax_negative_and_other_regions(self):
        self.assertEqual(tax.tax_cents(-1050, "NL"), -221)
        self.assertEqual(tax.tax_cents(50, "UK"), 10)
        self.assertEqual(tax.tax_cents(5, "FR"), 1)
        self.assertEqual(tax.tax_cents(2, "FR"), 0)

    def test_discount_tier_edges(self):
        self.assertEqual(discounts.discount_cents(49999), 0)
        self.assertEqual(discounts.discount_cents(50000), 2500)
        self.assertEqual(discounts.discount_cents(100050), 10005)
        self.assertEqual(discounts.discount_cents(50030), 2502)

    def test_coupon_ties(self):
        self.assertEqual(discounts.apply_coupon(50, 100), 1)
        self.assertEqual(discounts.apply_coupon(150, 100), 2)

    def test_invoice_with_everything(self):
        inv = invoice.build_invoice([Line("A", 1, 50010)], "DE", coupon_bps=100)
        # volume 2501 (tie up), coupon on 47509 is 475 (475.09), taxable 47034, tax 8936 (8936.46)
        self.assertEqual(inv["volume_discount"], 2501)
        self.assertEqual(inv["coupon"], 475)
        self.assertEqual(inv["taxable"], 47034)
        self.assertEqual(inv["tax"], 8936)
        self.assertEqual(inv["total"], 55970)

    def test_credit_note_total(self):
        inv = invoice.build_invoice([Line("A", 1, 1050)], "NL")
        self.assertEqual(invoice.credit_note(inv)["total"], -1271)

    def test_prorate_ties_and_negative(self):
        self.assertEqual(invoice.prorate(5, 1, 2), 3)
        self.assertEqual(invoice.prorate(-5, 1, 2), -3)
        self.assertEqual(invoice.prorate(1000, 3, 7), 429)

    def test_allocate(self):
        self.assertEqual(report.allocate(5, [1, 1]), [3, 2])
        self.assertEqual(report.allocate(100, [1, 1, 1]), [33, 33, 34])
        self.assertEqual(report.allocate(-1001, [1, 1]), [-501, -500])


if __name__ == "__main__":
    unittest.main()

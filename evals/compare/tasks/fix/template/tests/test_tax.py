import unittest

from billing import tax
from billing.rates import UnknownRegion


class TaxTest(unittest.TestCase):
    def test_round_numbers(self):
        self.assertEqual(tax.tax_cents(10000, "NL"), 2100)
        self.assertEqual(tax.tax_cents(2000, "US-CA"), 145)
        self.assertEqual(tax.tax_cents(5000, "US-OR"), 0)

    def test_half_cent_rounds_up(self):
        self.assertEqual(tax.tax_cents(1050, "NL"), 221)
        self.assertEqual(tax.tax_cents(250, "DE"), 48)

    def test_gross(self):
        self.assertEqual(tax.gross_cents(1050, "NL"), 1271)

    def test_unknown_region(self):
        with self.assertRaises(UnknownRegion):
            tax.tax_cents(100, "XX")


if __name__ == "__main__":
    unittest.main()

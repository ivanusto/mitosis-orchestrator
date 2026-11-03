import unittest

from billing import invoice, report
from billing.models import Line


class ReportTest(unittest.TestCase):
    def test_allocate_even(self):
        self.assertEqual(report.allocate(1000, [1, 1]), [500, 500])

    def test_allocate_odd_cent(self):
        self.assertEqual(report.allocate(1001, [1, 1]), [501, 500])

    def test_allocate_sums_to_total(self):
        shares = report.allocate(9999, [3, 3, 4])
        self.assertEqual(sum(shares), 9999)

    def test_render(self):
        invs = [
            invoice.build_invoice([Line("A", 1, 1050)], "NL"),
            invoice.build_invoice([Line("B", 2, 5000)], "NL"),
        ]
        text = report.render_report(invs)
        self.assertIn("invoices: 2", text)
        self.assertIn("tax: 23.21", text)
        self.assertIn("total: 133.71", text)


if __name__ == "__main__":
    unittest.main()

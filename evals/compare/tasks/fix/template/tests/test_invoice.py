import unittest

from billing import invoice
from billing.models import Line


class InvoiceTest(unittest.TestCase):
    def test_simple_invoice(self):
        inv = invoice.build_invoice([Line("A", 2, 5000)], "NL")
        self.assertEqual(inv["subtotal"], 10000)
        self.assertEqual(inv["tax"], 2100)
        self.assertEqual(inv["total"], 12100)

    def test_invoice_with_half_cent_tax(self):
        inv = invoice.build_invoice([Line("A", 1, 1050)], "NL")
        self.assertEqual(inv["tax"], 221)
        self.assertEqual(inv["total"], 1271)

    def test_prorate(self):
        self.assertEqual(invoice.prorate(1000, 1, 8), 125)
        self.assertEqual(invoice.prorate(1001, 1, 2), 501)

    def test_credit_note(self):
        inv = invoice.build_invoice([Line("A", 2, 5000)], "NL")
        note = invoice.credit_note(inv)
        self.assertEqual(note["total"], -12100)
        self.assertEqual(note["region"], "NL")


if __name__ == "__main__":
    unittest.main()

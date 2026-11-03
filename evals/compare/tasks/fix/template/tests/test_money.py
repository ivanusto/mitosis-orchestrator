import unittest

from shop import money


class MoneyTest(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(money.parse_amount("12.34"), 1234)
        self.assertEqual(money.parse_amount("-0.5"), -50)
        self.assertEqual(money.parse_amount("7"), 700)
        self.assertEqual(money.format_cents(-1234), "-12.34")
        self.assertEqual(money.format_cents(5, "$"), "$0.05")

    def test_round_div(self):
        self.assertEqual(money.round_div(7, 3), 2)
        self.assertEqual(money.round_div(5, 2), 3)
        self.assertEqual(money.round_div(-5, 2), -3)
        self.assertEqual(money.round_div(4, 10), 0)
        with self.assertRaises(ZeroDivisionError):
            money.round_div(1, 0)

    def test_percent(self):
        self.assertEqual(money.percent(10000, 2100), 2100)
        self.assertEqual(money.percent(1050, 500), 53)

    def test_allocate_simple_cases(self):
        self.assertEqual(money.allocate(100, [1, 1]), [50, 50])
        self.assertEqual(money.allocate(100, [3, 1]), [75, 25])
        self.assertEqual(money.allocate(0, [2, 5]), [0, 0])
        self.assertEqual(money.split_evenly(100, 3), [34, 33, 33])

    def test_allocate_keeps_the_total(self):
        for total in (1, 7, 99, 1001, 12345):
            for weights in ([1, 1, 1], [5, 3, 2], [7, 1], [1999, 4550, 333]):
                self.assertEqual(sum(money.allocate(total, weights)), total)

    def test_allocate_rejects_bad_weights(self):
        for bad in ([], [0, 0], [-1, 2]):
            with self.assertRaises(ValueError):
                money.allocate(10, bad)


if __name__ == "__main__":
    unittest.main()

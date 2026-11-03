import unittest

from billing import money


class MoneyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(money.parse_amount("12.34"), 1234)
        self.assertEqual(money.parse_amount("-0.5"), -50)
        self.assertEqual(money.parse_amount("7"), 700)

    def test_round_div_exact_and_clear_cases(self):
        self.assertEqual(money.round_div(10, 5), 2)
        self.assertEqual(money.round_div(7, 3), 2)
        self.assertEqual(money.round_div(8, 3), 3)
        self.assertEqual(money.round_div(-8, 3), -3)

    def test_zero_denominator(self):
        with self.assertRaises(ZeroDivisionError):
            money.round_div(1, 0)


if __name__ == "__main__":
    unittest.main()

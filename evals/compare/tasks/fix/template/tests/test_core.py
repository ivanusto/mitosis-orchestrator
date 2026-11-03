import datetime
import unittest

from shop import config, ids, textutil, units, validate
from shop.clock import Clock, FixedClock
from shop.errors import ValidationError
from shop.store import Repository
from shop.errors import NotFound


class IdsTest(unittest.TestCase):
    def test_sequence(self):
        gen = ids.IdGenerator("ORD")
        self.assertEqual([gen.next(), gen.next()], ["ORD-0001", "ORD-0002"])
        self.assertEqual(gen.peek(), "ORD-0003")

    def test_parse(self):
        self.assertEqual(ids.parse_id("INV-0042"), ("INV", 42))
        with self.assertRaises(ValueError):
            ids.parse_id("nonsense")


class ClockTest(unittest.TestCase):
    def test_fixed_clock_moves_only_when_told(self):
        clock = FixedClock(datetime.datetime(2026, 3, 2, 9, 0))
        self.assertEqual(clock.today(), datetime.date(2026, 3, 2))
        clock.advance(days=3, hours=2)
        self.assertEqual(clock.now(), datetime.datetime(2026, 3, 5, 11, 0))

    def test_wall_clock_is_naive_utc(self):
        self.assertIsNone(Clock().now().tzinfo)


class UnitsTest(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(units.to_grams(2, "kg"), 2000)
        self.assertEqual(units.to_grams(1, "lb"), 454)
        self.assertEqual(units.to_grams(12, "oz"), 340)

    def test_parse_and_format(self):
        self.assertEqual(units.parse_weight("1.5kg"), 1500)
        self.assertEqual(units.parse_weight(" 300 g "), 300)
        self.assertEqual(units.format_weight(1250), "1.250 kg")
        self.assertEqual(units.format_weight(80), "80 g")
        with self.assertRaises(ValueError):
            units.parse_weight("heavy")
        with self.assertRaises(ValueError):
            units.to_grams(1, "stone")

    def test_total_weight(self):
        self.assertEqual(units.total_weight([(300, 2), (450, 1)]), 1050)


class ValidateTest(unittest.TestCase):
    def test_checks(self):
        self.assertEqual(validate.require_positive_int(3, "n"), 3)
        for bad in (0, -1, True, "3"):
            with self.assertRaises(ValidationError):
                validate.require_positive_int(bad, "n")
        self.assertEqual(validate.require_text("  x ", "t"), "x")
        self.assertEqual(validate.require_email("A@B.org"), "a@b.org")
        with self.assertRaises(ValidationError):
            validate.require_email("nope")
        with self.assertRaises(ValidationError):
            validate.require_in("x", {"a", "b"}, "letter")


class TextTest(unittest.TestCase):
    def test_slugify_and_pad(self):
        self.assertEqual(textutil.slugify("Desk Lamp (XL)!"), "desk-lamp-xl")
        self.assertEqual(textutil.slugify("!!!"), "item")
        self.assertEqual(textutil.pad_left(7, 3), "  7")
        self.assertEqual(textutil.truncate("abcdefgh", 5), "abcd~")

    def test_table(self):
        text = textutil.render_table(["a", "bb"], [(1, "x"), (22, "yyy")], right_align=(0,))
        self.assertEqual(text.splitlines(), [" a  bb", "--  ---", " 1  x", "22  yyy"])


class StoreTest(unittest.TestCase):
    def test_repository(self):
        repo = Repository("thing")
        repo.add("b", 2)
        repo.add("a", 1)
        self.assertEqual(repo.all(), [1, 2])
        self.assertEqual(repo.find(lambda v: v > 1), [2])
        with self.assertRaises(KeyError):
            repo.add("a", 3)
        with self.assertRaises(NotFound):
            repo.get("zz")
        repo.remove("a")
        self.assertEqual(len(repo), 1)


class ConfigTest(unittest.TestCase):
    def test_environment_and_overrides(self):
        settings = config.load(environ={"SHOP_HANDLING_DAYS": "4", "SHOP_CURRENCY_SYMBOL": "EUR"}, overrides={"report_top_n": 5})
        self.assertEqual(settings["handling_days"], 4)
        self.assertEqual(settings["currency_symbol"], "EUR")
        self.assertEqual(settings["report_top_n"], 5)
        self.assertEqual(config.load(environ={})["low_stock_threshold"], 5)
        self.assertIn("handling_days=2", config.describe(config.load(environ={})))


if __name__ == "__main__":
    unittest.main()

import datetime
import unittest

from shop import cache, calendar, currency, events, fx_rates, payments, rate_limit, serializers
from shop.addresses import Address
from shop.address_book import AddressBook
from shop.clock import FixedClock
from shop.errors import InvalidState, NotFound, ValidationError
from shop.permissions import Directory, PermissionDenied
from shop.reviews import ReviewBook
from shop.suppliers import Supplier, SupplierBook
from shop.warehouse import Warehouse, parse_bin, route_key
from shop.wishlist import WishlistBook
from helpers import MONDAY, make_shop


class EventsAndCacheTest(unittest.TestCase):
    def test_bus(self):
        bus = events.EventBus()
        seen = []
        handler = bus.subscribe("paid", lambda **kw: seen.append(kw["order"]))
        self.assertEqual(bus.publish("paid", order="O1"), 1)
        self.assertEqual(bus.publish("shipped", order="O1"), 0)
        self.assertTrue(bus.unsubscribe("paid", handler))
        self.assertEqual(bus.publish("paid", order="O2"), 0)
        self.assertEqual(seen, ["O1"])
        self.assertEqual([t for t, _ in bus.history()], ["paid", "shipped", "paid"])

    def test_cache_expiry(self):
        clock = FixedClock(MONDAY)
        c = cache.TTLCache(clock, 60)
        c.set("a", 1)
        clock.advance(seconds=59)
        self.assertEqual(c.get("a"), 1)
        clock.advance(seconds=1)
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.get_or_compute("b", lambda: 7), 7)
        self.assertEqual(c.get_or_compute("b", lambda: 8), 7)
        clock.advance(seconds=120)
        self.assertEqual(c.purge(), 1)
        self.assertEqual(c.stats()["hits"], 2)

    def test_token_bucket(self):
        clock = FixedClock(MONDAY)
        bucket = rate_limit.TokenBucket(clock, 3, 1)
        self.assertEqual([bucket.allow() for _ in range(4)], [True, True, True, False])
        self.assertEqual(bucket.seconds_until(), 1.0)
        clock.advance(seconds=2)
        self.assertEqual(bucket.available(), 2)
        limiter = rate_limit.KeyedLimiter(clock, 1, 0.5)
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))


class SerializerTest(unittest.TestCase):
    def test_dumps_dataclasses_dates_and_sets(self):
        shop = make_shop()
        order = shop.orders.create("C1", [("P-MUG", 1)])
        data = serializers.to_jsonable(order.lines[0])
        self.assertEqual(data["sku"], "P-MUG")
        self.assertEqual(serializers.dumps({"d": datetime.date(2026, 3, 2), "s": {3, 1}}), '{"d":"2026-03-02","s":[1,3]}')
        self.assertEqual(serializers.roundtrip_dates({"d": "2026-03-02"}, ["d"])["d"], datetime.date(2026, 3, 2))
        self.assertEqual(serializers.parse_datetime("2026-03-02T09:00:00"), MONDAY)


class CalendarExtrasTest(unittest.TestCase):
    def test_leap_years_and_month_lengths(self):
        self.assertTrue(calendar.is_leap_year(2028))
        self.assertFalse(calendar.is_leap_year(2026))
        self.assertEqual(calendar.days_in_month(2028, 2), 29)
        self.assertEqual(calendar.days_in_month(2026, 4), 30)
        self.assertEqual(calendar.month_end(datetime.date(2026, 2, 10)), datetime.date(2026, 2, 28))
        self.assertEqual(calendar.quarter_of(datetime.date(2026, 8, 1)), 3)
        with self.assertRaises(ValueError):
            calendar.days_in_month(2026, 13)

    def test_add_months_without_overflow(self):
        self.assertEqual(calendar.add_months(datetime.date(2026, 3, 5), 2), datetime.date(2026, 5, 5))
        self.assertEqual(calendar.add_months(datetime.date(2026, 11, 15), 3), datetime.date(2027, 2, 15))
        self.assertEqual(calendar.add_months(datetime.date(2026, 3, 5), -3), datetime.date(2025, 12, 5))
        self.assertEqual(calendar.add_months(datetime.date(2026, 3, 5), 0), datetime.date(2026, 3, 5))


class RatesTest(unittest.TestCase):
    def test_table(self):
        table = fx_rates.RateTable()
        self.assertEqual(table.supported(), ["CHF", "EUR", "GBP", "SEK", "USD"])
        self.assertEqual(table.cross_rate_micro("EUR", "USD"), 1085000)
        self.assertLess(table.inverse_pair_error("EUR", "USD"), 5)
        with self.assertRaises(ValidationError):
            table.get("XXX")
        with self.assertRaises(ValidationError):
            table.set_rate("XXX", 0)

    def test_same_currency_and_formatting(self):
        self.assertEqual(currency.convert(1234, "EUR", "EUR"), 1234)
        self.assertEqual(currency.format_amount(1234, "USD"), "USD 12.34")
        self.assertEqual(currency.format_amount(5, "NOK"), "NOK 0.05")


class PaymentsTest(unittest.TestCase):
    def test_fees(self):
        self.assertEqual(payments.fee_for("card", 10000), 320)
        self.assertEqual(payments.fee_for("transfer", 10000), 0)
        self.assertEqual(payments.fee_for("wallet", 1000), 25)
        self.assertEqual(payments.fee_for("card", 0), 0)
        with self.assertRaises(ValidationError):
            payments.fee_for("cheque", 1)
        self.assertEqual(payments.net_of_fees([("card", 10000), ("gift", 500)]), 10000 - 320 + 500)
        self.assertEqual(payments.split_by_method([("card", 1), ("gift", 2), ("card", 3)]), {"card": 4, "gift": 2})
        self.assertEqual(payments.cheapest_split(1000, 400), [("gift", 400), ("card", 600)])


class DirectoryTest(unittest.TestCase):
    def test_roles(self):
        d = Directory()
        d.assign("ann", "clerk")
        d.assign("bob", "admin")
        self.assertTrue(d.can("ann", "orders.write"))
        self.assertFalse(d.can("ann", "refunds.approve"))
        self.assertTrue(d.can("bob", "anything.at.all"))
        with self.assertRaises(PermissionDenied):
            d.require("ann", "refunds.approve")
        self.assertEqual(d.users_with("orders.write"), ["ann", "bob"])
        d.revoke("ann", "clerk")
        self.assertFalse(d.can("ann", "orders.read"))
        with self.assertRaises(ValueError):
            d.assign("x", "wizard")


class AddressBookTest(unittest.TestCase):
    def test_defaults(self):
        book = AddressBook()
        home = Address("1 Main", "Amsterdam", "1012 AB", "NL")
        work = Address("2 Dock", "Rotterdam", "3011 AA", "NL")
        book.add("C1", "home", home)
        book.add("C1", "work", work, make_default=True)
        self.assertEqual(book.get("C1"), work)
        self.assertEqual(book.labels("C1"), ["home", "work"])
        book.remove("C1", "work")
        self.assertEqual(book.default_label("C1"), "home")
        with self.assertRaises(ValidationError):
            book.add("C1", "home", home)
        with self.assertRaises(NotFound):
            book.get("C9")
        with self.assertRaises(ValidationError):
            book.add("C1", "bad", Address("x", "y", "12", "NL"))


class WishlistAndReviewTest(unittest.TestCase):
    def test_wishlist(self):
        shop = make_shop()
        wl = WishlistBook(shop.catalog)
        wl.add("C1", "P-MUG", 2)
        wl.add("C1", "P-TEE")
        wl.add("C2", "P-MUG")
        self.assertEqual(wl.items("C1"), [("P-MUG", 2), ("P-TEE", 1)])
        self.assertEqual(wl.as_basket("C1", in_stock=lambda s: s != "P-TEE"), [("P-MUG", 2)])
        self.assertEqual(wl.most_wanted(1), [("P-MUG", 3)])
        with self.assertRaises(NotFound):
            wl.add("C1", "P-NOPE")

    def test_reviews(self):
        book = ReviewBook()
        for cust, stars in (("a", 5), ("b", 4), ("c", 4), ("d", 4)):
            book.add("P-MUG", cust, stars)
        book.add("P-TEE", "a", 3)
        self.assertEqual(book.average_tenths("P-MUG"), 43)
        self.assertEqual(book.histogram("P-MUG")[4], 3)
        self.assertEqual(book.best_rated(1), ["P-MUG", "P-TEE"])
        self.assertEqual(book.best_rated(2), ["P-MUG"])
        self.assertIsNone(book.average_tenths("P-NONE"))
        with self.assertRaises(ValidationError):
            book.add("P-MUG", "a", 1)
        with self.assertRaises(ValidationError):
            book.add("P-MUG", "z", 9)


class SupplierAndWarehouseTest(unittest.TestCase):
    def test_suppliers(self):
        book = SupplierBook()
        book.add(Supplier("S1", "Alpha", 5, 10, ("P-MUG", "P-TEE"), {"P-MUG": 400, "P-TEE": 900}))
        book.add(Supplier("S2", "Beta", 2, 1, ("P-MUG",), {"P-MUG": 450}))
        self.assertEqual([s.id for s in book.for_sku("P-MUG")], ["S1", "S2"])
        self.assertEqual(book.best_for("P-MUG", 5).id, "S2")
        self.assertEqual(book.best_for("P-MUG", 20).id, "S1")
        self.assertIsNone(book.best_for("P-KIT", 1))

    def test_bins_and_routes(self):
        self.assertEqual(parse_bin("B-07-2"), ("B", 7, 2))
        with self.assertRaises(ValueError):
            parse_bin("7B")
        wh = Warehouse()
        for code, cap in (("A-02-1", 10), ("A-03-1", 10), ("A-04-1", 10), ("B-01-1", 5)):
            wh.add_bin(code, cap)
        self.assertEqual(wh.bins(), ["A-02-1", "A-04-1", "A-03-1", "B-01-1"])
        wh.place("P-MUG", "A-03-1", 6)
        wh.place("P-MUG", "A-02-1", 4)
        self.assertEqual(wh.locate("P-MUG"), [("A-02-1", 4), ("A-03-1", 6)])
        self.assertEqual(wh.stock_of("P-MUG"), 10)
        self.assertEqual(wh.free("A-03-1"), 4)
        wh.take("P-MUG", "A-03-1", 6)
        with self.assertRaises(InvalidState):
            wh.take("P-MUG", "A-03-1", 1)
        with self.assertRaises(InvalidState):
            wh.place("P-TEE", "B-01-1", 6)
        self.assertLess(route_key("A-02-1"), route_key("A-03-1"))


if __name__ == "__main__":
    unittest.main()

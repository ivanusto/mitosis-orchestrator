import unittest

from shop.addresses import Address, regions_in_zone, validate_postcode, zone_for
from shop.catalog import Product
from shop.errors import NotFound, UnknownRegion, ValidationError
from shop.customers import Customer
from helpers import make_shop


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.shop = make_shop()

    def test_lookup_and_search(self):
        self.assertEqual(self.shop.catalog.get("P-MUG").price_cents, 1050)
        self.assertEqual([p.sku for p in self.shop.catalog.search("lamp")], ["P-LAMP"])
        self.assertEqual([p.sku for p in self.shop.catalog.by_category("apparel")], ["P-SOCK", "P-TEE"])
        self.assertEqual(self.shop.catalog.categories(), ["apparel", "books", "bulk", "home", "seasonal"])
        self.assertEqual(self.shop.catalog.price_range(), (333, 4550))
        with self.assertRaises(NotFound):
            self.shop.catalog.get("P-NOPE")

    def test_rejects_bad_product(self):
        with self.assertRaises(ValidationError):
            self.shop.catalog.add(Product("P-BAD", "Bad", -1, 10))


class CustomerTest(unittest.TestCase):
    def setUp(self):
        self.shop = make_shop()

    def test_zone_and_promotion(self):
        self.assertEqual(self.shop.customers.get("C1").zone, "EU")
        self.assertEqual(self.shop.customers.promote("C1").tier, "silver")
        self.assertEqual(self.shop.customers.promote("C1").tier, "gold")
        self.assertEqual(self.shop.customers.promote("C1").tier, "gold")
        self.assertEqual([c.id for c in self.shop.customers.in_region("DE")], ["C2"])

    def test_validation(self):
        with self.assertRaises(UnknownRegion):
            self.shop.customers.add(Customer("C9", "Zed", "z@example.org", "XX"))
        with self.assertRaises(ValidationError):
            self.shop.customers.add(Customer("C9", "Zed", "bad-email", "NL"))


class AddressTest(unittest.TestCase):
    def test_postcodes(self):
        self.assertEqual(validate_postcode("NL", "1012 ab"), "1012 AB")
        self.assertEqual(validate_postcode("UK", "sw1a 1aa"), "SW1A 1AA")
        with self.assertRaises(ValidationError):
            validate_postcode("DE", "1234")
        with self.assertRaises(UnknownRegion):
            validate_postcode("ZZ", "1")
        self.assertEqual(Address("x", "y", "97201", "US-OR").validated().region, "US-OR")

    def test_zones(self):
        self.assertEqual(zone_for("US-CA"), "US")
        self.assertEqual(regions_in_zone("EU"), ["DE", "FR", "NL"])


if __name__ == "__main__":
    unittest.main()

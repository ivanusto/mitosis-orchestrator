"""Shared fixtures: a small shop with a pinned clock."""

import datetime
import unittest

from shop.api import Shop
from shop.catalog import Product
from shop.clock import FixedClock
from shop.customers import Customer
from shop.discounts import Coupon

MONDAY = datetime.datetime(2026, 3, 2, 9, 0)

PRODUCTS = [
    Product("P-BOOK", "Paperback", 1999, 450, taxable=False, category="books"),
    Product("P-KIT", "Pallet kit", 2000, 2500, category="bulk"),
    Product("P-LAMP", "Desk lamp", 4550, 1200, category="home"),
    Product("P-MUG", "Mug", 1050, 300, category="home"),
    Product("P-SEAS", "Wreath", 1700, 800, category="seasonal"),
    Product("P-SOCK", "Socks", 333, 60, category="apparel"),
    Product("P-TEE", "T-shirt", 2499, 200, category="apparel"),
]

CUSTOMERS = [
    Customer("C1", "Ann", "ann@example.org", "NL"),
    Customer("C2", "Bob", "bob@example.org", "DE", tier="gold"),
    Customer("C3", "Cy", "cy@example.org", "US-CA", tier="silver"),
    Customer("C4", "Di", "di@example.org", "UK"),
    Customer("C5", "Eve", "eve@example.org", "FR"),
]

COUPONS = [
    Coupon("FIVE", "fixed", 500),
    Coupon("TENPCT", "percent", 1000),
    Coupon("BIG", "fixed", 2000, min_subtotal=10000),
]

STOCK = {"P-BOOK": 20, "P-KIT": 12, "P-LAMP": 10, "P-MUG": 30, "P-SEAS": 5, "P-SOCK": 100, "P-TEE": 40}


def make_shop(stock=None):
    shop = Shop(clock=FixedClock(MONDAY))
    for p in PRODUCTS:
        shop.catalog.add(p)
    for c in CUSTOMERS:
        shop.customers.add(c)
    for c in COUPONS:
        shop.coupons.add(c)
    for sku, qty in (STOCK if stock is None else stock).items():
        shop.inventory.receive(sku, qty)
    return shop


class ShopCase(unittest.TestCase):
    def setUp(self):
        self.shop = make_shop()

"""Command line front end: reads a JSON scenario and prints a short report."""

import argparse
import json
import sys

from shop import reports
from shop.api import Shop
from shop.catalog import Product
from shop.customers import Customer
from shop.clock import FixedClock
import datetime


def build_shop(spec):
    shop = Shop(clock=FixedClock(datetime.datetime(2026, 3, 2, 9, 0)))
    for p in spec.get("products", []):
        shop.catalog.add(Product(**p))
    for c in spec.get("customers", []):
        shop.customers.add(Customer(**c))
    for sku, qty in spec.get("stock", {}).items():
        shop.inventory.receive(sku, qty)
    return shop


def run(spec, out):
    shop = build_shop(spec)
    for o in spec.get("orders", []):
        shop.place_order(o["customer"], [tuple(i) for i in o["items"]], o.get("coupon"))
    print(reports.money_lines(reports.revenue_summary(shop.orders.all())), file=out)
    print(reports.stock_report(shop.inventory, shop.catalog), file=out)
    return shop


def main(argv=None, out=None):
    parser = argparse.ArgumentParser(description="Run a shop scenario")
    parser.add_argument("scenario", help="path to a JSON scenario file")
    args = parser.parse_args(argv)
    with open(args.scenario, encoding="utf-8") as fh:
        spec = json.load(fh)
    run(spec, out or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

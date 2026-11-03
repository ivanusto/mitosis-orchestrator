"""Bulk import of products and stock from CSV text."""

import csv
import io

from shop import money
from shop.catalog import Product
from shop.errors import ValidationError

REQUIRED = ["sku", "name", "price", "weight_g"]


def parse_products(text):
    """Rows of a CSV with sku,name,price,weight_g[,category,taxable] -> list of Product."""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise ValidationError("missing columns: %s" % ", ".join(missing))
    products = []
    seen = set()
    for number, row in enumerate(reader, start=2):
        sku = (row["sku"] or "").strip()
        if not sku or sku in seen:
            raise ValidationError("line %d: empty or duplicate sku %r" % (number, sku))
        seen.add(sku)
        try:
            weight = int(row["weight_g"])
        except ValueError:
            raise ValidationError("line %d: bad weight %r" % (number, row["weight_g"]))
        taxable = (row.get("taxable") or "yes").strip().lower() not in ("no", "false", "0")
        products.append(Product(sku, row["name"].strip(), money.parse_amount(row["price"]), weight, taxable, (row.get("category") or "general").strip()))
    return products


def import_products(catalog, text):
    """Add every product; returns (added, skipped existing SKUs)."""
    added, skipped = [], []
    for product in parse_products(text):
        if catalog.has(product.sku):
            skipped.append(product.sku)
        else:
            catalog.add(product)
            added.append(product.sku)
    return added, skipped


def import_stock(inventory, text):
    reader = csv.DictReader(io.StringIO(text))
    total = 0
    for row in reader:
        qty = int(row["qty"])
        inventory.receive(row["sku"].strip(), qty)
        total += qty
    return total

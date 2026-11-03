"""Products and the catalog that holds them."""

from dataclasses import dataclass

from shop import validate
from shop.store import Repository


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int
    weight_g: int
    taxable: bool = True
    category: str = "general"


class Catalog:
    def __init__(self):
        self._repo = Repository("product")

    def add(self, product):
        validate.require_text(product.sku, "sku")
        validate.require_non_negative_int(product.price_cents, "price_cents")
        validate.require_non_negative_int(product.weight_g, "weight_g")
        return self._repo.add(product.sku, product)

    def get(self, sku):
        return self._repo.get(sku)

    def has(self, sku):
        return self._repo.has(sku)

    def all(self):
        return self._repo.all()

    def by_category(self, category):
        return self._repo.find(lambda p: p.category == category)

    def search(self, text):
        needle = text.lower()
        return self._repo.find(lambda p: needle in p.name.lower() or needle in p.sku.lower())

    def categories(self):
        return sorted({p.category for p in self._repo.all()})

    def price_range(self):
        prices = [p.price_cents for p in self._repo.all()]
        return (min(prices), max(prices)) if prices else (0, 0)

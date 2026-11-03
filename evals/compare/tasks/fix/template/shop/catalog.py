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


class VariantMatrix:
    """Colour/size variants that share a parent product's price and weight."""

    def __init__(self, parent):
        self.parent = parent
        self._options = {}

    def add_option(self, name, values):
        if not values:
            raise ValueError("an option needs at least one value")
        self._options[name] = list(values)

    def combinations(self):
        combos = [()]
        for name in sorted(self._options):
            combos = [c + ((name, v),) for c in combos for v in self._options[name]]
        return combos

    def sku_for(self, combo):
        suffix = "-".join(str(v).upper()[:3] for _, v in combo)
        return "%s-%s" % (self.parent.sku, suffix) if suffix else self.parent.sku

    def products(self, price_deltas=None):
        """One Product per combination; price_deltas maps an option value to a price change in cents."""
        deltas = price_deltas or {}
        out = []
        for combo in self.combinations():
            extra = sum(deltas.get(v, 0) for _, v in combo)
            out.append(Product(self.sku_for(combo), "%s (%s)" % (self.parent.name, ", ".join(str(v) for _, v in combo)),
                               self.parent.price_cents + extra, self.parent.weight_g, self.parent.taxable, self.parent.category))
        return out

    def register(self, catalog, price_deltas=None):
        for product in self.products(price_deltas):
            catalog.add(product)
        return len(self.combinations())

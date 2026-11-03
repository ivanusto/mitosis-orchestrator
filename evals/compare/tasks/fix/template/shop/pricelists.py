"""Price lists in other currencies, derived from the euro catalog."""

from shop import currency, money
from shop.errors import ValidationError


class PriceList:
    def __init__(self, catalog, code, markup_bps=0, table=None):
        self.catalog = catalog
        self.code = code
        self.markup_bps = markup_bps
        self.table = table

    def price(self, sku):
        """Unit price in this list's currency: converted, then marked up."""
        base = currency.convert(self.catalog.get(sku).price_cents, "EUR", self.code, self.table)
        return base + money.percent(base, self.markup_bps)

    def line(self, sku, qty):
        if qty <= 0:
            raise ValidationError("qty must be positive")
        return self.price(sku) * qty

    def table_of_prices(self):
        return {p.sku: self.price(p.sku) for p in self.catalog.all()}

    def basket_total(self, items):
        return sum(self.line(sku, qty) for sku, qty in items)

    def in_euro(self, cents):
        return currency.convert(cents, self.code, "EUR", self.table)


class PriceListBook:
    def __init__(self, catalog):
        self.catalog = catalog
        self._lists = {}

    def add(self, code, markup_bps=0, table=None):
        self._lists[code] = PriceList(self.catalog, code, markup_bps, table)
        return self._lists[code]

    def get(self, code):
        if code == "EUR" and code not in self._lists:
            self.add("EUR")
        try:
            return self._lists[code]
        except KeyError:
            raise ValidationError("no price list for %s" % code)

    def codes(self):
        return sorted(self._lists)

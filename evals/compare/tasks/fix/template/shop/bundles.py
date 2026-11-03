"""Bundles: several products sold together at a discount."""

from dataclasses import dataclass

from shop import money, pricing, validate


@dataclass(frozen=True)
class Bundle:
    code: str
    name: str
    components: tuple  # ((sku, qty), ...)
    discount_bps: int


class BundleBook:
    def __init__(self, catalog):
        self.catalog = catalog
        self._bundles = {}

    def add(self, bundle):
        validate.require_text(bundle.code, "code")
        for sku, qty in bundle.components:
            self.catalog.get(sku)
            validate.require_positive_int(qty, "qty")
        self._bundles[bundle.code] = bundle
        return bundle

    def get(self, code):
        return self._bundles[code]

    def list_price(self, code, tier="standard"):
        b = self.get(code)
        return sum(pricing.line_amount(self.catalog.get(sku), qty, tier) for sku, qty in b.components)

    def price(self, code, tier="standard"):
        total = self.list_price(code, tier)
        return total - money.percent(total, self.get(code).discount_bps)

    def component_shares(self, code, tier="standard"):
        """Bundle price split over the components in proportion to their list amounts."""
        b = self.get(code)
        amounts = [pricing.line_amount(self.catalog.get(sku), qty, tier) for sku, qty in b.components]
        return list(zip([sku for sku, _ in b.components], money.allocate(self.price(code, tier), amounts)))

    def applicable(self, basket):
        """Bundles whose components are all in the basket {sku: qty}, with how many sets fit."""
        out = []
        for code in sorted(self._bundles):
            sets = min(basket.get(sku, 0) // qty for sku, qty in self._bundles[code].components)
            if sets:
                out.append((code, sets))
        return out

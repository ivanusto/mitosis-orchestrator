"""Suppliers and what they sell."""

from dataclasses import dataclass, field

from shop import validate
from shop.store import Repository


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    lead_business_days: int
    min_order_units: int = 1
    skus: tuple = field(default_factory=tuple)
    unit_cost_cents: dict = field(default_factory=dict)


class SupplierBook:
    def __init__(self):
        self._repo = Repository("supplier")

    def add(self, supplier):
        validate.require_text(supplier.name, "name")
        validate.require_non_negative_int(supplier.lead_business_days, "lead_business_days")
        return self._repo.add(supplier.id, supplier)

    def get(self, supplier_id):
        return self._repo.get(supplier_id)

    def for_sku(self, sku):
        """Suppliers that sell sku, cheapest unit cost first, then shortest lead time, then id."""
        found = self._repo.find(lambda s: sku in s.skus)
        return sorted(found, key=lambda s: (s.unit_cost_cents.get(sku, 10 ** 9), s.lead_business_days, s.id))

    def best_for(self, sku, units):
        for supplier in self.for_sku(sku):
            if units >= supplier.min_order_units:
                return supplier
        return None

    def all(self):
        return self._repo.all()

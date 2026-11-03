"""Plain data holders."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    sku: str
    qty: int
    unit_cents: int

    @property
    def total(self):
        return self.qty * self.unit_cents

"""Stock levels with reservations.

on_hand counts physical units in the warehouse, reserved counts units promised to open
orders. available is what can still be promised.
"""

from shop.errors import InvalidState, OutOfStock
from shop import validate


class Inventory:
    def __init__(self):
        self._on_hand = {}
        self._reserved = {}

    def receive(self, sku, qty):
        validate.require_positive_int(qty, "qty")
        self._on_hand[sku] = self._on_hand.get(sku, 0) + qty
        self._reserved.setdefault(sku, 0)

    def on_hand(self, sku):
        return self._on_hand.get(sku, 0)

    def reserved(self, sku):
        return self._reserved.get(sku, 0)

    def available(self, sku):
        return self.on_hand(sku) - self.reserved(sku)

    def reserve(self, sku, qty):
        validate.require_positive_int(qty, "qty")
        if qty > self.available(sku):
            raise OutOfStock(sku, qty, self.available(sku))
        self._reserved[sku] = self.reserved(sku) + qty

    def release(self, sku, qty):
        """Give back qty units of an earlier reservation."""
        validate.require_positive_int(qty, "qty")
        if qty > self.reserved(sku):
            raise InvalidState("%s: cannot release %d, only %d reserved" % (sku, qty, self.reserved(sku)))
        self._reserved[sku] = self.reserved(sku) - qty

    def commit(self, sku, qty):
        """Units leave the warehouse: they stop being reserved and stop being on hand."""
        validate.require_positive_int(qty, "qty")
        if qty > self.reserved(sku):
            raise InvalidState("%s: cannot ship %d, only %d reserved" % (sku, qty, self.reserved(sku)))
        self._reserved[sku] = self.reserved(sku) - qty
        self._on_hand[sku] = self.on_hand(sku) - qty

    def restock(self, sku, qty):
        """Units come back from a customer."""
        self.receive(sku, qty)

    def snapshot(self):
        return {sku: (self._on_hand[sku], self._reserved.get(sku, 0)) for sku in sorted(self._on_hand)}

    def low_stock(self, threshold):
        return sorted(sku for sku in self._on_hand if self.available(sku) <= threshold)

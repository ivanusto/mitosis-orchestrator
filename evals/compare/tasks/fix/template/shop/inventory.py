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


class LotInventory:
    """Stock tracked in dated lots, so the oldest expiry ships first (first-expiry-first-out)."""

    def __init__(self):
        self._lots = {}

    def receive(self, sku, qty, lot, expires=None):
        validate.require_positive_int(qty, "qty")
        lots = self._lots.setdefault(sku, {})
        entry = lots.setdefault(lot, {"qty": 0, "expires": expires})
        entry["qty"] += qty
        if expires is not None:
            entry["expires"] = expires

    def lots(self, sku):
        """[(lot, qty, expires)] in picking order: soonest expiry first, undated lots last, then lot name."""
        items = [(lot, e["qty"], e["expires"]) for lot, e in self._lots.get(sku, {}).items() if e["qty"] > 0]
        return sorted(items, key=lambda t: (t[2] is None, t[2] or 0, t[0]))

    def on_hand(self, sku):
        return sum(q for _, q, _ in self.lots(sku))

    def take(self, sku, qty, today=None):
        """Remove qty units, oldest expiry first; expired lots are skipped. Returns [(lot, qty)]."""
        taken = []
        need = qty
        for lot, have, expires in self.lots(sku):
            if need <= 0:
                break
            if today is not None and expires is not None and expires < today:
                continue
            part = min(need, have)
            self._lots[sku][lot]["qty"] -= part
            taken.append((lot, part))
            need -= part
        if need > 0:
            for lot, part in taken:
                self._lots[sku][lot]["qty"] += part
            raise OutOfStock(sku, qty, qty - need)
        return taken

    def expired(self, today):
        return sorted((sku, lot, q) for sku in self._lots for lot, q, e in self.lots(sku) if e is not None and e < today)

    def write_off(self, today):
        total = 0
        for sku, lot, q in self.expired(today):
            self._lots[sku][lot]["qty"] = 0
            total += q
        return total

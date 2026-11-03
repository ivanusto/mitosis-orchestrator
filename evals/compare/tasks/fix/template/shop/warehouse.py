"""Bin locations inside the warehouse."""

import re

from shop import validate
from shop.errors import InvalidState, NotFound

_BIN = re.compile(r"^([A-Z])-(\d{2})-(\d)$")


def parse_bin(code):
    """'B-07-2' is aisle B, rack 7, shelf 2."""
    match = _BIN.match(code)
    if not match:
        raise ValueError("bad bin code: %r" % (code,))
    return match.group(1), int(match.group(2)), int(match.group(3))


def route_key(code):
    aisle, rack, shelf = parse_bin(code)
    # walk up the even racks of an aisle and back down the odd ones
    return (aisle, rack % 2, rack if rack % 2 == 0 else -rack, shelf)


class Warehouse:
    def __init__(self):
        self._capacity = {}
        self._contents = {}

    def add_bin(self, code, capacity):
        parse_bin(code)
        validate.require_positive_int(capacity, "capacity")
        self._capacity[code] = capacity
        self._contents.setdefault(code, {})

    def load(self, code):
        return sum(self._contents[code].values())

    def free(self, code):
        return self._capacity[code] - self.load(code)

    def place(self, sku, code, qty):
        validate.require_positive_int(qty, "qty")
        if code not in self._capacity:
            raise NotFound("bin", code)
        if qty > self.free(code):
            raise InvalidState("bin %s has room for %d, not %d" % (code, self.free(code), qty))
        self._contents[code][sku] = self._contents[code].get(sku, 0) + qty

    def take(self, sku, code, qty):
        have = self._contents.get(code, {}).get(sku, 0)
        if qty > have:
            raise InvalidState("bin %s holds %d of %s, not %d" % (code, have, sku, qty))
        self._contents[code][sku] = have - qty
        if self._contents[code][sku] == 0:
            del self._contents[code][sku]

    def locate(self, sku):
        """[(bin, qty)] holding sku, in walking order."""
        found = [(code, items[sku]) for code, items in self._contents.items() if sku in items]
        return sorted(found, key=lambda pair: route_key(pair[0]))

    def stock_of(self, sku):
        return sum(qty for _, qty in self.locate(sku))

    def bins(self):
        return sorted(self._capacity, key=route_key)

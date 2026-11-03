"""Co-purchase recommendations from past orders."""

import itertools


class CoPurchase:
    def __init__(self):
        self._pairs = {}
        self._counts = {}

    def learn(self, orders):
        for order in orders:
            if order.status == "cancelled":
                continue
            skus = sorted({line.sku for line in order.lines})
            for sku in skus:
                self._counts[sku] = self._counts.get(sku, 0) + 1
            for a, b in itertools.combinations(skus, 2):
                self._pairs[(a, b)] = self._pairs.get((a, b), 0) + 1

    def together(self, a, b):
        return self._pairs.get(tuple(sorted((a, b))), 0)

    def lift_bps(self, a, b, total_orders):
        """How much more often a and b are bought together than chance predicts, in basis points of 1.0."""
        ca, cb = self._counts.get(a, 0), self._counts.get(b, 0)
        if not (ca and cb and total_orders):
            return 0
        return self.together(a, b) * total_orders * 10000 // (ca * cb)

    def suggest(self, sku, n=3):
        scored = []
        for (a, b), count in self._pairs.items():
            if sku == a:
                scored.append((b, count))
            elif sku == b:
                scored.append((a, count))
        scored.sort(key=lambda t: (-t[1], t[0]))
        return [s for s, _ in scored[:n]]

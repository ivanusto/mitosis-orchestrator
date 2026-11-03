"""Wish lists, with a way to turn a list into an order basket."""

from shop import validate


class WishlistBook:
    def __init__(self, catalog):
        self.catalog = catalog
        self._lists = {}

    def add(self, customer_id, sku, qty=1):
        self.catalog.get(sku)
        validate.require_positive_int(qty, "qty")
        items = self._lists.setdefault(customer_id, {})
        items[sku] = items.get(sku, 0) + qty

    def remove(self, customer_id, sku):
        self._lists.get(customer_id, {}).pop(sku, None)

    def items(self, customer_id):
        return sorted(self._lists.get(customer_id, {}).items())

    def as_basket(self, customer_id, in_stock=None):
        """[(sku, qty)] ready for OrderService.create, skipping SKUs that in_stock(sku) says are unavailable."""
        basket = []
        for sku, qty in self.items(customer_id):
            if in_stock is None or in_stock(sku):
                basket.append((sku, qty))
        return basket

    def most_wanted(self, n):
        counts = {}
        for items in self._lists.values():
            for sku, qty in items.items():
                counts[sku] = counts.get(sku, 0) + qty
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

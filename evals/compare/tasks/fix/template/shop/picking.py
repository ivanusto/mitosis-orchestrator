"""Pick lists: which bins to visit, in what order, for a batch of orders."""

from shop.errors import OutOfStock
from shop.warehouse import route_key


def pick_lines(warehouse, order):
    """[(bin, sku, qty)] for one order, taking from the bins that come first on the walking route."""
    lines = []
    for line in order.lines:
        needed = line.qty - order.shipped.get(line.sku, 0)
        for code, qty in warehouse.locate(line.sku):
            if needed <= 0:
                break
            take = min(needed, qty)
            lines.append((code, line.sku, take))
            needed -= take
        if needed > 0:
            raise OutOfStock(line.sku, line.qty, line.qty - needed)
    return sorted(lines, key=lambda l: (route_key(l[0]), l[1]))


def batch(warehouse, orders):
    """One combined walk for several orders: quantities per (bin, sku) with the order ids that need them."""
    merged = {}
    for order in orders:
        for code, sku, qty in pick_lines(warehouse, order):
            entry = merged.setdefault((code, sku), {"qty": 0, "orders": []})
            entry["qty"] += qty
            entry["orders"].append(order.id)
    return [(code, sku, v["qty"], v["orders"]) for (code, sku), v in sorted(merged.items(), key=lambda kv: (route_key(kv[0][0]), kv[0][1]))]


def walking_distance(codes):
    """Rough walk length in aisle changes plus rack steps along the given bins, in order."""
    total = 0
    previous = None
    for code in codes:
        aisle, rack, _ = _split(code)
        if previous is not None:
            total += abs(ord(aisle) - ord(previous[0])) * 10 + abs(rack - previous[1])
        previous = (aisle, rack)
    return total


def _split(code):
    from shop.warehouse import parse_bin
    return parse_bin(code)

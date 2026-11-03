"""Reorder suggestions from recent sales."""


def units_sold(orders):
    sold = {}
    for order in orders:
        if order.status == "cancelled":
            continue
        for line in order.lines:
            sold[line.sku] = sold.get(line.sku, 0) + line.qty
    return sold


def daily_rate(units, days):
    if days <= 0:
        raise ValueError("days must be positive")
    return units / float(days)


def reorder_quantity(inventory, sku, units, days, cover_days):
    """Units to order so that available stock covers cover_days of the recent rate."""
    need = daily_rate(units, days) * cover_days
    short = need - inventory.available(sku)
    return max(0, int(short + 0.999999))


def suggestions(inventory, orders, days, cover_days):
    sold = units_sold(orders)
    out = {}
    for sku in sorted(sold):
        qty = reorder_quantity(inventory, sku, sold[sku], days, cover_days)
        if qty:
            out[sku] = qty
    return out

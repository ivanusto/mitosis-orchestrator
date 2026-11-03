"""Reserving stock for paid orders, shipping them (possibly in parts) and cancelling."""

from shop import calendar, shipping
from shop.errors import InvalidState, OutOfStock, ValidationError

HANDLING_DAYS = 2


class FulfilmentService:
    def __init__(self, inventory, orders, clock, holidays=frozenset()):
        self.inventory = inventory
        self.orders = orders
        self.clock = clock
        self.holidays = holidays

    def reserve(self, order_id):
        """Reserve every line of a paid order, all or nothing."""
        order = self.orders.get(order_id)
        if order.status != "paid":
            raise InvalidState("order %s is %s, expected paid" % (order.id, order.status))
        done = []
        try:
            for line in order.lines:
                self.inventory.reserve(line.sku, line.qty)
                done.append(line)
        except OutOfStock:
            for line in done:
                self.inventory.release(line.sku, line.qty)
            raise
        order.status = "allocated"
        return order

    def ship_by(self, order_id):
        order = self.orders.get(order_id)
        if order.paid_at is None:
            raise InvalidState("order %s is not paid" % order.id)
        return calendar.add_business_days(order.paid_at.date(), HANDLING_DAYS, self.holidays)

    def ship(self, order_id, quantities=None):
        """Ship the given {sku: qty} (default: everything left) as one parcel."""
        order = self.orders.get(order_id)
        if order.status not in ("allocated", "partial"):
            raise InvalidState("order %s is %s, cannot ship" % (order.id, order.status))
        wanted = dict(quantities) if quantities else {l.sku: order.remaining(l.sku) for l in order.lines}
        weight = 0
        for sku, qty in wanted.items():
            if qty <= 0 or qty > order.remaining(sku):
                raise ValidationError("cannot ship %d of %s, %d left" % (qty, sku, order.remaining(sku)))
            weight += qty * (order.line(sku).weight_g // order.line(sku).qty)
        limit, price = next(t for t in shipping.TIERS[order.zone] if weight <= t[0])
        for sku, qty in sorted(wanted.items()):
            self.inventory.commit(sku, qty)
            order.shipped[sku] = order.shipped.get(sku, 0) + qty
        order.shipments.append({
            "number": len(order.shipments) + 1,
            "date": self.clock.today(),
            "lines": dict(sorted(wanted.items())),
            "weight_g": weight,
            "tier_limit_g": limit,
            "tier_price_cents": price,
        })
        done = all(order.remaining(l.sku) == 0 for l in order.lines)
        order.status = "shipped" if done else "partial"
        return order.shipments[-1]

    def cancel(self, order_id):
        """Cancel an order and give back the stock that was reserved but never shipped."""
        order = self.orders.get(order_id)
        if order.status in ("shipped", "cancelled"):
            raise InvalidState("order %s is %s, cannot cancel" % (order.id, order.status))
        if order.status in ("allocated", "partial"):
            for line in order.lines:
                if order.remaining(line.sku) > 0:
                    self.inventory.release(line.sku, line.qty)
        order.status = "cancelled"
        return order

    def backlog(self):
        """Orders that still have units to ship, oldest first."""
        return [o for o in self.orders.all() if o.status in ("allocated", "partial")]

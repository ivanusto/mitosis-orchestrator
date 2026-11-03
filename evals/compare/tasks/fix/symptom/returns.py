"""Returns: windows, refund amounts and restocking."""

from dataclasses import dataclass

from shop import calendar, money
from shop.errors import InvalidState, ReturnWindowClosed, ValidationError

RETURN_WINDOW_DAYS = 10


@dataclass
class ReturnRecord:
    order_id: str
    sku: str
    qty: int
    refund_cents: int
    refunds: list  # (payment method, negative cents) per original payment


class ReturnService:
    def __init__(self, orders, invoices, inventory, loyalty, clock, holidays=frozenset()):
        self.orders = orders
        self.invoices = invoices
        self.inventory = inventory
        self.loyalty = loyalty
        self.clock = clock
        self.holidays = holidays
        self._records = []

    def deadline(self, order_id):
        order = self.orders.get(order_id)
        if not order.shipments:
            raise InvalidState("order %s has not shipped" % order.id)
        return calendar.add_business_days(order.shipments[-1]["date"], RETURN_WINDOW_DAYS, self.holidays)

    def refund_for(self, order_id, sku, qty):
        invoice = self.invoices.for_order(order_id)
        line = next(l for l in invoice.lines if l.sku == sku)
        return money.round_div(line.total * qty, line.qty)

    def request(self, order_id, sku, qty):
        order = self.orders.get(order_id)
        if self.clock.today() > self.deadline(order_id):
            raise ReturnWindowClosed(order_id)
        returnable = order.shipped.get(sku, 0) - order.returned.get(sku, 0)
        if qty <= 0 or qty > returnable:
            raise ValidationError("can return at most %d of %s" % (returnable, sku))
        refund = self.refund_for(order_id, sku, qty)
        weights = [cents for _, cents in order.payments]
        parts = _spread_cents(-refund, weights)
        refunds = [(method, part) for (method, _), part in zip(order.payments, parts)]
        order.returned[sku] = order.returned.get(sku, 0) + qty
        self.inventory.restock(sku, qty)
        record = ReturnRecord(order.id, sku, qty, refund, refunds)
        self._records.append(record)
        return record

    def records(self):
        return list(self._records)

    def total_refunded(self):
        return sum(r.refund_cents for r in self._records)


def exchange_difference(returned_line_total_cents, new_line_total_cents):
    """What the customer pays (positive) or gets back (negative) when swapping one item for another."""
    return new_line_total_cents - returned_line_total_cents


def returns_by_reason(records, reasons):
    """Count return records per reason; reasons maps (order id, sku) to a reason string."""
    out = {}
    for r in records:
        reason = reasons.get((r.order_id, r.sku), "unknown")
        out[reason] = out.get(reason, 0) + r.qty
    return dict(sorted(out.items()))


def _spread_cents(total, weights):
    """Largest remainder split kept next to its only caller."""
    sign = -1 if total < 0 else 1
    mag = abs(total)
    wsum = sum(weights)
    shares = [mag * w // wsum for w in weights]
    left = mag - sum(shares)
    for i in sorted(range(len(weights)), key=lambda i: (-(mag * weights[i] % wsum), i))[:left]:
        shares[i] += 1
    return [sign * s for s in shares]

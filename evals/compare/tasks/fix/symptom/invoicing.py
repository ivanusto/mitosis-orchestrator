"""Invoices built from orders."""

from dataclasses import dataclass

from shop import money
from shop.ids import IdGenerator
from shop.textutil import render_table


@dataclass
class InvoiceLine:
    sku: str
    description: str
    qty: int
    amount: int
    discount: int
    tax: int
    shipping: int

    @property
    def total(self):
        return self.amount - self.discount + self.tax + self.shipping


@dataclass
class Invoice:
    number: str
    order_id: str
    customer_id: str
    lines: list
    issued_on: object

    @property
    def total(self):
        return sum(line.total for line in self.lines)

    @property
    def shipping(self):
        return sum(line.shipping for line in self.lines)


class InvoiceBook:
    def __init__(self, orders, clock):
        self.orders = orders
        self.clock = clock
        self._ids = IdGenerator("INV")
        self._by_order = {}

    def build(self, order_id):
        order = self.orders.get(order_id)
        nets = [line.net_cents for line in order.lines]
        if order.shipping_cents == 0:
            shares = [0] * len(nets)
        elif sum(nets) == 0:
            shares = money.split_evenly(order.shipping_cents, len(nets))
        else:
            shares = _spread_cents(order.shipping_cents, nets)
        lines = [
            InvoiceLine(l.sku, l.name, l.qty, l.amount_cents, l.discount_cents, l.tax_cents, share)
            for l, share in zip(order.lines, shares)
        ]
        invoice = Invoice(self._ids.next(), order.id, order.customer_id, lines, self.clock.today())
        self._by_order[order.id] = invoice
        return invoice

    def for_order(self, order_id):
        return self._by_order[order_id]

    def all(self):
        return [self._by_order[k] for k in sorted(self._by_order)]


def installments(total_cents, count):
    """Split an invoice total into equal monthly payments; earlier ones are a cent larger if needed.

    A negative total (a credit note) splits the same way with the sign flipped.
    """
    return money.split_evenly(total_cents, count)


def render(invoice):
    rows = [(l.sku, l.qty, money.format_cents(l.amount), money.format_cents(-l.discount), money.format_cents(l.tax),
             money.format_cents(l.shipping), money.format_cents(l.total)) for l in invoice.lines]
    table = render_table(["sku", "qty", "amount", "discount", "tax", "shipping", "total"], rows, right_align=(1, 2, 3, 4, 5, 6))
    return "%s for order %s\n%s\ntotal %s" % (invoice.number, invoice.order_id, table, money.format_cents(invoice.total))


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

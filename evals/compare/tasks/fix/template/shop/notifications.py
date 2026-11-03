"""Customer messages, rendered from small templates and queued for sending."""

from shop import money

TEMPLATES = {
    "order_placed": "Hello {name}, we received order {order} for {total}.",
    "order_shipped": "Hello {name}, order {order} shipped in {parcels} parcel(s), expected {eta}.",
    "order_cancelled": "Hello {name}, order {order} was cancelled.",
    "refund": "Hello {name}, we refunded {amount} for order {order}.",
}


class Outbox:
    def __init__(self):
        self._messages = []

    def render(self, kind, **fields):
        return TEMPLATES[kind].format(**fields)

    def send(self, customer, kind, **fields):
        message = {"to": customer.email, "kind": kind, "body": self.render(kind, name=customer.name, **fields)}
        self._messages.append(message)
        return message

    def order_placed(self, customer, order):
        return self.send(customer, "order_placed", order=order.id, total=money.format_cents(order.total_cents))

    def order_shipped(self, customer, order, eta):
        return self.send(customer, "order_shipped", order=order.id, parcels=len(order.shipments), eta=eta.isoformat())

    def order_cancelled(self, customer, order):
        return self.send(customer, "order_cancelled", order=order.id)

    def refund(self, customer, order, cents):
        return self.send(customer, "refund", order=order.id, amount=money.format_cents(cents))

    def messages(self, kind=None):
        return [m for m in self._messages if kind is None or m["kind"] == kind]

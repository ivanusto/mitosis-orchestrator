"""Orders: pricing a basket into lines, discounts, tax and shipping."""

from dataclasses import dataclass, field

from shop import discounts, pricing, promotions, shipping, tax, validate
from shop.errors import InvalidState, NotFound, ValidationError
from shop.ids import IdGenerator

STATUSES = ("new", "paid", "allocated", "partial", "shipped", "cancelled")


@dataclass
class OrderLine:
    sku: str
    name: str
    qty: int
    unit_cents: int
    amount_cents: int
    discount_cents: int
    tax_cents: int
    weight_g: int
    category: str = "general"

    @property
    def net_cents(self):
        return self.amount_cents - self.discount_cents

    @property
    def total_cents(self):
        return self.net_cents + self.tax_cents


@dataclass
class Order:
    id: str
    customer_id: str
    region: str
    zone: str
    lines: list
    coupon_code: str
    shipping_cents: int
    created_at: object
    status: str = "new"
    paid_at: object = None
    payments: list = field(default_factory=list)
    shipped: dict = field(default_factory=dict)
    shipments: list = field(default_factory=list)
    returned: dict = field(default_factory=dict)

    @property
    def subtotal_cents(self):
        return sum(line.amount_cents for line in self.lines)

    @property
    def discount_cents(self):
        return sum(line.discount_cents for line in self.lines)

    @property
    def tax_cents(self):
        return sum(line.tax_cents for line in self.lines)

    @property
    def total_cents(self):
        return self.subtotal_cents - self.discount_cents + self.tax_cents + self.shipping_cents

    @property
    def weight_g(self):
        return sum(line.weight_g for line in self.lines)

    def line(self, sku):
        for line in self.lines:
            if line.sku == sku:
                return line
        raise NotFound("order line", sku)

    def remaining(self, sku):
        """Units of sku not shipped yet."""
        return self.line(sku).qty - self.shipped.get(sku, 0)


class OrderService:
    def __init__(self, catalog, customers, coupons, clock):
        self.catalog = catalog
        self.customers = customers
        self.coupons = coupons
        self.clock = clock
        self._ids = IdGenerator("ORD")
        self._orders = {}

    def _merge(self, items):
        merged = {}
        for sku, qty in items:
            validate.require_positive_int(qty, "qty")
            merged[sku] = merged.get(sku, 0) + qty
        if not merged:
            raise ValidationError("an order needs at least one item")
        return sorted(merged.items())

    def create(self, customer_id, items, coupon_code=None):
        customer = self.customers.get(customer_id)
        coupon = self.coupons.get(coupon_code)
        merged = self._merge(items)
        products = [(self.catalog.get(sku), qty) for sku, qty in merged]
        amounts = [pricing.line_amount(p, qty, customer.tier) for p, qty in products]
        promos = [promotions.line_promotion(a, qty, p.category) for a, (p, qty) in zip(amounts, products)]
        after_promo = [a - d for a, d in zip(amounts, promos)]
        coupon_parts = discounts.apply_coupon(after_promo, coupon)
        lines = []
        for (product, qty), amount, promo, part in zip(products, amounts, promos, coupon_parts):
            discount = promo + part
            lines.append(OrderLine(
                sku=product.sku, name=product.name, qty=qty,
                unit_cents=pricing.unit_price(product, customer.tier),
                amount_cents=amount, discount_cents=discount,
                tax_cents=tax.tax_for_amount(amount - discount, customer.region, product.taxable),
                weight_g=product.weight_g * qty, category=product.category,
            ))
        subtotal_after = sum(line.net_cents for line in lines)
        order = Order(
            id=self._ids.next(), customer_id=customer.id, region=customer.region, zone=customer.zone,
            lines=lines, coupon_code=coupon.code if coupon else None,
            shipping_cents=0, created_at=self.clock.now(),
        )
        order.shipping_cents = _quote(order.zone, order.weight_g, subtotal_after)
        self._orders[order.id] = order
        return order

    def get(self, order_id):
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFound("order", order_id)

    def all(self):
        return [self._orders[k] for k in sorted(self._orders)]

    def for_customer(self, customer_id):
        return [o for o in self.all() if o.customer_id == customer_id]

    def mark_paid(self, order_id, payments=None):
        order = self.get(order_id)
        if order.status != "new":
            raise InvalidState("order %s is %s, not new" % (order.id, order.status))
        payments = list(payments) if payments else [("card", order.total_cents)]
        if sum(cents for _, cents in payments) != order.total_cents:
            raise ValidationError("payments %d do not add up to the total %d" % (sum(c for _, c in payments), order.total_cents))
        order.payments = payments
        order.paid_at = self.clock.now()
        order.status = "paid"
        return order


def order_summary(order):
    return "%s %s lines=%d total=%d" % (order.id, order.status, len(order.lines), order.total_cents)


def reprice(service, order_id, new_items):
    """Replace the basket of an unpaid order and price it again with the same coupon."""
    order = service.get(order_id)
    if order.status != "new":
        raise InvalidState("order %s is %s, only new orders can be repriced" % (order.id, order.status))
    fresh = service.create(order.customer_id, new_items, order.coupon_code)
    del service._orders[fresh.id]
    order.lines = fresh.lines
    order.shipping_cents = fresh.shipping_cents
    return order


def _quote(zone, weight_g, subtotal_cents):
    try:
        return shipping.quote(zone, weight_g, subtotal_cents)
    except IndexError:
        return shipping.TIERS[zone][-1][1]

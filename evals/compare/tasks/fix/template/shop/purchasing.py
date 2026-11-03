"""Purchase orders to suppliers and goods receipt."""

from dataclasses import dataclass, field

from shop import calendar, validate
from shop.errors import InvalidState, NotFound
from shop.ids import IdGenerator


@dataclass
class PurchaseLine:
    sku: str
    ordered: int
    unit_cost_cents: int
    received: int = 0

    @property
    def open_qty(self):
        return self.ordered - self.received


@dataclass
class PurchaseOrder:
    id: str
    supplier_id: str
    placed_on: object
    expected_on: object
    lines: list
    status: str = "open"
    receipts: list = field(default_factory=list)


class Purchasing:
    def __init__(self, suppliers, inventory, clock, holidays=frozenset()):
        self.suppliers = suppliers
        self.inventory = inventory
        self.clock = clock
        self.holidays = holidays
        self._ids = IdGenerator("PO")
        self._orders = {}

    def create(self, supplier_id, items):
        supplier = self.suppliers.get(supplier_id)
        lines = []
        for sku, qty in items:
            validate.require_positive_int(qty, "qty")
            if sku not in supplier.skus:
                raise InvalidState("%s does not sell %s" % (supplier.name, sku))
            lines.append(PurchaseLine(sku, qty, supplier.unit_cost_cents.get(sku, 0)))
        if sum(l.ordered for l in lines) < supplier.min_order_units:
            raise InvalidState("below the minimum order of %d units" % supplier.min_order_units)
        today = self.clock.today()
        po = PurchaseOrder(self._ids.next(), supplier_id, today,
                           calendar.add_business_days(today, supplier.lead_business_days, self.holidays), lines)
        self._orders[po.id] = po
        return po

    def get(self, po_id):
        try:
            return self._orders[po_id]
        except KeyError:
            raise NotFound("purchase order", po_id)

    def receive(self, po_id, sku, qty):
        """Book qty units of sku delivered against a purchase order; they go on the shelf immediately."""
        po = self.get(po_id)
        if po.status == "closed":
            raise InvalidState("%s is closed" % po.id)
        line = next((l for l in po.lines if l.sku == sku), None)
        if line is None:
            raise NotFound("purchase line", sku)
        if qty <= 0 or qty > line.open_qty:
            raise InvalidState("%s: %d open, cannot receive %d" % (sku, line.open_qty, qty))
        line.received += qty
        self.inventory.receive(sku, qty)
        po.receipts.append((self.clock.today(), sku, qty))
        if all(l.open_qty == 0 for l in po.lines):
            po.status = "closed"
        return po.status

    def value_cents(self, po_id):
        return sum(l.ordered * l.unit_cost_cents for l in self.get(po_id).lines)

    def open_value_cents(self, po_id):
        return sum(l.open_qty * l.unit_cost_cents for l in self.get(po_id).lines)

    def late(self, today):
        return sorted(p.id for p in self._orders.values() if p.status == "open" and p.expected_on < today)

    def open_orders(self):
        return [self._orders[k] for k in sorted(self._orders) if self._orders[k].status == "open"]

    def reorder_plan(self, shortfalls):
        """{sku: units short} -> [(supplier id, sku, units)] choosing the best supplier for each SKU."""
        plan = []
        for sku in sorted(shortfalls):
            supplier = self.suppliers.best_for(sku, shortfalls[sku])
            if supplier is None and self.suppliers.for_sku(sku):
                supplier = self.suppliers.for_sku(sku)[0]  # nobody accepts so small an order: take the cheapest and meet its minimum
            if supplier is not None:
                plan.append((supplier.id, sku, max(shortfalls[sku], supplier.min_order_units)))
        return plan

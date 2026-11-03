"""The facade other code (and the command line) talks to. It wires every service together."""

from shop import (audit, calendar, config, discounts, dunning, events, fulfilment, invoicing, ledger, loyalty, notifications, orders,
                  payments, purchasing, returns, shipping, subscriptions, suppliers, warehouse)
from shop.catalog import Catalog
from shop.clock import Clock
from shop.customers import CustomerBook
from shop.inventory import Inventory


class Shop:
    def __init__(self, clock=None, holidays=None, settings=None):
        self.settings = config.load(environ={}, overrides=settings)
        self.clock = clock or Clock()
        self.holidays = frozenset(holidays) if holidays is not None else frozenset(calendar.holidays_for(self.clock.today().year))
        self.catalog = Catalog()
        self.customers = CustomerBook()
        self.coupons = discounts.CouponBook()
        self.inventory = Inventory()
        self.orders = orders.OrderService(self.catalog, self.customers, self.coupons, self.clock)
        self.fulfilment = fulfilment.FulfilmentService(self.inventory, self.orders, self.clock, self.holidays)
        self.invoices = invoicing.InvoiceBook(self.orders, self.clock)
        self.loyalty = loyalty.LoyaltyBook()
        self.returns = returns.ReturnService(self.orders, self.invoices, self.inventory, self.loyalty, self.clock, self.holidays)
        self.audit = audit.AuditLog(self.clock)
        self.outbox = notifications.Outbox()
        self.events = events.EventBus()
        self.ledger = ledger.Ledger()
        self.subscriptions = subscriptions.SubscriptionService(self.ledger, self.clock)
        self.dunning = dunning.Dunning(self.ledger, self.holidays)
        self.suppliers = suppliers.SupplierBook()
        self.purchasing = purchasing.Purchasing(self.suppliers, self.inventory, self.clock, self.holidays)
        self.warehouse = warehouse.Warehouse()

    # -- placing and paying
    def place_order(self, customer_id, items, coupon_code=None, payments=None):
        order = self.orders.create(customer_id, items, coupon_code)
        self.orders.mark_paid(order.id, payments)
        self.fulfilment.reserve(order.id)
        customer = self.customers.get(customer_id)
        self.loyalty.earn(customer_id, order.total_cents - order.shipping_cents - order.tax_cents, customer.tier, order.id)
        self.invoices.build(order.id)
        self.audit.record("placed", order.id, "total=%d" % order.total_cents)
        self.outbox.order_placed(customer, order)
        return order

    def ship(self, order_id, quantities=None):
        shipment = self.fulfilment.ship(order_id, quantities)
        order = self.orders.get(order_id)
        eta = shipping.delivery_date(order.zone, shipment["date"], self.holidays)
        self.audit.record("shipped", order_id, "parcel=%d" % shipment["number"])
        self.outbox.order_shipped(self.customers.get(order.customer_id), order, eta)
        return shipment

    def cancel(self, order_id):
        order = self.fulfilment.cancel(order_id)
        self.loyalty.reverse(order.customer_id, order.id)
        self.audit.record("cancelled", order_id)
        self.outbox.order_cancelled(self.customers.get(order.customer_id), order)
        return order

    def return_item(self, order_id, sku, qty):
        record = self.returns.request(order_id, sku, qty)
        order = self.orders.get(order_id)
        self.audit.record("returned", order_id, "%s x%d" % (sku, qty))
        self.outbox.refund(self.customers.get(order.customer_id), order, record.refund_cents)
        return record

    def snapshot(self):
        return {
            "orders": {o.id: o.status for o in self.orders.all()},
            "stock": self.inventory.snapshot(),
            "audit": self.audit.count_by_kind(),
        }

    # -- recurring billing and receivables
    def subscribe(self, customer_id, plan_code, start=None):
        sub = self.subscriptions.subscribe(customer_id, plan_code, start)
        self.audit.record("subscribed", sub.id, plan_code)
        self.events.publish("subscribed", subscription=sub.id, customer=customer_id)
        return sub

    def bill_due_subscriptions(self, on_date=None):
        """Renew every subscription that is due and return the invoice numbers opened."""
        on_date = on_date or self.clock.today()
        numbers = []
        for sub_id in self.subscriptions.due_for_renewal(on_date):
            numbers.append(self.subscriptions.renew(sub_id))
        return numbers

    def receive_payment(self, customer_id, cents, method="transfer"):
        applied = self.ledger.apply_payment(customer_id, cents)
        fee = payments.fee_for(method, cents)
        self.audit.record("payment", customer_id, "%d via %s fee %d" % (cents, method, fee))
        return applied

    def collections(self, customer_ids, today=None):
        return self.dunning.worklist(customer_ids, today or self.clock.today())

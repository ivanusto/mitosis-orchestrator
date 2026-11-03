"""Management reports over orders, invoices and stock."""

from shop import calendar, money
from shop.textutil import render_table


def live_orders(orders):
    return [o for o in orders if o.status != "cancelled"]


def sales_by_region(orders):
    """{region: gross sales in cents} over non-cancelled orders."""
    out = {}
    for order in live_orders(orders):
        out[order.region] = out.get(order.region, 0) + order.total_cents
    return dict(sorted(out.items()))


def top_products(orders, n):
    """The n best sellers by units, ties broken by sku."""
    units = {}
    for order in live_orders(orders):
        for line in order.lines:
            units[line.sku] = units.get(line.sku, 0) + line.qty
    ranked = sorted(units.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:n]


def revenue_summary(orders):
    live = live_orders(orders)
    return {
        "orders": len(live),
        "subtotal": sum(o.subtotal_cents for o in live),
        "discounts": sum(o.discount_cents for o in live),
        "tax": sum(o.tax_cents for o in live),
        "shipping": sum(o.shipping_cents for o in live),
        "total": sum(o.total_cents for o in live),
    }


def stock_report(inventory, catalog):
    rows = []
    for product in catalog.all():
        rows.append((product.sku, product.name, inventory.on_hand(product.sku), inventory.reserved(product.sku), inventory.available(product.sku)))
    return render_table(["sku", "name", "on hand", "reserved", "available"], rows, right_align=(2, 3, 4))


def backlog_report(fulfilment):
    rows = []
    for order in fulfilment.backlog():
        rows.append((order.id, order.status, fulfilment.ship_by(order.id).isoformat(), sum(order.remaining(l.sku) for l in order.lines)))
    return render_table(["order", "status", "ship by", "units left"], rows, right_align=(3,))


def receivable_aging(invoices, today, holidays=frozenset()):
    """Open invoices bucketed by business days since issue: 0-5, 6-10, 11+."""
    buckets = {"0-5": 0, "6-10": 0, "11+": 0}
    for invoice in invoices:
        age = calendar.business_days_between(invoice.issued_on, today, holidays)
        key = "0-5" if age <= 5 else "6-10" if age <= 10 else "11+"
        buckets[key] += invoice.total
    return buckets


def money_lines(summary):
    return "\n".join("%-9s %s" % (k, money.format_cents(v) if k != "orders" else v) for k, v in summary.items())


def category_sales(orders, catalog):
    """{category: net sales in cents} over non-cancelled orders."""
    out = {}
    for order in live_orders(orders):
        for line in order.lines:
            cat = catalog.get(line.sku).category
            out[cat] = out.get(cat, 0) + line.net_cents
    return dict(sorted(out.items()))


def customer_ranking(orders, n):
    spend = {}
    for order in live_orders(orders):
        spend[order.customer_id] = spend.get(order.customer_id, 0) + order.total_cents
    return sorted(spend.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def shipping_income(orders):
    return sum(o.shipping_cents for o in live_orders(orders))


def discount_rate_bps(orders):
    live = live_orders(orders)
    gross = sum(o.subtotal_cents for o in live)
    return money.round_div(sum(o.discount_cents for o in live) * 10000, gross) if gross else 0

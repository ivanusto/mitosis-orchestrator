"""One-page operational summary assembled from the other services."""

from shop import money
from shop.textutil import render_table


def stock_alerts(inventory, catalog, threshold):
    return [(sku, inventory.available(sku)) for sku in inventory.low_stock(threshold) if catalog.has(sku)]


def receivable_lines(ledger, today):
    return ["%-8s %s" % (k, money.format_cents(v)) for k, v in ledger.aging(today).items()]


def backlog_lines(fulfilment):
    return ["%s %s" % (o.id, o.status) for o in fulfilment.backlog()]


def render(inventory, catalog, ledger, fulfilment, today, threshold=5):
    parts = []
    alerts = stock_alerts(inventory, catalog, threshold)
    parts.append("LOW STOCK")
    parts.append(render_table(["sku", "available"], alerts, right_align=(1,)) if alerts else "none")
    parts.append("RECEIVABLES")
    parts.extend(receivable_lines(ledger, today))
    parts.append("BACKLOG")
    parts.extend(backlog_lines(fulfilment) or ["empty"])
    return "\n".join(parts)

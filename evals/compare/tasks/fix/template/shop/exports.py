"""CSV and JSON exports."""

import csv
import io
import json

from shop import money

INVOICE_COLUMNS = ["invoice", "order", "sku", "qty", "amount", "discount", "tax", "shipping", "total"]


def invoices_csv(invoices):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INVOICE_COLUMNS)
    for invoice in invoices:
        for l in invoice.lines:
            writer.writerow([invoice.number, invoice.order_id, l.sku, l.qty, money.format_cents(l.amount), money.format_cents(l.discount),
                             money.format_cents(l.tax), money.format_cents(l.shipping), money.format_cents(l.total)])
    return buffer.getvalue()


def read_invoice_csv(text):
    """Parse invoices_csv output back into dict rows with integer cents."""
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        for col in ("amount", "discount", "tax", "shipping", "total"):
            row[col] = money.parse_amount(row[col])
        row["qty"] = int(row["qty"])
        rows.append(row)
    return rows


def orders_json(orders):
    payload = []
    for o in orders:
        payload.append({
            "id": o.id, "customer": o.customer_id, "status": o.status, "region": o.region,
            "subtotal": o.subtotal_cents, "discount": o.discount_cents, "tax": o.tax_cents,
            "shipping": o.shipping_cents, "total": o.total_cents,
            "lines": [{"sku": l.sku, "qty": l.qty, "net": l.net_cents} for l in o.lines],
        })
    return json.dumps(payload, indent=2, sort_keys=True)


def ledger_csv(ledger, customers):
    """Open items per customer as CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["customer", "invoice", "due", "amount", "paid", "balance"])
    for customer in sorted(customers):
        for i in ledger.open_items(customer):
            writer.writerow([customer, i.number, i.due.isoformat(), money.format_cents(i.amount), money.format_cents(i.paid), money.format_cents(i.balance)])
    return buffer.getvalue()

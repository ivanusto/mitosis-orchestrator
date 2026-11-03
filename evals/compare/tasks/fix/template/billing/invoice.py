"""Invoice assembly."""

from billing import discounts, money, tax


def build_invoice(lines, region, coupon_bps=0):
    """Return a dict of integer-cent figures for the given lines."""
    subtotal = sum(line.total for line in lines)
    volume = discounts.discount_cents(subtotal)
    coupon = discounts.apply_coupon(subtotal - volume, coupon_bps)
    taxable = subtotal - volume - coupon
    due_tax = tax.tax_cents(taxable, region)
    return {
        "subtotal": subtotal,
        "volume_discount": volume,
        "coupon": coupon,
        "taxable": taxable,
        "tax": due_tax,
        "total": taxable + due_tax,
        "region": region,
    }


def prorate(total_cents, days_used, days_in_period):
    """Part of a period charge that falls on days_used of days_in_period."""
    return money.round_div(total_cents * days_used, days_in_period)


def credit_note(invoice):
    """Mirror image of an invoice: every money figure negated."""
    out = dict(invoice)
    for key in ("subtotal", "volume_discount", "coupon", "taxable", "tax", "total"):
        out[key] = -invoice[key]
    return out

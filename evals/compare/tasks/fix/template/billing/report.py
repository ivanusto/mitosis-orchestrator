"""Reports over several invoices."""

from billing import formatting, money


def allocate(total_cents, weights):
    """Split total_cents in proportion to weights; the last share absorbs the remainder."""
    weight_sum = sum(weights)
    shares = []
    for weight in weights[:-1]:
        shares.append(money.round_div(total_cents * weight, weight_sum))
    shares.append(total_cents - sum(shares))
    return shares


def summarize(invoices):
    return {
        "count": len(invoices),
        "subtotal": sum(i["subtotal"] for i in invoices),
        "tax": sum(i["tax"] for i in invoices),
        "total": sum(i["total"] for i in invoices),
    }


def render_report(invoices):
    s = summarize(invoices)
    return "\n".join([
        "invoices: %d" % s["count"],
        "subtotal: %s" % formatting.format_cents(s["subtotal"]),
        "tax: %s" % formatting.format_cents(s["tax"]),
        "total: %s" % formatting.format_cents(s["total"]),
    ])

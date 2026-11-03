"""Sales analytics across currencies, periods and customer cohorts."""

import datetime

from shop import calendar, currency, money


def monthly_revenue(orders):
    """{(year, month): order totals in cents} over non-cancelled orders, by creation date."""
    out = {}
    for o in orders:
        if o.status == "cancelled":
            continue
        key = (o.created_at.year, o.created_at.month)
        out[key] = out.get(key, 0) + o.total_cents
    return dict(sorted(out.items()))


def quarterly_revenue(orders):
    out = {}
    for (year, month), cents in monthly_revenue(orders).items():
        key = (year, calendar.quarter_of(datetime.date(year, month, 1)))
        out[key] = out.get(key, 0) + cents
    return dict(sorted(out.items()))


def revenue_in(orders_by_currency, dst, table=None):
    """Combine {currency: [orders]} into one figure in dst."""
    totals = {}
    for code, orders in orders_by_currency.items():
        totals[code] = sum(o.total_cents for o in orders if o.status != "cancelled")
    return currency.total_in(totals, dst, table)


def average_order_value(orders):
    live = [o for o in orders if o.status != "cancelled"]
    return money.round_div(sum(o.total_cents for o in live), len(live)) if live else 0


def growth_bps(previous, current):
    """Change from previous to current in basis points; None when there is no baseline."""
    if previous == 0:
        return None
    return money.round_div((current - previous) * 10000, previous)


def repeat_customers(orders):
    seen = {}
    for o in orders:
        if o.status != "cancelled":
            seen[o.customer_id] = seen.get(o.customer_id, 0) + 1
    return sorted(c for c, n in seen.items() if n > 1)


def cohort_retention(orders, cohort_month):
    """Share (basis points) of customers first seen in cohort_month who ordered again later."""
    first = {}
    later = set()
    for o in sorted(orders, key=lambda o: (o.created_at, o.id)):
        key = (o.created_at.year, o.created_at.month)
        if o.customer_id not in first:
            first[o.customer_id] = key
        elif key > first[o.customer_id]:
            later.add(o.customer_id)
    members = [c for c, k in first.items() if k == cohort_month]
    if not members:
        return 0
    return len([c for c in members if c in later]) * 10000 // len(members)

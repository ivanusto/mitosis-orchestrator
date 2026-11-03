"""Sales tax by region."""

from shop import money
from shop.errors import UnknownRegion

RATES_BPS = {
    "NL": 2100,
    "DE": 1900,
    "FR": 2000,
    "UK": 2000,
    "US-CA": 725,
    "US-OR": 0,
}


def rate_for(region):
    try:
        return RATES_BPS[region]
    except KeyError:
        raise UnknownRegion(region)


def tax_for_amount(amount_cents, region, taxable=True):
    if not taxable:
        return 0
    return money.percent(amount_cents, rate_for(region))


def gross(amount_cents, region, taxable=True):
    return amount_cents + tax_for_amount(amount_cents, region, taxable)


def net_from_gross(gross_cents, region):
    """Invert gross() for a taxable amount, rounding to the nearest cent."""
    return money.round_div(gross_cents * 10000, 10000 + rate_for(region))


def summarize_collected(rows):
    """rows are (region, taxable amount, tax charged); returns {region: (taxable, tax, effective bps)}."""
    out = {}
    for region, amount, charged in rows:
        a, t = out.get(region, (0, 0))
        out[region] = (a + amount, t + charged)
    return {r: (a, t, money.round_div(t * 10000, a) if a else 0) for r, (a, t) in sorted(out.items())}


def expected_vs_charged(rows):
    """Per region, the difference between tax recomputed from the rates and tax actually charged."""
    return {r: sum(tax_for_amount(a, r) - c for rr, a, c in rows if rr == r) for r in sorted({row[0] for row in rows})}

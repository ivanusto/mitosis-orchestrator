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

"""Static rate tables."""

TAX_BPS = {
    "NL": 2100,
    "DE": 1900,
    "FR": 2000,
    "UK": 2000,
    "US-CA": 725,
    "US-OR": 0,
}

# (minimum subtotal in cents, discount in basis points), highest threshold first
DISCOUNT_TIERS = [
    (100000, 1000),
    (50000, 500),
    (0, 0),
]


class UnknownRegion(KeyError):
    pass


def rate_for(region):
    try:
        return TAX_BPS[region]
    except KeyError:
        raise UnknownRegion(region)


def discount_bps(subtotal_cents):
    for threshold, bps in DISCOUNT_TIERS:
        if subtotal_cents >= threshold:
            return bps
    return 0

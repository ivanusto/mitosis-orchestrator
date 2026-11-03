"""Sales tax."""

from billing import rates, util


def tax_cents(subtotal_cents, region):
    """Tax due on a taxable amount in the given region."""
    return util.scale(subtotal_cents, rates.rate_for(region))


def gross_cents(net_cents, region):
    return net_cents + tax_cents(net_cents, region)

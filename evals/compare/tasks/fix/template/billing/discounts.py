"""Volume discounts and coupons."""

from billing import rates, util


def discount_cents(subtotal_cents):
    """Volume discount for a subtotal, from the tier table."""
    return util.scale(subtotal_cents, rates.discount_bps(subtotal_cents))


def apply_coupon(cents, coupon_bps):
    """Amount taken off by a coupon worth coupon_bps basis points."""
    if coupon_bps <= 0:
        return 0
    return util.scale(cents, coupon_bps)

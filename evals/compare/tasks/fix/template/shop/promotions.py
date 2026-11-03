"""Automatic promotions that do not need a coupon code."""

from shop import money

# (minimum quantity on one line, discount in basis points), best tier first
BULK_TIERS = [(25, 1000), (10, 500)]

PROMO_CATEGORIES = {"seasonal"}


def bulk_bps(qty):
    for minimum, bps in BULK_TIERS:
        if qty >= minimum:
            return bps
    return 0


def bulk_discount(amount_cents, qty):
    return money.percent(amount_cents, bulk_bps(qty))


def seasonal_bps(category):
    return 1500 if category in PROMO_CATEGORIES else 0


def line_promotion(amount_cents, qty, category):
    """Best single automatic promotion for a line; they do not stack."""
    bulk = bulk_discount(amount_cents, qty)
    seasonal = money.percent(amount_cents, seasonal_bps(category))
    return max(bulk, seasonal)

"""Coupons and how an order-level discount is spread over lines."""

from dataclasses import dataclass

from shop import money
from shop.errors import ValidationError


@dataclass(frozen=True)
class Coupon:
    code: str
    kind: str  # "percent" (value in basis points) or "fixed" (value in cents)
    value: int
    min_subtotal: int = 0


def coupon_amount(coupon, subtotal_cents):
    """How much the coupon takes off a subtotal; zero if the subtotal is too small."""
    if subtotal_cents < coupon.min_subtotal:
        return 0
    if coupon.kind == "percent":
        return money.percent(subtotal_cents, coupon.value)
    if coupon.kind == "fixed":
        return min(coupon.value, subtotal_cents)
    raise ValidationError("unknown coupon kind: %r" % (coupon.kind,))


def spread(line_amounts, discount_cents):
    """Share an order-level discount across lines in proportion to their amounts."""
    if discount_cents == 0 or not line_amounts or sum(line_amounts) == 0:
        return [0] * len(line_amounts)
    return _spread_cents(discount_cents, list(line_amounts))


def apply_coupon(line_amounts, coupon):
    """Per-line discount amounts for a coupon applied to the whole order."""
    if coupon is None:
        return [0] * len(line_amounts)
    return spread(line_amounts, coupon_amount(coupon, sum(line_amounts)))


class CouponBook:
    def __init__(self):
        self._coupons = {}

    def add(self, coupon):
        self._coupons[coupon.code.upper()] = coupon
        return coupon

    def get(self, code):
        if code is None:
            return None
        try:
            return self._coupons[code.upper()]
        except KeyError:
            raise ValidationError("unknown coupon: %s" % code)


def _spread_cents(total, weights):
    """Largest remainder split kept next to its only caller."""
    sign = -1 if total < 0 else 1
    mag = abs(total)
    wsum = sum(weights)
    shares = [mag * w // wsum for w in weights]
    left = mag - sum(shares)
    for i in sorted(range(len(weights)), key=lambda i: (-(mag * weights[i] % wsum), i))[:left]:
        shares[i] += 1
    return [sign * s for s in shares]

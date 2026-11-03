"""Shared arithmetic on basis points."""

from billing import money

BPS = 10000


def scale(cents, bps):
    """Return cents * bps / 10000 as whole cents."""
    return money.round_div(cents * bps, BPS)


def clamp(value, low, high):
    return max(low, min(high, value))

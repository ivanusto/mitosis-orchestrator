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


class Campaign:
    """A promotion that only runs between two dates and only for some categories."""

    def __init__(self, name, start, end, bps, categories=()):
        if end < start:
            raise ValueError("campaign ends before it starts")
        self.name, self.start, self.end, self.bps, self.categories = name, start, end, bps, frozenset(categories)

    def active_on(self, day):
        return self.start <= day <= self.end

    def applies(self, day, category):
        return self.active_on(day) and (not self.categories or category in self.categories)


def best_campaign_bps(campaigns, day, category):
    """The single best running campaign for a category (campaigns do not stack)."""
    return max([c.bps for c in campaigns if c.applies(day, category)] or [0])


def campaign_discount(amount_cents, campaigns, day, category):
    return money.percent(amount_cents, best_campaign_bps(campaigns, day, category))


def calendar_of(campaigns):
    """[(name, start, end)] sorted by start date, for display."""
    return sorted(((c.name, c.start, c.end) for c in campaigns), key=lambda t: (t[1], t[0]))

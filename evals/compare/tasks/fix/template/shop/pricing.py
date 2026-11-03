"""Unit prices and line amounts."""

from shop import money, validate

TIER_BPS = {"standard": 0, "silver": 300, "gold": 500}


def unit_price(product, tier="standard"):
    """List price less the tier discount, in cents."""
    bps = TIER_BPS[tier]
    return product.price_cents - money.percent(product.price_cents, bps)


def line_amount(product, qty, tier="standard"):
    validate.require_positive_int(qty, "qty")
    return unit_price(product, tier) * qty


def price_list(catalog, tier="standard"):
    return {p.sku: unit_price(p, tier) for p in catalog.all()}


def cheapest(catalog, category=None):
    products = catalog.by_category(category) if category else catalog.all()
    return min(products, key=lambda p: (p.price_cents, p.sku)) if products else None

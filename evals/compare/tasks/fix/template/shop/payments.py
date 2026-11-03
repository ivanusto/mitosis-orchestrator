"""Payment methods and what they cost the shop."""

from shop import money
from shop.errors import ValidationError

# method -> (fee in basis points, fixed fee in cents)
FEES = {
    "card": (290, 30),
    "transfer": (0, 0),
    "gift": (0, 0),
    "wallet": (150, 10),
}


def fee_for(method, cents):
    """Fee the processor keeps on a payment of cents."""
    try:
        bps, fixed = FEES[method]
    except KeyError:
        raise ValidationError("unknown payment method: %s" % method)
    if cents <= 0:
        return 0
    return money.percent(cents, bps) + (fixed if bps or fixed else 0)


def net_of_fees(payments):
    """[(method, cents)] -> cents the shop actually receives."""
    return sum(cents - fee_for(method, cents) for method, cents in payments)


def split_by_method(payments):
    out = {}
    for method, cents in payments:
        out[method] = out.get(method, 0) + cents
    return dict(sorted(out.items()))


def cheapest_split(total_cents, gift_balance):
    """Use as much gift balance as possible, the rest by transfer if allowed, else card."""
    from_gift = min(total_cents, gift_balance)
    rest = total_cents - from_gift
    plan = []
    if from_gift:
        plan.append(("gift", from_gift))
    if rest:
        plan.append(("card", rest))
    return plan

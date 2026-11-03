"""What a return costs the customer, by reason."""

from shop import money
from shop.errors import ValidationError

# reason -> (restocking fee in basis points, customer pays the return shipping)
POLICY = {
    "defective": (0, False),
    "wrong_item": (0, False),
    "changed_mind": (1000, True),
    "too_late": (2500, True),
}


def restocking_fee(reason, refund_cents):
    try:
        bps, _ = POLICY[reason]
    except KeyError:
        raise ValidationError("unknown return reason: %s" % reason)
    return money.percent(refund_cents, bps)


def customer_pays_shipping(reason):
    return POLICY[reason][1]


def net_refund(reason, refund_cents, return_shipping_cents=0):
    fee = restocking_fee(reason, refund_cents)
    shipping = return_shipping_cents if customer_pays_shipping(reason) else 0
    return max(0, refund_cents - fee - shipping)


def classify(days_since_delivery, defective=False, wrong_item=False):
    if defective:
        return "defective"
    if wrong_item:
        return "wrong_item"
    return "changed_mind" if days_since_delivery <= 14 else "too_late"

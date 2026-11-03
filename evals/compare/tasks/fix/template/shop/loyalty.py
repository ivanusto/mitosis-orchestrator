"""Loyalty points earned on spend and redeemed against later orders."""

from shop import validate
from shop.errors import InvalidState

MULTIPLIER = {"standard": 1, "silver": 2, "gold": 3}
CENTS_PER_POINT = 2


def points_for(spend_cents, tier):
    """One point per whole 100 cents spent, times the tier multiplier."""
    return (max(spend_cents, 0) // 100) * MULTIPLIER[tier]


class LoyaltyBook:
    def __init__(self):
        self._balance = {}
        self._history = []

    def balance(self, customer_id):
        return self._balance.get(customer_id, 0)

    def earn(self, customer_id, spend_cents, tier, reference):
        points = points_for(spend_cents, tier)
        self._balance[customer_id] = self.balance(customer_id) + points
        self._history.append((customer_id, reference, points))
        return points

    def redeem(self, customer_id, points):
        validate.require_positive_int(points, "points")
        if points > self.balance(customer_id):
            raise InvalidState("only %d points available" % self.balance(customer_id))
        self._balance[customer_id] = self.balance(customer_id) - points
        self._history.append((customer_id, "redeem", -points))
        return points * CENTS_PER_POINT

    def reverse(self, customer_id, reference):
        """Take back the points an order earned (for example when it is cancelled)."""
        total = sum(p for c, r, p in self._history if c == customer_id and r == reference and p > 0)
        self._balance[customer_id] = max(0, self.balance(customer_id) - total)
        self._history.append((customer_id, "reverse:" + reference, -total))
        return total

    def history(self, customer_id):
        return [(r, p) for c, r, p in self._history if c == customer_id]

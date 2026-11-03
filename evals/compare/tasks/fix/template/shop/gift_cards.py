"""Gift cards: balances, redemption and expiry."""

from shop import calendar, validate
from shop.errors import InvalidState, NotFound

VALID_MONTHS = 12


class GiftCardBook:
    def __init__(self, clock):
        self.clock = clock
        self._cards = {}

    def issue(self, code, cents):
        validate.require_positive_int(cents, "cents")
        if code in self._cards:
            raise InvalidState("duplicate gift card %s" % code)
        today = self.clock.today()
        self._cards[code] = {"balance": cents, "expires": calendar.add_months(today, VALID_MONTHS), "issued": cents}
        return self._cards[code]["expires"]

    def _card(self, code):
        if code not in self._cards:
            raise NotFound("gift card", code)
        return self._cards[code]

    def balance(self, code):
        card = self._card(code)
        return 0 if self.clock.today() > card["expires"] else card["balance"]

    def redeem(self, code, cents):
        validate.require_positive_int(cents, "cents")
        if cents > self.balance(code):
            raise InvalidState("gift card %s holds %d" % (code, self.balance(code)))
        self._card(code)["balance"] -= cents
        return cents

    def top_up(self, code, cents):
        card = self._card(code)
        card["balance"] += cents
        card["expires"] = calendar.add_months(self.clock.today(), VALID_MONTHS)
        return card["expires"]

    def expiring_before(self, day):
        return sorted(c for c, card in self._cards.items() if card["expires"] < day and card["balance"] > 0)

    def liability(self):
        """Money owed to holders of unexpired cards."""
        today = self.clock.today()
        return sum(card["balance"] for card in self._cards.values() if card["expires"] >= today)

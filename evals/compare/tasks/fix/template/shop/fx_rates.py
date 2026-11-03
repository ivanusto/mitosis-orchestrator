"""Exchange rates stored as integer micro-units per one euro."""

from shop.errors import ValidationError

MICRO = 1000000

DEFAULT_RATES = {
    "EUR": 1000000,
    "USD": 1085000,
    "GBP": 856000,
    "CHF": 962000,
    "SEK": 11230000,
}


class RateTable:
    def __init__(self, rates=None):
        self._rates = dict(DEFAULT_RATES if rates is None else rates)

    def set_rate(self, code, micro_per_eur):
        if micro_per_eur <= 0:
            raise ValidationError("rate for %s must be positive" % code)
        self._rates[code] = micro_per_eur

    def get(self, code):
        try:
            return self._rates[code]
        except KeyError:
            raise ValidationError("no rate for currency %s" % code)

    def supported(self):
        return sorted(self._rates)

    def cross_rate_micro(self, src, dst):
        """How many micro-units of dst one unit of src is worth."""
        return self.get(dst) * MICRO // self.get(src)

    def inverse_pair_error(self, src, dst):
        """Rounding drift in micro-units when converting there and back; a sanity check for new rates."""
        there = self.cross_rate_micro(src, dst)
        back = self.cross_rate_micro(dst, src)
        return abs(there * back // MICRO - MICRO)

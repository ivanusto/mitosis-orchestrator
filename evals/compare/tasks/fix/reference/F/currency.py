"""Currency conversion between the supported currencies, via euro."""

from shop import money
from shop.fx_rates import RateTable

DEFAULT_TABLE = RateTable()


def convert(cents, src, dst, table=None):
    """Convert an amount in cents from src to dst, rounded to the nearest cent (halves away from zero)."""
    table = table or DEFAULT_TABLE
    if src == dst:
        return cents
    return money.round_div(cents * table.get(dst), table.get(src))


def format_amount(cents, code):
    symbols = {"EUR": "EUR ", "USD": "USD ", "GBP": "GBP ", "CHF": "CHF ", "SEK": "SEK "}
    return symbols.get(code, code + " ") + money.format_cents(cents)


def convert_many(amounts, src, dst, table=None):
    return [convert(a, src, dst, table) for a in amounts]


def total_in(amounts_by_currency, dst, table=None):
    """Sum a {currency: cents} mapping in dst, converting each currency once."""
    return sum(convert(cents, code, dst, table) for code, cents in sorted(amounts_by_currency.items()))

"""Integer-cent money helpers."""


def parse_amount(text):
    """Parse a decimal string such as '12.34' or '-0.5' into integer cents.

    Digits beyond the second decimal place are ignored.
    """
    s = text.strip()
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    return sign * (int(whole or "0") * 100 + int(frac))


def round_div(num, den):
    """Divide num by den and round the result to an integer, halves away from zero."""
    if den == 0:
        raise ZeroDivisionError("round_div by zero")
    sign = -1 if (num < 0) != (den < 0) else 1
    n, d = abs(num), abs(den)
    q, r = divmod(n, d)
    if r * 2 > d:
        q += 1
    return sign * q

"""Integer-cent money helpers. No floats anywhere in this module."""


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


def format_cents(cents, symbol=""):
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return "%s%s%d.%02d" % (sign, symbol, whole, frac)


def round_div(num, den):
    """Divide num by den and round to an integer, halves away from zero."""
    if den == 0:
        raise ZeroDivisionError("round_div by zero")
    sign = -1 if (num < 0) != (den < 0) else 1
    n, d = abs(num), abs(den)
    q, r = divmod(n, d)
    if r * 2 >= d:
        q += 1
    return sign * q


def percent(cents, bps):
    """cents * bps / 10000, rounded to whole cents."""
    return round_div(cents * bps, 10000)


def allocate(total, weights):
    """Split total cents in proportion to weights so that the parts add up to total exactly.

    Every part gets the floor of its exact share; the cents left over are handed out one
    each, starting from the first part.
    """
    if not weights:
        raise ValueError("allocate needs at least one weight")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("weights must be non-negative and not all zero")
    weight_sum = sum(weights)
    shares = [total * w // weight_sum for w in weights]
    leftover = total - sum(shares)
    for i in range(leftover):
        shares[i] += 1
    return shares


def split_evenly(total, parts):
    if parts <= 0:
        raise ValueError("parts must be positive")
    return allocate(total, [1] * parts)


def sum_cents(values):
    return sum(values)

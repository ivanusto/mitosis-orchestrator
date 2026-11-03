"""Weight units. Everything inside the package is stored in whole grams."""

import re

GRAMS_PER = {
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.349523125,
    "lb": 453.59237,
}

_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)\s*$")


def to_grams(value, unit):
    """Convert a quantity to whole grams, rounding half up."""
    try:
        factor = GRAMS_PER[unit]
    except KeyError:
        raise ValueError("unknown weight unit: %r" % (unit,))
    return int(value * factor + 0.5)


def parse_weight(text):
    """Parse '1.5kg', '12 oz' or '300g' into grams."""
    match = _PATTERN.match(text.lower())
    if not match:
        raise ValueError("cannot parse weight: %r" % (text,))
    return to_grams(float(match.group(1)), match.group(2))


def format_weight(grams):
    if grams >= 1000:
        kilos, rest = divmod(grams, 1000)
        return "%d.%03d kg" % (kilos, rest)
    return "%d g" % grams


def total_weight(items):
    """Sum (grams, quantity) pairs."""
    return sum(grams * qty for grams, qty in items)

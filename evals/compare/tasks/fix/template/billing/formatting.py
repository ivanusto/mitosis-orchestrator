"""Display helpers; no arithmetic on money beyond splitting cents."""


def format_cents(cents):
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return "%s%d.%02d" % (sign, whole, frac)

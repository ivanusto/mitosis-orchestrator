"""Sequential identifier generation."""


class IdGenerator:
    def __init__(self, prefix, start=1, width=4):
        self.prefix = prefix
        self._next = start
        self.width = width

    def next(self):
        value = "%s-%0*d" % (self.prefix, self.width, self._next)
        self._next += 1
        return value

    def peek(self):
        return "%s-%0*d" % (self.prefix, self.width, self._next)


def parse_id(text):
    """Split 'ORD-0042' into ('ORD', 42)."""
    prefix, _, number = text.rpartition("-")
    if not prefix or not number.isdigit():
        raise ValueError("not an id: %r" % (text,))
    return prefix, int(number)

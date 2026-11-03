"""A tiny in-memory repository used by the catalog, customer book and order service."""

from shop.errors import NotFound


class Repository:
    def __init__(self, kind):
        self.kind = kind
        self._items = {}

    def add(self, key, value):
        if key in self._items:
            raise KeyError("duplicate %s: %s" % (self.kind, key))
        self._items[key] = value
        return value

    def put(self, key, value):
        self._items[key] = value
        return value

    def get(self, key):
        try:
            return self._items[key]
        except KeyError:
            raise NotFound(self.kind, key)

    def has(self, key):
        return key in self._items

    def remove(self, key):
        try:
            return self._items.pop(key)
        except KeyError:
            raise NotFound(self.kind, key)

    def all(self):
        return [self._items[k] for k in sorted(self._items)]

    def find(self, predicate):
        return [v for v in self.all() if predicate(v)]

    def __len__(self):
        return len(self._items)


def group_by(items, key):
    out = {}
    for item in items:
        out.setdefault(key(item), []).append(item)
    return out

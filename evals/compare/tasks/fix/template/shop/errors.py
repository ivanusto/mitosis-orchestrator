"""Exception types shared by every layer."""


class ShopError(Exception):
    """Base class for all errors raised on purpose by this package."""


class ValidationError(ShopError):
    pass


class NotFound(ShopError):
    def __init__(self, kind, key):
        super().__init__("%s not found: %s" % (kind, key))
        self.kind = kind
        self.key = key


class OutOfStock(ShopError):
    def __init__(self, sku, wanted, available):
        super().__init__("%s: wanted %d, available %d" % (sku, wanted, available))
        self.sku = sku
        self.wanted = wanted
        self.available = available


class OverweightError(ShopError):
    pass


class InvalidState(ShopError):
    pass


class UnknownRegion(ShopError):
    pass


class ReturnWindowClosed(ShopError):
    pass

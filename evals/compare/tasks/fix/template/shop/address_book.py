"""Saved delivery addresses per customer."""

from shop.addresses import Address
from shop.errors import NotFound, ValidationError


class AddressBook:
    def __init__(self):
        self._by_customer = {}
        self._default = {}

    def add(self, customer_id, label, address, make_default=False):
        if not isinstance(address, Address):
            raise ValidationError("address must be an Address")
        address.validated()
        book = self._by_customer.setdefault(customer_id, {})
        if label in book:
            raise ValidationError("customer %s already has an address called %s" % (customer_id, label))
        book[label] = address
        if make_default or customer_id not in self._default:
            self._default[customer_id] = label
        return address

    def get(self, customer_id, label=None):
        book = self._by_customer.get(customer_id, {})
        label = label or self._default.get(customer_id)
        if label not in book:
            raise NotFound("address", "%s/%s" % (customer_id, label))
        return book[label]

    def labels(self, customer_id):
        return sorted(self._by_customer.get(customer_id, {}))

    def remove(self, customer_id, label):
        book = self._by_customer.get(customer_id, {})
        if label not in book:
            raise NotFound("address", "%s/%s" % (customer_id, label))
        del book[label]
        if self._default.get(customer_id) == label:
            rest = sorted(book)
            if rest:
                self._default[customer_id] = rest[0]
            else:
                del self._default[customer_id]

    def default_label(self, customer_id):
        return self._default.get(customer_id)

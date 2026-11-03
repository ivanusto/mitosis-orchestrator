"""Customers and loyalty tiers."""

from dataclasses import dataclass, replace

from shop import validate
from shop.addresses import zone_for
from shop.store import Repository

TIERS = ("standard", "silver", "gold")


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    email: str
    region: str
    tier: str = "standard"

    @property
    def zone(self):
        return zone_for(self.region)


class CustomerBook:
    def __init__(self):
        self._repo = Repository("customer")

    def add(self, customer):
        validate.require_text(customer.name, "name")
        validate.require_email(customer.email)
        validate.require_in(customer.tier, TIERS, "tier")
        zone_for(customer.region)
        return self._repo.add(customer.id, customer)

    def get(self, customer_id):
        return self._repo.get(customer_id)

    def all(self):
        return self._repo.all()

    def promote(self, customer_id):
        customer = self._repo.get(customer_id)
        position = TIERS.index(customer.tier)
        if position + 1 < len(TIERS):
            customer = replace(customer, tier=TIERS[position + 1])
            self._repo.put(customer_id, customer)
        return customer

    def in_region(self, region):
        return self._repo.find(lambda c: c.region == region)


def segment(spend_cents, orders_count):
    """Marketing segment from lifetime spend and order count."""
    if orders_count == 0:
        return "prospect"
    if spend_cents >= 100000 or orders_count >= 20:
        return "vip"
    if orders_count >= 3:
        return "regular"
    return "new"


def segments(orders):
    """{customer id: segment} from non-cancelled orders."""
    spend, count = {}, {}
    for o in orders:
        if o.status == "cancelled":
            continue
        spend[o.customer_id] = spend.get(o.customer_id, 0) + o.total_cents
        count[o.customer_id] = count.get(o.customer_id, 0) + 1
    return {c: segment(spend[c], count[c]) for c in sorted(spend)}


def suggested_tier(spend_cents):
    """Which loyalty tier a customer's spend qualifies for."""
    if spend_cents >= 250000:
        return "gold"
    if spend_cents >= 100000:
        return "silver"
    return "standard"

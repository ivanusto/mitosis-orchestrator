"""Customer-specific tax treatment on top of the regional rates."""

from dataclasses import dataclass

from shop import tax

EXEMPT_CATEGORIES = {"books"}


@dataclass(frozen=True)
class TaxProfile:
    customer_id: str
    exempt: bool = False
    business_vat_id: str = ""
    region_override: str = ""


class TaxRules:
    def __init__(self):
        self._profiles = {}

    def set_profile(self, profile):
        self._profiles[profile.customer_id] = profile

    def profile(self, customer_id):
        return self._profiles.get(customer_id, TaxProfile(customer_id))

    def region_for(self, customer_id, default_region):
        return self.profile(customer_id).region_override or default_region

    def reverse_charge(self, customer_id, seller_region, buyer_region):
        """Cross-border business sales inside one VAT area carry no tax; the buyer accounts for it."""
        p = self.profile(customer_id)
        eu = {"NL", "DE", "FR"}
        return bool(p.business_vat_id) and seller_region != buyer_region and seller_region in eu and buyer_region in eu

    def tax_for_line(self, customer_id, amount_cents, region, category="general", taxable=True, seller_region="NL"):
        p = self.profile(customer_id)
        if p.exempt or category in EXEMPT_CATEGORIES:
            return 0
        region = self.region_for(customer_id, region)
        if self.reverse_charge(customer_id, seller_region, region):
            return 0
        return tax.tax_for_amount(amount_cents, region, taxable)

    def exempt_customers(self):
        return sorted(c for c, p in self._profiles.items() if p.exempt)

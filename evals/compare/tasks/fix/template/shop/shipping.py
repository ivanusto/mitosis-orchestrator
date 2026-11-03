"""Shipping tiers, quotes and delivery dates."""

import bisect

from shop import calendar
from shop.errors import OverweightError

# per zone: (heaviest parcel in grams that fits the tier, price in cents), lightest first
TIERS = {
    "EU": [(500, 495), (2000, 795), (5000, 1295), (10000, 1995)],
    "UK": [(500, 595), (2000, 895), (5000, 1495), (10000, 2295)],
    "US": [(500, 795), (2000, 1195), (5000, 1895), (10000, 2995)],
}

FREE_OVER_CENTS = {"EU": 10000, "UK": 12000, "US": 15000}

TRANSIT_DAYS = {"EU": 3, "UK": 4, "US": 6}


def max_weight(zone):
    return TIERS[zone][-1][0]


def tier_for(zone, weight_g):
    """The (limit, price) tier a parcel of weight_g falls into."""
    tiers = TIERS[zone]
    limits = [limit for limit, _ in tiers]
    if weight_g > limits[-1]:
        raise OverweightError("%d g is over the %d g limit for zone %s" % (weight_g, limits[-1], zone))
    return tiers[bisect.bisect_right(limits, weight_g)]


def quote(zone, weight_g, subtotal_cents):
    """Shipping charge for an order: free above the zone threshold, else by weight tier."""
    if subtotal_cents >= FREE_OVER_CENTS[zone]:
        return 0
    return tier_for(zone, weight_g)[1]


def delivery_date(zone, shipped_on, holidays=frozenset()):
    return calendar.add_business_days(shipped_on, TRANSIT_DAYS[zone], holidays)


def split_parcels(zone, weights):
    """Greedy first-fit packing of item weights into parcels no heavier than the zone limit."""
    limit = max_weight(zone)
    parcels = []
    for weight in sorted(weights, reverse=True):
        if weight > limit:
            raise OverweightError("single item of %d g exceeds %d g" % (weight, limit))
        for parcel in parcels:
            if sum(parcel) + weight <= limit:
                parcel.append(weight)
                break
        else:
            parcels.append([weight])
    return parcels

"""Regions, shipping zones and postcodes."""

import re
from dataclasses import dataclass

from shop.errors import UnknownRegion, ValidationError

ZONES = {
    "NL": "EU",
    "DE": "EU",
    "FR": "EU",
    "UK": "UK",
    "US-CA": "US",
    "US-OR": "US",
}

_POSTCODES = {
    "NL": re.compile(r"^[0-9]{4} ?[A-Z]{2}$"),
    "DE": re.compile(r"^[0-9]{5}$"),
    "FR": re.compile(r"^[0-9]{5}$"),
    "UK": re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$"),
    "US-CA": re.compile(r"^9[0-6][0-9]{3}$"),
    "US-OR": re.compile(r"^97[0-9]{3}$"),
}


@dataclass(frozen=True)
class Address:
    line1: str
    city: str
    postcode: str
    region: str

    def validated(self):
        validate_postcode(self.region, self.postcode)
        return self


def zone_for(region):
    try:
        return ZONES[region]
    except KeyError:
        raise UnknownRegion(region)


def validate_postcode(region, postcode):
    pattern = _POSTCODES.get(region)
    if pattern is None:
        raise UnknownRegion(region)
    if not pattern.match(postcode.strip().upper()):
        raise ValidationError("bad postcode %r for %s" % (postcode, region))
    return postcode.strip().upper()


def regions_in_zone(zone):
    return sorted(r for r, z in ZONES.items() if z == zone)

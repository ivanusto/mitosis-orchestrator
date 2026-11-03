"""Carriers, their service levels and daily cut-off times."""

import datetime
from dataclasses import dataclass

from shop import calendar
from shop.errors import ValidationError


@dataclass(frozen=True)
class Service:
    carrier: str
    level: str
    cutoff: datetime.time
    transit_business_days: int
    surcharge_cents: int = 0
    zones: tuple = ("EU",)


SERVICES = [
    Service("PostNL", "standard", datetime.time(16, 0), 3),
    Service("PostNL", "express", datetime.time(13, 0), 1, 450),
    Service("DHL", "standard", datetime.time(17, 30), 4, 0, ("EU", "UK")),
    Service("DHL", "express", datetime.time(15, 0), 2, 900, ("EU", "UK", "US")),
    Service("UPS", "saver", datetime.time(18, 0), 6, 0, ("US",)),
]


def services_for(zone):
    return [s for s in SERVICES if zone in s.zones]


def find(carrier, level):
    for s in SERVICES:
        if s.carrier == carrier and s.level == level:
            return s
    raise ValidationError("no such service: %s %s" % (carrier, level))


def handover_date(service, handed_over_at, holidays=frozenset()):
    """The business day the carrier counts as day zero: orders after the cut-off wait for the next one."""
    day = handed_over_at.date()
    if handed_over_at.time() > service.cutoff or not calendar.is_business_day(day, holidays):
        day = calendar.next_business_day(day + calendar.ONE_DAY, holidays)
    return day


def delivery_estimate(service, handed_over_at, holidays=frozenset()):
    return calendar.add_business_days(handover_date(service, handed_over_at, holidays), service.transit_business_days, holidays)


def cheapest_by(zone, deadline, handed_over_at, holidays=frozenset()):
    """The cheapest service for zone that still arrives on or before deadline."""
    fits = [s for s in services_for(zone) if delivery_estimate(s, handed_over_at, holidays) <= deadline]
    return min(fits, key=lambda s: (s.surcharge_cents, s.transit_business_days, s.carrier)) if fits else None

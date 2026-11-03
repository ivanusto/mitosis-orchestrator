"""Delivery promises measured against what actually happened."""

from shop import calendar

PROMISE_BUSINESS_DAYS = {"EU": 5, "UK": 6, "US": 9}


def promised_date(zone, paid_on, holidays=frozenset()):
    return calendar.add_business_days(paid_on, PROMISE_BUSINESS_DAYS[zone], holidays)


def days_late(promised, delivered):
    """Business days between promise and delivery; zero when on time."""
    return max(0, calendar.business_days_between(promised, delivered))


def compensation_bps(late_days):
    if late_days <= 0:
        return 0
    if late_days <= 2:
        return 500
    if late_days <= 5:
        return 1000
    return 2000


def evaluate(zone, paid_on, delivered_on, holidays=frozenset()):
    promised = promised_date(zone, paid_on, holidays)
    late = days_late(promised, delivered_on)
    return {"promised": promised, "late_days": late, "compensation_bps": compensation_bps(late)}


def summarize(results):
    """Share of on-time deliveries in basis points, and the worst delay."""
    if not results:
        return {"on_time_bps": 0, "worst": 0, "count": 0}
    on_time = sum(1 for r in results if r["late_days"] == 0)
    return {"on_time_bps": on_time * 10000 // len(results), "worst": max(r["late_days"] for r in results), "count": len(results)}

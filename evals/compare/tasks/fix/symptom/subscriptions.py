"""Recurring plans billed on the day of the month they started."""

import datetime
from dataclasses import dataclass, field

from shop import calendar, money
from shop.errors import InvalidState, NotFound, ValidationError
from shop.ids import IdGenerator

PAYMENT_TERMS_DAYS = 14


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price_cents: int
    interval_months: int
    trial_days: int = 0


@dataclass
class Subscription:
    id: str
    customer_id: str
    plan: Plan
    start: datetime.date
    status: str = "active"
    renewals: int = 0
    cancelled_on: object = None
    invoices: list = field(default_factory=list)


class SubscriptionService:
    def __init__(self, ledger, clock):
        self.ledger = ledger
        self.clock = clock
        self._plans = {}
        self._subs = {}
        self._ids = IdGenerator("SUB")
        self._invoice_ids = IdGenerator("SIN", width=5)

    def add_plan(self, plan):
        if plan.interval_months not in (1, 3, 12):
            raise ValidationError("interval must be 1, 3 or 12 months")
        self._plans[plan.code] = plan
        return plan

    def subscribe(self, customer_id, plan_code, start=None):
        if plan_code not in self._plans:
            raise NotFound("plan", plan_code)
        start = start or self.clock.today()
        sub = Subscription(self._ids.next(), customer_id, self._plans[plan_code], start)
        self._subs[sub.id] = sub
        return sub

    def get(self, sub_id):
        try:
            return self._subs[sub_id]
        except KeyError:
            raise NotFound("subscription", sub_id)

    def renewal_date(self, sub, n):
        """Date of the n-th renewal (n=1 is the first one after the start).

        Always counted from the start date, so a subscription that began on the 31st renews on the
        last day of shorter months and on the 31st again afterwards.
        """
        return _add_months_clamped(sub.start, sub.plan.interval_months * n)

    def next_renewal(self, sub):
        return self.renewal_date(sub, sub.renewals + 1)

    def first_billing_date(self, sub):
        return sub.start + datetime.timedelta(days=sub.plan.trial_days)

    def renew(self, sub_id):
        """Bill the next period: open an invoice due PAYMENT_TERMS_DAYS after the renewal date."""
        sub = self.get(sub_id)
        if sub.status != "active":
            raise InvalidState("subscription %s is %s" % (sub.id, sub.status))
        due_from = self.next_renewal(sub)
        number = self._invoice_ids.next()
        self.ledger.open_invoice(number, sub.customer_id, sub.plan.price_cents, due_from + datetime.timedelta(days=PAYMENT_TERMS_DAYS))
        sub.renewals += 1
        sub.invoices.append(number)
        return number

    def cancel(self, sub_id, on_date):
        """Cancel and return the credit for the unused part of the current period."""
        sub = self.get(sub_id)
        if sub.status != "active":
            raise InvalidState("subscription %s is %s" % (sub.id, sub.status))
        period_start = self.renewal_date(sub, sub.renewals) if sub.renewals else sub.start
        period_end = self.next_renewal(sub)
        total_days = (period_end - period_start).days
        unused = max(0, (period_end - on_date).days)
        sub.status = "cancelled"
        sub.cancelled_on = on_date
        return money.round_div(sub.plan.price_cents * unused, total_days)

    def pay(self, customer_id, cents):
        """Book a customer payment against their open invoices; returns [(invoice number, cents applied)]."""
        return self._settle(customer_id, cents)

    def apply_credit(self, customer_id):
        """Use a customer's stored credit on their open invoices."""
        stored = self.ledger.credit(customer_id)
        self.ledger._credit[customer_id] = 0
        return self._settle(customer_id, stored) if stored else []

    def due_for_renewal(self, on_date):
        return sorted(s.id for s in self._subs.values() if s.status == "active" and self.next_renewal(s) <= on_date)

    def monthly_recurring_cents(self):
        return sum(money.round_div(s.plan.price_cents, s.plan.interval_months) for s in self._subs.values() if s.status == "active")


def _add_months_clamped(day, months):
    index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(index, 12)
    last = calendar.days_in_month(year, month0 + 1)
    return datetime.date(year, month0 + 1, min(day.day, last))


def _settle(self, customer_id, cents):
    remaining = cents
    applied = []
    for invoice in self.ledger.open_items(customer_id):
        if remaining <= 0:
            break
        take = min(remaining, invoice.balance)
        invoice.paid += take
        remaining -= take
        applied.append((invoice.number, take))
    if remaining:
        self.ledger._credit[customer_id] = self.ledger.credit(customer_id) + remaining
    return applied


SubscriptionService._settle = _settle

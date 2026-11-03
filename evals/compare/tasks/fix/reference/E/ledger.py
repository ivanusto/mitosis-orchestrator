"""Accounts receivable: open invoices per customer, payments and ageing."""

import datetime
from dataclasses import dataclass

from shop.errors import InvalidState, NotFound, ValidationError


@dataclass
class OpenInvoice:
    number: str
    customer_id: str
    amount: int
    due: datetime.date
    paid: int = 0

    @property
    def balance(self):
        return self.amount - self.paid


class Ledger:
    def __init__(self):
        self._invoices = {}
        self._credit = {}

    def open_invoice(self, number, customer_id, amount, due):
        if amount <= 0:
            raise ValidationError("invoice amount must be positive")
        if number in self._invoices:
            raise InvalidState("duplicate invoice %s" % number)
        self._invoices[number] = OpenInvoice(number, customer_id, amount, due)
        return self._invoices[number]

    def get(self, number):
        try:
            return self._invoices[number]
        except KeyError:
            raise NotFound("invoice", number)

    def open_items(self, customer_id):
        """Unsettled invoices of a customer, oldest due date first (invoice number breaks ties)."""
        items = [i for i in self._invoices.values() if i.customer_id == customer_id and i.balance > 0]
        return sorted(items, key=lambda i: (i.due, i.number))

    def balance(self, customer_id):
        return sum(i.balance for i in self._invoices.values() if i.customer_id == customer_id)

    def credit(self, customer_id):
        return self._credit.get(customer_id, 0)

    def apply_payment(self, customer_id, amount):
        """Settle the oldest due invoices first.

        A payment never takes more from an invoice than its remaining balance; whatever is left
        after the last open invoice is kept as credit. Returns [(invoice number, cents applied)].
        """
        if amount <= 0:
            raise ValidationError("payment must be positive")
        remaining = amount
        applied = []
        for invoice in self.open_items(customer_id):
            if remaining <= 0:
                break
            take = min(remaining, invoice.balance)
            invoice.paid += take
            remaining -= take
            applied.append((invoice.number, take))
        if remaining:
            self._credit[customer_id] = self.credit(customer_id) + remaining
        return applied

    def use_credit(self, customer_id):
        """Apply stored credit to open invoices; returns the applications."""
        stored = self.credit(customer_id)
        if not stored:
            return []
        self._credit[customer_id] = 0
        return self.apply_payment(customer_id, stored)

    def overdue(self, customer_id, today):
        return [i for i in self.open_items(customer_id) if i.due < today]

    def aging(self, today):
        """Open balances by days past due: current, 1-30, 31-60, 61+."""
        buckets = {"current": 0, "1-30": 0, "31-60": 0, "61+": 0}
        for i in self._invoices.values():
            if i.balance <= 0:
                continue
            late = (today - i.due).days
            key = "current" if late <= 0 else "1-30" if late <= 30 else "31-60" if late <= 60 else "61+"
            buckets[key] += i.balance
        return buckets

    def statement(self, customer_id):
        rows = [(i.number, i.due.isoformat(), i.amount, i.paid, i.balance) for i in sorted(
            (i for i in self._invoices.values() if i.customer_id == customer_id), key=lambda i: (i.due, i.number))]
        return {"rows": rows, "balance": self.balance(customer_id), "credit": self.credit(customer_id)}

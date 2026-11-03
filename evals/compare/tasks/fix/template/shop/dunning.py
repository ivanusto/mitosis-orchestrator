"""Payment reminders driven by the receivables ledger."""

from shop import calendar, money

# (business days overdue, level, fee in cents)
LEVELS = [(30, "final", 1500), (14, "second", 500), (5, "first", 0)]


def level_for(business_days_overdue):
    for minimum, name, fee in LEVELS:
        if business_days_overdue >= minimum:
            return name, fee
    return None, 0


class Dunning:
    def __init__(self, ledger, holidays=frozenset()):
        self.ledger = ledger
        self.holidays = holidays

    def reminders(self, customer_id, today):
        """[(invoice number, level, fee)] for the customer's overdue invoices."""
        out = []
        for invoice in self.ledger.overdue(customer_id, today):
            late = calendar.business_days_between(invoice.due, today, self.holidays)
            level, fee = level_for(late)
            if level:
                out.append((invoice.number, level, fee))
        return out

    def fees_total(self, customer_id, today):
        return sum(fee for _, _, fee in self.reminders(customer_id, today))

    def interest(self, customer_id, today, annual_bps=800):
        """Simple daily interest on overdue balances, rounded per invoice."""
        total = 0
        for invoice in self.ledger.overdue(customer_id, today):
            days = (today - invoice.due).days
            total += money.round_div(invoice.balance * annual_bps * days, 10000 * 365)
        return total

    def worklist(self, customers, today):
        rows = []
        for customer in customers:
            for number, level, fee in self.reminders(customer, today):
                rows.append((customer, number, level, fee))
        return sorted(rows, key=lambda r: ({"final": 0, "second": 1, "first": 2}[r[2]], r[0], r[1]))

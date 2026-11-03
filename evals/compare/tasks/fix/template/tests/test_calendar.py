import datetime
import unittest

from shop import calendar

D = datetime.date


class CalendarTest(unittest.TestCase):
    def test_business_day(self):
        self.assertTrue(calendar.is_business_day(D(2026, 3, 2)))     # Monday
        self.assertFalse(calendar.is_business_day(D(2026, 3, 7)))    # Saturday
        self.assertFalse(calendar.is_business_day(D(2026, 3, 8)))    # Sunday
        self.assertFalse(calendar.is_business_day(D(2026, 12, 25), calendar.holidays_for(2026)))

    def test_add_business_days_skips_weekends(self):
        self.assertEqual(calendar.add_business_days(D(2026, 3, 2), 2), D(2026, 3, 4))
        self.assertEqual(calendar.add_business_days(D(2026, 3, 5), 3), D(2026, 3, 10))
        self.assertEqual(calendar.add_business_days(D(2026, 3, 7), 1), D(2026, 3, 9))
        self.assertEqual(calendar.add_business_days(D(2026, 3, 2), 0), D(2026, 3, 2))

    def test_add_business_days_skips_holidays(self):
        holidays = calendar.holidays_for(2026)
        self.assertEqual(calendar.add_business_days(D(2026, 12, 24), 1, holidays), D(2026, 12, 28))

    def test_next_business_day_and_between(self):
        self.assertEqual(calendar.next_business_day(D(2026, 3, 7)), D(2026, 3, 9))
        self.assertEqual(calendar.business_days_between(D(2026, 3, 2), D(2026, 3, 9)), 5)
        self.assertEqual(calendar.business_days_between(D(2026, 3, 9), D(2026, 3, 2)), -5)
        with self.assertRaises(ValueError):
            calendar.add_business_days(D(2026, 3, 2), -1)


if __name__ == "__main__":
    unittest.main()

"""Test scripts/error_watch/schedule.py, which says how often the error watch probes."""

import unittest
from datetime import datetime, timedelta, timezone

from error_watch.schedule import interval, is_business_hours, is_due, window


def utc(text: str) -> datetime:
    """A time in UTC, e.g. ``2026-11-05 14:00``."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class TestBusinessHours(unittest.TestCase):
    """9:00 eastern to 17:00 pacific, Monday to Friday, with daylight saving time."""

    def test_thursday_in_winter(self):
        # 2026-11-05 is a Thursday, after daylight saving time ended: EST is UTC-5, PST is UTC-8.
        self.assertFalse(is_business_hours(utc("2026-11-05 13:59")))
        self.assertTrue(is_business_hours(utc("2026-11-05 14:00")))
        self.assertTrue(is_business_hours(utc("2026-11-06 00:59")))
        self.assertFalse(is_business_hours(utc("2026-11-06 01:00")))

    def test_summer(self):
        # 2026-07-01, a Wednesday: EDT is UTC-4, PDT is UTC-7.
        self.assertTrue(is_business_hours(utc("2026-07-01 13:00")))
        self.assertFalse(is_business_hours(utc("2026-07-02 00:00")))

    def test_weekend(self):
        self.assertFalse(is_business_hours(utc("2026-11-07 18:00")))


class TestInterval(unittest.TestCase):
    """10 minutes during a problem, 20 in business hours, 60 otherwise."""

    def test_intervals(self):
        self.assertEqual(interval(utc("2026-11-07 18:00"), incident_open=True), timedelta(minutes=10))
        self.assertEqual(interval(utc("2026-11-05 18:00"), incident_open=False), timedelta(minutes=20))
        self.assertEqual(interval(utc("2026-11-05 06:00"), incident_open=False), timedelta(minutes=60))

    def test_window_is_at_least_half_an_hour(self):
        self.assertEqual(window(timedelta(minutes=10)), timedelta(minutes=30))
        self.assertEqual(window(timedelta(minutes=60)), timedelta(minutes=60))


class TestIsDue(unittest.TestCase):
    """A probe is due a little early, because GitHub starts runs late."""

    def test_due(self):
        now = utc("2026-11-05 18:00")
        twenty = timedelta(minutes=20)
        self.assertTrue(is_due(now, None, twenty))
        self.assertTrue(is_due(now, now - timedelta(minutes=16), twenty))
        self.assertFalse(is_due(now, now - timedelta(minutes=10), twenty))


if __name__ == "__main__":
    unittest.main()

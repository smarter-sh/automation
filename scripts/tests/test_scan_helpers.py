"""Tests of scripts/scan_helpers.py: the calendar-month cutoff and Slack escaping that the scans share."""

import unittest
from datetime import datetime, timezone

from scan_helpers import months_before, slack_escape

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class TestMonthsBefore(unittest.TestCase):
    """Months_before() counts calendar months, and keeps a day the earlier month has."""

    def test_month_end(self):
        end_of_august = datetime(2026, 8, 31, tzinfo=timezone.utc)
        expected = datetime(2026, 2, 28, tzinfo=timezone.utc)
        self.assertEqual(months_before(end_of_august, 6), expected)

    def test_across_a_year(self):
        expected = datetime(2025, 11, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(months_before(NOW, 12), expected)

    def test_rejects_bad_input(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            months_before(NOW, 0)
        with self.assertRaisesRegex(ValueError, "timezone"):
            months_before(datetime(2026, 1, 1), 1)
        with self.assertRaisesRegex(ValueError, "too large"):
            months_before(NOW, 2026 * 12)


class TestSlackEscape(unittest.TestCase):
    """Slack_escape() keeps names that GitHub returned from pinging or linking."""

    def test_escapes(self):
        self.assertNotIn("<!here>", slack_escape("<!here>"))
        self.assertEqual(slack_escape("a & b"), "a &amp; b")

    def test_plain_name(self):
        self.assertEqual(slack_escape("DEPLOY_TOKEN"), "DEPLOY_TOKEN")


if __name__ == "__main__":
    unittest.main()

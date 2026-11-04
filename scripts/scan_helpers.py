#!/usr/bin/env python3
"""Small helpers that the organization's scans share: a calendar-month cutoff and Slack-safe text.

The secret age review and the organization health reports both look back a number of calendar
months, and both post names that GitHub returned (repositories, paths, secrets) to Slack.

It uses only Python's standard library, because the workflows' runners have no virtual environment.
"""

import calendar
from datetime import datetime
from html import escape


def months_before(now: datetime, months: int) -> datetime:
    """
    Go back a number of calendar months, keeping the time of day.

    A day that the earlier month doesn't have becomes its last day: six months before 31 August
    is 28 February (29 in a leap year).

    :param now: The time to count back from. It must have a timezone.
    :param months: How many months to go back, 1 or more.
    :returns: The same time of day, ``months`` calendar months earlier.
    :raises ValueError: When ``now`` has no timezone, ``months`` is less than 1, or the result
        would be before the year 1.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if months < 1:
        raise ValueError("months must be a positive integer")
    # Count months from the year 0, so that going back across a new year is a subtraction.
    month_number = now.year * 12 + now.month - 1
    earlier_month_number = month_number - months
    year, month_index = divmod(earlier_month_number, 12)
    if year < 1:
        raise ValueError("months is too large")
    month = month_index + 1
    _, last_day = calendar.monthrange(year, month)
    day = min(now.day, last_day)
    return now.replace(year=year, month=month, day=day)


def slack_escape(value: str) -> str:
    """
    Escape text that GitHub returned, so that Slack shows it as text.

    Slack reads ``<...>`` as a link or a mention, so a repository or secret name could otherwise
    ping a channel or hide a link.

    :param value: A name or description that came from the GitHub API.
    :returns: The text, with ``&``, ``<`` and ``>`` escaped.
    """
    return escape(value, quote=False)

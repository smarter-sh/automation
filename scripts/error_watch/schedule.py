#!/usr/bin/env python3
"""Decide whether the error watch is due to probe the logs, and how far back to read them.

The workflow starts every 10 minutes, but probes only as often as this module says:

- every :data:`INCIDENT_INTERVAL` minutes while a problem is open, until it is resolved;
- every :data:`BUSINESS_INTERVAL` minutes in business hours, from 9:00 on the US east coast to 17:00 on
  the west coast, Monday to Friday (:func:`is_business_hours`);
- every :data:`OTHER_INTERVAL` minutes otherwise.

GitHub starts scheduled runs late, by minutes or more, so a probe is due a little early, :data:`TOLERANCE`
before its interval has passed: otherwise a run that started at 19 minutes would wait for the next one,
at 29.

The time zones come from the system's time zone database, which every GitHub runner has, through
Python's ``zoneinfo``, so daylight saving time is handled.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
"""The US east coast, where business hours start."""

PACIFIC = ZoneInfo("America/Los_Angeles")
"""The US west coast, where business hours end."""

BUSINESS_START = time(9, 0)
"""When business hours start, on the east coast."""

BUSINESS_END = time(17, 0)
"""When business hours end, on the west coast."""

INCIDENT_INTERVAL = timedelta(minutes=10)
"""How often to probe while a problem is open."""

BUSINESS_INTERVAL = timedelta(minutes=20)
"""How often to probe in business hours."""

OTHER_INTERVAL = timedelta(minutes=60)
"""How often to probe outside business hours."""

TOLERANCE = timedelta(minutes=5)
"""How early a probe may run, because GitHub starts scheduled runs late."""

MIN_WINDOW = timedelta(minutes=30)
"""The shortest period a probe reads: enough for a problem to repeat."""


def is_business_hours(now: datetime) -> bool:
    """
    Tell whether it is business hours somewhere in the continental US.

    That is from 9:00 eastern to 17:00 pacific, Monday to Friday.

    :param now: The time, with its time zone.
    :returns: True in business hours.
    """
    eastern_now = now.astimezone(EASTERN)
    is_weekend = eastern_now.weekday() >= 5
    if is_weekend:
        return False
    today = eastern_now.date()
    starts = datetime.combine(today, BUSINESS_START, tzinfo=EASTERN)
    ends = datetime.combine(today, BUSINESS_END, tzinfo=PACIFIC)
    return starts <= now < ends


def interval(now: datetime, incident_open: bool) -> timedelta:
    """
    Say how often to probe at this time.

    :param now: The time, with its time zone.
    :param incident_open: Whether a problem is open.
    :returns: :data:`INCIDENT_INTERVAL`, :data:`BUSINESS_INTERVAL` or :data:`OTHER_INTERVAL`.
    """
    if incident_open:
        return INCIDENT_INTERVAL
    if is_business_hours(now):
        return BUSINESS_INTERVAL
    return OTHER_INTERVAL


def window(probe_interval: timedelta) -> timedelta:
    """
    Say how far back a probe reads: its interval, so that no minute is missed, and at least :data:`MIN_WINDOW`.

    :param probe_interval: How often it probes.
    :returns: The period to read.
    """
    return max(probe_interval, MIN_WINDOW)


def is_due(now: datetime, last_probe: datetime | None, probe_interval: timedelta) -> bool:
    """
    Tell whether a probe is due.

    :param now: The time, with its time zone.
    :param last_probe: When the last probe ran, or None if none ever did.
    :param probe_interval: How often to probe.
    :returns: True if no probe ever ran, or the interval, less :data:`TOLERANCE`, has passed.
    """
    if last_probe is None:
        return True
    elapsed = now - last_probe
    return elapsed >= probe_interval - TOLERANCE

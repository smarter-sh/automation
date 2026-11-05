#!/usr/bin/env python3
"""Keep track of a problem that the error watch found, from the first Slack message to the last.

The workflow runs every 10 minutes and forgets everything between runs, so what it must remember is
saved in a small JSON file, :class:`State`, which the workflow keeps in the Actions cache:

- when the last probe ran, so that :mod:`error_watch.schedule` can tell whether the next one is due;
- the open problem, if there is one (:class:`Incident`): when it started, when Slack last heard about it,
  the Slack thread, and the keys of the errors that make it up.

The file holds only times, keys and labels: never a log line or a message from one. It is still in a
public repository's cache, so it holds nothing secret.

After each probe, :func:`decide` chooses one :data:`ACTIONS`:

``open``
    No problem was open, and an error repeated at least ``min_repeats`` times: post the findings.
``new``
    A problem is open, and another error started repeating: post it in the problem's thread, and to the
    channel.
``remind``
    The problem goes on, and Slack last heard about it :data:`REMINDER_INTERVAL` ago or more: say so in
    the thread.
``ongoing``
    The problem goes on, and Slack heard about it recently: post nothing.
``resolve``
    None of the problem's errors was logged for :data:`QUIET_PERIOD`, and no error repeats: say that it
    is resolved, in the thread and to the channel, and go back to the normal schedule.
``healthy``
    No problem, and none was open: post nothing.

:func:`message` writes each action's Slack message, in Slack's mrkdwn.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scan_helpers import slack_escape

ACTIONS = ("open", "new", "remind", "ongoing", "resolve", "healthy")
"""What :func:`decide` may choose."""

REMINDER_INTERVAL = timedelta(minutes=30)
"""How often to tell Slack that an open problem goes on."""

QUIET_PERIOD = timedelta(minutes=20)
"""How long none of a problem's errors must be logged before it is resolved."""

MAX_LISTED = 8
"""The most findings that one Slack message lists."""


@dataclass
class Incident:
    """An open problem."""

    opened: str
    """When it was first posted, in ISO 8601, UTC."""

    last_post: str
    """When Slack last heard about it, in ISO 8601, UTC."""

    keys: dict[str, str] = field(default_factory=dict)
    """The keys of its errors, each with its label."""

    channel: str = ""
    """The Slack channel's id of its thread, or empty if nothing was posted."""

    thread_ts: str = ""
    """The Slack timestamp of its first message, which its other messages reply to, or empty."""


@dataclass
class State:
    """What the error watch remembers between runs."""

    last_probe: str = ""
    """When the last probe ran, in ISO 8601, UTC, or empty if none did."""

    incident: Incident | None = None
    """The open problem, or None."""


@dataclass
class Decision:
    """What to do after a probe, and the findings that it is about."""

    action: str
    """One of :data:`ACTIONS`."""

    repeating: list[dict] = field(default_factory=list)
    """The findings that repeated at least ``min_repeats`` times in the probe's period."""

    new: list[dict] = field(default_factory=list)
    """The repeating findings that the open problem did not have yet."""

    lingering: list[dict] = field(default_factory=list)
    """The open problem's findings that were logged within :data:`QUIET_PERIOD`, but not that often."""


def parse_time(text: str) -> datetime | None:
    """
    Read a time that this module wrote.

    :param text: A time in ISO 8601, or empty.
    :returns: The time, or None if the text is empty.
    """
    if not text:
        return None
    return datetime.fromisoformat(text)


def format_time(when: datetime) -> str:
    """
    Write a time as this module saves it.

    :param when: The time, with its time zone.
    :returns: e.g. ``2026-11-05T14:00:00+00:00``.
    """
    utc_time = when.astimezone(timezone.utc)
    whole_seconds = utc_time.replace(microsecond=0)
    return whole_seconds.isoformat()


def load_state(path: Path) -> State:
    """
    Read the saved state, or start afresh.

    :param path: The state's file, which the Actions cache restored, or which does not exist yet.
    :returns: The state; an empty one if the file is missing or unreadable.
    """
    if not path.is_file():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print("::warning::The error watch's saved state could not be read, so it starts afresh.")
        return State()
    incident_data = data.get("incident")
    incident = None
    if isinstance(incident_data, dict):
        incident = Incident(
            opened=incident_data.get("opened", ""),
            last_post=incident_data.get("last_post", ""),
            keys=incident_data.get("keys") or {},
            channel=incident_data.get("channel", ""),
            thread_ts=incident_data.get("thread_ts", ""),
        )
    last_probe = data.get("last_probe", "")
    return State(last_probe=last_probe, incident=incident)


def save_state(state: State, path: Path) -> None:
    """
    Write the state, for the Actions cache to keep.

    :param state: The state.
    :param path: The file to write; its folder is created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(state)
    text = json.dumps(data, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def nanoseconds_to_time(value: int) -> datetime:
    """
    Convert Unix nanoseconds, as Loki gives a line's time, into a time.

    :param value: The time, in Unix nanoseconds.
    :returns: The time, in UTC.
    """
    seconds = value / 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def decide(state: State, findings: list[dict], probed_at: datetime, min_repeats: int) -> Decision:
    """
    Choose what to do after a probe.

    :param state: What was remembered before the probe.
    :param findings: Every error the probe found, repeated or not, as ``detect.Finding`` dicts.
    :param probed_at: When the probe read the logs.
    :param min_repeats: How many times an error must be logged in the probe's period to be a problem.
    :returns: The decision. The state is not changed.
    """
    repeating = []
    for finding in findings:
        if finding["count"] >= min_repeats:
            repeating.append(finding)
    incident = state.incident
    if incident is None:
        if repeating:
            return Decision(action="open", repeating=repeating)
        return Decision(action="healthy")

    new = []
    for finding in repeating:
        if finding["key"] not in incident.keys:
            new.append(finding)
    lingering = recent_findings(incident, findings, probed_at)
    if new:
        return Decision(action="new", repeating=repeating, new=new, lingering=lingering)
    if not repeating and not lingering:
        return Decision(action="resolve")
    last_post = parse_time(incident.last_post) or probed_at
    since_last_post = probed_at - last_post
    if since_last_post >= REMINDER_INTERVAL:
        return Decision(action="remind", repeating=repeating, lingering=lingering)
    return Decision(action="ongoing", repeating=repeating, lingering=lingering)


def recent_findings(incident: Incident, findings: list[dict], probed_at: datetime) -> list[dict]:
    """
    Find the open problem's errors that were logged within :data:`QUIET_PERIOD` of the probe.

    :param incident: The open problem.
    :param findings: Every error the probe found.
    :param probed_at: When the probe read the logs.
    :returns: The findings of the problem's keys that were logged recently, however often.
    """
    quiet_since = probed_at - QUIET_PERIOD
    recent = []
    for finding in findings:
        if finding["key"] not in incident.keys:
            continue
        last_seen = nanoseconds_to_time(finding["last_seen"])
        if last_seen >= quiet_since:
            recent.append(finding)
    return recent


def apply(state: State, decision: Decision, probed_at: datetime, posted: dict | None) -> State:
    """
    Record a decision in the state, once its Slack message, if any, was posted.

    :param state: The state before the probe.
    :param decision: What was decided.
    :param probed_at: When the probe read the logs.
    :param posted: Slack's answer to the decision's first message, with its ``channel`` and ``ts``, or None
        if nothing was posted, e.g. without a Slack token.
    :returns: The new state.
    """
    now_text = format_time(probed_at)
    new_state = State(last_probe=now_text, incident=state.incident)
    if decision.action == "resolve":
        new_state.incident = None
        return new_state
    if decision.action == "open":
        new_state.incident = Incident(opened=now_text, last_post=now_text)
        if posted:
            new_state.incident.channel = posted.get("channel", "")
            new_state.incident.thread_ts = posted.get("ts", "")
    if new_state.incident is None:
        return new_state
    for finding in decision.repeating:
        new_state.incident.keys[finding["key"]] = finding["label"]
    posts_a_message = decision.action in ("new", "remind")
    if posts_a_message:
        new_state.incident.last_post = now_text
    return new_state


def duration_text(period: timedelta) -> str:
    """
    Write a period for people to read.

    :param period: e.g. 80 minutes.
    :returns: e.g. ``1 h 20 min``, or ``40 min``.
    """
    minutes = int(period.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes} min"
    return f"{hours} h {minutes} min"


def finding_lines(finding: dict) -> list[str]:
    """
    Describe one finding in a Slack message.

    :param finding: A ``detect.Finding`` dict.
    :returns: Its lines, in Slack's mrkdwn: the label, count, components and last time, then its redacted
        message, if any.
    """
    label = slack_escape(finding["label"])
    components = ", ".join(finding["components"])
    last_seen = nanoseconds_to_time(finding["last_seen"])
    lines = [f"• *{label}*: {finding['count']}× in {slack_escape(components)}, last at {last_seen:%H:%M} UTC"]
    detail = finding.get("detail") or ""
    if detail:
        # a backtick would end the code span early.
        detail_text = detail.replace("`", "'")
        lines.append(f"      `{slack_escape(detail_text)}`")
    return lines


def findings_lines(findings: list[dict]) -> list[str]:
    """
    Describe findings in a Slack message, at most :data:`MAX_LISTED` of them.

    :param findings: ``detect.Finding`` dicts, the most frequent first.
    :returns: Their lines, and one that counts the rest, if any.
    """
    lines = []
    listed = findings[:MAX_LISTED]
    for finding in listed:
        lines.extend(finding_lines(finding))
    rest = len(findings) - len(listed)
    if rest > 0:
        lines.append(f"• …and {rest} more: see the run.")
    return lines


def message(
    decision: Decision, state: State, probed_at: datetime, where: str, window_minutes: int, run_url: str
) -> str:
    """
    Write the Slack message of a decision.

    :param decision: What was decided.
    :param state: The state before the probe, whose incident the message is about.
    :param probed_at: When the probe read the logs.
    :param where: The environment's name, e.g. ``ubc.smarter.sh``.
    :param window_minutes: How far back the probe read.
    :param run_url: The workflow run's page.
    :returns: The message, in Slack's mrkdwn, or an empty string for an action that posts nothing.
    """
    run_link = f"<{run_url}|this run>"
    where_text = slack_escape(where)
    opened = probed_at
    if state.incident is not None:
        opened = parse_time(state.incident.opened) or probed_at
    lasted = duration_text(probed_at - opened)

    if decision.action == "open":
        lines = [
            f":rotating_light: *Repeated errors in {where_text}*",
            f"These errors repeated in the last {window_minutes} minutes, which suggests a failure or a bug:",
        ]
        lines.extend(findings_lines(decision.repeating))
        lines.append(f"I'll check every 10 minutes, and post in this thread until they stop. Details: {run_link}.")
        return "\n".join(lines)
    if decision.action == "new":
        lines = [f":rotating_light: *Another error is repeating in {where_text}*, {lasted} into this problem:"]
        lines.extend(findings_lines(decision.new))
        lines.append(f"Details: {run_link}.")
        return "\n".join(lines)
    if decision.action == "remind":
        still = decision.repeating or decision.lingering
        lines = [
            f":hourglass: *Still failing in {where_text}*, for {lasted} now. In the last {window_minutes} minutes:"
        ]
        lines.extend(findings_lines(still))
        lines.append(f"Details: {run_link}.")
        return "\n".join(lines)
    if decision.action == "resolve":
        quiet = duration_text(QUIET_PERIOD)
        return (
            f":white_check_mark: *Resolved in {where_text}*: none of these errors was logged in the last {quiet}. "
            f"The problem lasted {lasted}. I've stopped the 10-minute checks, and gone back to the normal "
            f"schedule. Details: {run_link}."
        )
    return ""

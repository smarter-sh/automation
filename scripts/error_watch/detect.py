#!/usr/bin/env python3
"""Find the Python tracebacks and the ERROR and CRITICAL lines in pods' logs, and group the repeated ones.

The error watch (``.github/workflows/errorWatch.yml``) reads the last half hour or hour of every pod's log
from Loki, and hands each pod's lines, oldest first, to :class:`StreamParser`. Loki stores one line per
entry, so a traceback arrives as many lines, which the parser puts back together:

.. code-block:: text

    [2026-11-05 14:00:00 +0000] - ERROR - MainProcess - Internal Server Error: /api/v1/chat/
    Traceback (most recent call last):
      File "/smarter/smarter/apps/chat/views.py", line 42, in post
        reply = self.provider.send(messages)
    TimeoutError: the provider did not answer in 30 seconds

That is one :class:`Event`: a ``TimeoutError`` in ``smarter/apps/chat/views.py``, ``post()``. The ERROR line
just before the traceback belongs to it, and is not counted again. In a chain of exceptions ("During
handling of the above exception, another exception occurred"), the last one is the event.

An ERROR or CRITICAL line without a traceback is an event of its own, keyed by its message with the
numbers, ids, quoted values and urls taken out, so that "Task 12 failed" and "Task 13 failed" are the
same problem.

:func:`group` counts the events by key into :class:`Finding` objects. A finding that repeats is a sign of
a failure or a bug, rather than one user's bad request.

Log lines are untrusted and may hold personal data, so each finding has two texts: ``label``, which holds
only the exception's type and the code's location (or a short id), and is safe for the workflow's public
summary, and ``detail``, the redacted message, which only goes to Slack.

It uses only Python's standard library, and :mod:`pod_log.redact` for the redaction.
"""

import collections
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field

# Run by its path, a script has only its own folder on sys.path. Add scripts/, so that it can import
# the other packages.
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from pod_log.redact import redact  # noqa: E402

TRACEBACK_STARTS = ("Traceback (most recent call last):", "Exception Group Traceback (most recent call last):")
"""The first line of a Python traceback."""

CHAIN_MARKERS = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)
"""The lines between two tracebacks of one chain of exceptions."""

FRAME = re.compile(r'^\s+File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>\S+)')
"""One frame of a traceback: the file, the line number and the function."""

EXCEPTION_TYPE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s*(?P<message>.*))?$")
"""The last line of a traceback: the exception's type, and its message."""

LEVEL = re.compile(r"(?:^|[\s\[:/-])(?P<level>ERROR|CRITICAL)(?=[\s\]/:-])")
"""The level of an ERROR or CRITICAL line, as Django (`` - ERROR - ``) and Celery (``: ERROR/``) write it."""

LEVEL_SEARCH_LENGTH = 80
"""How far into a line the level is looked for: past the time, before the message."""

PROCESS = re.compile(r"^[\s\]:/-]*(?:[A-Za-z]*(?:Process|Worker)(?:-\d+)?)?[\s\]:-]*")
"""What comes between the level and the message, e.g. `` - MainProcess - `` or ``/ForkPoolWorker-2] ``."""

LIBRARY_PATHS = ("site-packages/", "dist-packages/", "/lib/python")
"""Paths of code that is not the platform's own.

The innermost frame outside them locates the bug.
"""

PATH_PARTS = 4
"""How many of a file path's last parts a label keeps, e.g. ``smarter/apps/chat/views.py``."""

DETAIL_LENGTH = 200
"""The longest ``detail`` that goes to Slack."""

NORMALIZE = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"'[^']*'"), "'…'"),
    (re.compile(r'"[^"]*"'), '"…"'),
    (re.compile(r"\d+"), "N"),
]
"""What :func:`normalize` replaces, in order, so that one problem's messages have one key."""

IGNORED = [
    re.compile(r"DisallowedHost"),
    re.compile(r"Invalid HTTP_HOST header"),
]
"""Errors that are not the platform's failure, e.g. scanners that send a wrong host.

They are never findings.
"""


@dataclass
class Event:
    """One error in one pod's log: a traceback, or an ERROR or CRITICAL line."""

    key: str
    """What makes two events the same problem."""

    kind: str
    """``traceback`` or ``error-line``."""

    label: str
    """A name of the problem that holds nothing from the log's text: safe for the public summary."""

    detail: str
    """The redacted message, for Slack only."""

    time: int
    """When it was logged, in Unix nanoseconds."""

    component: str
    """The pod's component, e.g. ``smarter-worker``."""


@dataclass
class Finding:
    """The events of one key, counted."""

    key: str
    """What makes two events the same problem."""

    kind: str
    """``traceback`` or ``error-line``."""

    label: str
    """A name of the problem that is safe for the public summary."""

    detail: str
    """The redacted message of its first event, for Slack only."""

    count: int = 0
    """How many times it was logged."""

    first_seen: int = 0
    """When it was first logged, in Unix nanoseconds."""

    last_seen: int = 0
    """When it was last logged, in Unix nanoseconds."""

    components: list[str] = field(default_factory=list)
    """The components whose pods logged it, sorted."""


def normalize(text: str) -> str:
    """
    Take the values out of a message, so that one problem's messages are the same text.

    :param text: A message, e.g. ``Task 12 failed for 'acme'``.
    :returns: e.g. ``Task N failed for '…'``.
    """
    for pattern, replacement in NORMALIZE:
        text = pattern.sub(replacement, text)
    return text.strip()


def safe_detail(text: str) -> str:
    """
    Make a message fit to post to Slack: values taken out, credentials and personal data redacted, and short.

    :param text: A message from a log.
    :returns: The message, normalized, redacted and cut to :data:`DETAIL_LENGTH` characters.
    """
    counts: collections.Counter = collections.Counter()
    normalized = normalize(text)
    redacted = redact(normalized, counts)
    if len(redacted) > DETAIL_LENGTH:
        redacted = redacted[: DETAIL_LENGTH - 1] + "…"
    return redacted


def short_id(text: str) -> str:
    """
    Give a short, stable id of a text, to name a problem without quoting it.

    :param text: e.g. a key.
    :returns: Eight hexadecimal digits.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:8]


def short_path(path: str) -> str:
    """
    Shorten a file path to its last parts, from the library's folder if it is a library's.

    :param path: e.g. ``/usr/local/lib/python3.12/site-packages/django/db/backends/utils.py``.
    :returns: e.g. ``django/db/backends/utils.py``.
    """
    for marker in ("site-packages/", "dist-packages/"):
        if marker in path:
            return path.split(marker, 1)[1]
    parts = path.strip("/").split("/")
    last_parts = parts[-PATH_PARTS:]
    return "/".join(last_parts)


def is_library(path: str) -> bool:
    """
    Tell whether a frame's file is a library's rather than the platform's own code.

    :param path: The frame's file path.
    :returns: True if it is under one of :data:`LIBRARY_PATHS`.
    """
    for marker in LIBRARY_PATHS:
        if marker in path:
            return True
    return False


def choose_frame(frames: list[tuple[str, str]]) -> tuple[str, str]:
    """
    Choose the frame that locates a traceback's problem: the innermost one in the platform's own code.

    :param frames: Each frame's file path and function, outermost first.
    :returns: The frame's file path and function, the innermost frame if every one is a library's, or
        ``("unknown", "unknown")`` if there is none.
    """
    if not frames:
        return ("unknown", "unknown")
    for path, function in reversed(frames):
        if not is_library(path):
            return (path, function)
    return frames[-1]


def is_ignored(text: str) -> bool:
    """
    Tell whether an error is one of :data:`IGNORED`.

    :param text: The event's key or message.
    :returns: True if any pattern of :data:`IGNORED` matches it.
    """
    for pattern in IGNORED:
        if pattern.search(text):
            return True
    return False


def error_message(text: str) -> str | None:
    """
    Read the message of an ERROR or CRITICAL line.

    :param text: A log line.
    :returns: The message after the level and the process, or None if the line is not an ERROR or CRITICAL
        line.
    """
    head = text[:LEVEL_SEARCH_LENGTH]
    match = LEVEL.search(head)
    if match is None:
        return None
    rest = text[match.end() :]
    return PROCESS.sub("", rest, count=1)


class StreamParser:
    """
    Read one pod's lines, oldest first, and collect its errors as :class:`Event` objects.

    :param component: The pod's component, e.g. ``smarter``.
    """

    def __init__(self, component: str):
        """
        Start with no events.

        :param component: The pod's component.
        """
        self.component = component
        self.events: list[Event] = []
        self.in_traceback = False
        self.frames: list[tuple[str, str]] = []
        self.traceback_time = 0
        # True after "During handling of the above exception": the next traceback replaces the last event.
        self.chained = False
        # The ERROR line just before a traceback, which the traceback's event replaces.
        self.error_line_event: Event | None = None

    def feed(self, line_time: int, text: str) -> None:
        """
        Read the pod's next line.

        :param line_time: The line's time, in Unix nanoseconds.
        :param text: The line.
        """
        line = text.rstrip()
        if line.startswith(TRACEBACK_STARTS):
            self.start_traceback(line_time)
            return
        if self.in_traceback:
            self.read_traceback_line(line)
            return
        if not line:
            return
        if line in CHAIN_MARKERS:
            self.chained = True
            return
        self.chained = False
        self.error_line_event = None
        message = error_message(line)
        if message is not None:
            self.add_error_line(line_time, message)

    def start_traceback(self, line_time: int) -> None:
        """
        Begin a traceback.

        :param line_time: Its first line's time.
        """
        self.in_traceback = True
        self.frames = []
        self.traceback_time = line_time

    def read_traceback_line(self, line: str) -> None:
        """
        Read a line inside a traceback: a frame, its source, or the exception that ends it.

        :param line: The line, without its trailing whitespace.
        """
        if not line:
            return
        is_indented = line[0].isspace()
        if is_indented:
            match = FRAME.match(line)
            if match is not None:
                frame = (match.group("path"), match.group("function"))
                self.frames.append(frame)
            return
        self.finish_traceback(line)

    def finish_traceback(self, exception_line: str) -> None:
        """
        End a traceback at its exception's line, and add its event.

        :param exception_line: e.g. ``TimeoutError: the provider did not answer``.
        """
        self.in_traceback = False
        match = EXCEPTION_TYPE.match(exception_line)
        if match is None:
            exception_type = "UnknownException"
            message = exception_line
        else:
            exception_type = match.group("type")
            message = match.group("message") or ""
        path, function = choose_frame(self.frames)
        location = short_path(path)
        event = Event(
            key=f"traceback|{exception_type}|{location}|{function}",
            kind="traceback",
            label=f"{exception_type} in {location}, {function}()",
            detail=safe_detail(message),
            time=self.traceback_time,
            component=self.component,
        )
        self.replace_owned_event()
        self.chained = False
        self.error_line_event = None
        if not is_ignored(event.key) and not is_ignored(message):
            self.events.append(event)

    def replace_owned_event(self) -> None:
        """Drop the last event if this traceback replaces it: the ERROR line before it, or the chain's last one."""
        if not self.events:
            return
        last_event = self.events[-1]
        owns_error_line = last_event is self.error_line_event
        continues_chain = self.chained and last_event.kind == "traceback"
        if owns_error_line or continues_chain:
            self.events.pop()

    def add_error_line(self, line_time: int, message: str) -> None:
        """
        Add an ERROR or CRITICAL line's event.

        :param line_time: The line's time.
        :param message: The line's message, after the level and the process.
        """
        if is_ignored(message):
            return
        template = normalize(message)
        key = f"error-line|{template}"
        event = Event(
            key=key,
            kind="error-line",
            label=f"ERROR line {short_id(key)}",
            detail=safe_detail(message),
            time=line_time,
            component=self.component,
        )
        self.events.append(event)
        self.error_line_event = event


def parse(component: str, lines: list[tuple[int, str]]) -> list[Event]:
    """
    Collect the errors of one pod's lines.

    :param component: The pod's component, e.g. ``smarter-worker``.
    :param lines: The pod's lines, oldest first: each one's time, in Unix nanoseconds, and its text.
    :returns: The pod's errors, oldest first.
    """
    parser = StreamParser(component)
    for line_time, text in lines:
        parser.feed(line_time, text)
    return parser.events


def group(events: list[Event]) -> list[Finding]:
    """
    Count the events by key.

    :param events: Every pod's errors.
    :returns: One finding per key, the most frequent first.
    """
    findings: dict[str, Finding] = {}
    components: dict[str, set[str]] = {}
    for event in events:
        if event.key not in findings:
            findings[event.key] = Finding(
                key=event.key,
                kind=event.kind,
                label=event.label,
                detail=event.detail,
                first_seen=event.time,
                last_seen=event.time,
            )
            components[event.key] = set()
        finding = findings[event.key]
        finding.count += 1
        finding.first_seen = min(finding.first_seen, event.time)
        finding.last_seen = max(finding.last_seen, event.time)
        components[event.key].add(event.component)
    result = list(findings.values())
    for finding in result:
        finding.components = sorted(components[finding.key])
    result.sort(key=finding_order)
    return result


def finding_order(finding: Finding) -> tuple[int, str]:
    """
    Give the key that sorts findings, the most frequent first, for ``sort()``.

    :param finding: A finding.
    :returns: Its count, negated, and its key.
    """
    return (-finding.count, finding.key)

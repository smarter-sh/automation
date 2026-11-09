#!/usr/bin/env python3
"""Count the tests that a GitHub Actions job ran, and how many passed, from the summary lines in its log.

``repository_tests.py`` reads every job log of each repository's test run with :func:`count_log`.
The smarter-sh repositories use several test runners, and each one prints its own summary:

=================  ==================================================
Runner             Summary line
=================  ==================================================
pytest             ``==== 1 failed, 69 passed, 2 skipped in 0.58s ====``
unittest, Django   ``Ran 148 tests in 0.9s``, then ``OK (skipped=10)`` or
                   ``FAILED (failures=1, errors=2)``
vitest             ``Tests  134 passed | 10 skipped (144)``
Jest               ``Tests:       4 skipped, 146 passed, 150 total``
node --test        ``# pass 96`` and ``# fail 0`` (``ℹ pass 96`` with the spec reporter)
mocha              ``40 passing (2s)`` and ``2 failing``
PHPUnit            ``OK (26 tests, 68 assertions)``, or
                   ``Tests: 49, Assertions: 131, Failures: 2, Skipped: 1.``
=================  ==================================================

A skipped test isn't counted as run. ``go test`` without ``-v`` prints no count, and a job that
only lints or builds prints none either: such a log has no count at all.

A job that merges other jobs' reports (``vitest --merge-reports``) prints their counts a second
time, so its log isn't counted.

It uses only Python's standard library, because the workflows' runners have no virtual environment.
"""

import re
from dataclasses import dataclass

TIMESTAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ")
"""The time stamp that GitHub puts in front of every line of a job's log."""

COLOR_CODE = re.compile(r"\x1b\[[0-9;]*m")
"""A terminal color code, e.g. vitest's green ``passed``."""

MERGED_REPORT = "--merge-reports"
"""In a log, the sign of a job that repeats other jobs' counts."""

PYTEST_SUMMARY = re.compile(r"^=+ (.+) in [\d.]+s(?: \([^)]*\))? =+$")
UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ")
UNITTEST_VERDICT = re.compile(r"^(?:OK|FAILED)(?: \((.*)\))?$")
PHPUNIT_OK = re.compile(r"^OK \((\d+) tests?, \d+ assertions?\)$")
PHPUNIT_TOTALS = re.compile(r"^Tests: (\d+), Assertions: \d+(.*)$")
JAVASCRIPT_TESTS = re.compile(r"^Tests:?\s+(.*\d+ (?:passed|failed).*)$")
NODE_COUNT = re.compile(r"^(?:#|ℹ) (pass|fail) (\d+)$")
MOCHA_COUNT = re.compile(r"^(\d+) (passing|failing)\b")

NUMBER_AND_WORD = re.compile(r"(\d+) ([a-z]+)")
"""One count of a pytest, vitest or Jest summary, e.g. ``69 passed``."""

UNITTEST_DETAIL = re.compile(r"([a-z ]+)=(\d+)")
"""One count of a unittest verdict, e.g. ``skipped=10``."""

PHPUNIT_DETAIL = re.compile(r"([A-Za-z]+): (\d+)")
"""One count of a PHPUnit summary, e.g. ``Failures: 2``."""


@dataclass
class Counts:
    """How many tests ran, and how many of them passed."""

    #: The tests that ran: passed or failed, not skipped.
    run: int = 0
    #: The tests that passed.
    passed: int = 0

    def add(self, other: "Counts") -> None:
        """
        Add another summary's counts to these.

        :param other: The counts to add.
        """
        self.run += other.run
        self.passed += other.passed


def clean_line(line: str) -> str:
    """
    Take the time stamp and the color codes out of a line of a job's log.

    :param line: The line, as GitHub serves it, e.g. ``2026-11-08T15:09:08.5Z ==== 70 passed in 0.58s ====``.
    :returns: The text the test runner printed, without the white space around it.
    """
    without_time = TIMESTAMP.sub("", line)
    without_color = COLOR_CODE.sub("", without_time)
    return without_color.strip()


def word_counts(text: str) -> dict[str, int]:
    """
    Read the counts of a summary that puts the number first, e.g. ``1 failed, 69 passed``.

    :param text: The summary.
    :returns: Each word's number, e.g. ``{"failed": 1, "passed": 69}``.
    """
    counts = {}
    for number, word in NUMBER_AND_WORD.findall(text):
        counts[word] = int(number)
    return counts


def _pytest(match: re.Match) -> Counts:
    """
    Count a pytest summary, e.g. ``1 failed, 69 passed, 2 skipped``.

    :param match: The match of :data:`PYTEST_SUMMARY`.
    :returns: The counts.
    """
    counts = word_counts(match.group(1))
    passed = counts.get("passed", 0)
    errors = counts.get("error", 0) + counts.get("errors", 0)
    failed = counts.get("failed", 0) + errors
    return Counts(run=passed + failed, passed=passed)


def _javascript(match: re.Match) -> Counts:
    """
    Count a vitest or Jest summary, e.g. ``2 failed | 40 passed (42)``.

    :param match: The match of :data:`JAVASCRIPT_TESTS`.
    :returns: The counts.
    """
    counts = word_counts(match.group(1))
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    return Counts(run=passed + failed, passed=passed)


def _phpunit_ok(match: re.Match) -> Counts:
    """
    Count a PHPUnit run where every test passed, e.g. ``OK (26 tests, 68 assertions)``.

    :param match: The match of :data:`PHPUNIT_OK`.
    :returns: The counts.
    """
    tests = int(match.group(1))
    return Counts(run=tests, passed=tests)


def _phpunit_totals(match: re.Match) -> Counts:
    """
    Count a PHPUnit run with failures or skipped tests, e.g. ``Tests: 49, Assertions: 131, Failures: 2.``.

    :param match: The match of :data:`PHPUNIT_TOTALS`.
    :returns: The counts.
    """
    tests = int(match.group(1))
    details = {}
    for name, number in PHPUNIT_DETAIL.findall(match.group(2)):
        details[name] = int(number)
    not_run = details.get("Skipped", 0) + details.get("Incomplete", 0)
    failed = details.get("Failures", 0) + details.get("Errors", 0)
    run = tests - not_run
    return Counts(run=run, passed=run - failed)


def _node(match: re.Match) -> Counts:
    """
    Count one line of node's test runner, ``# pass 96`` or ``# fail 0``.

    :param match: The match of :data:`NODE_COUNT`.
    :returns: The counts.
    """
    number = int(match.group(2))
    if match.group(1) == "pass":
        return Counts(run=number, passed=number)
    return Counts(run=number)


def _mocha(match: re.Match) -> Counts:
    """
    Count one line of mocha's summary, ``40 passing (2s)`` or ``2 failing``.

    :param match: The match of :data:`MOCHA_COUNT`.
    :returns: The counts.
    """
    number = int(match.group(1))
    if match.group(2) == "passing":
        return Counts(run=number, passed=number)
    return Counts(run=number)


LINE_SUMMARIES = (
    (PYTEST_SUMMARY, _pytest),
    (PHPUNIT_OK, _phpunit_ok),
    (PHPUNIT_TOTALS, _phpunit_totals),
    (JAVASCRIPT_TESTS, _javascript),
    (NODE_COUNT, _node),
    (MOCHA_COUNT, _mocha),
)
"""The summaries that fit on one line, and how to count each.

PHPUnit's go before Jest's ``Tests:``.
"""


def line_counts(line: str) -> Counts | None:
    """
    Count a summary that fits on one line.

    :param line: A line of the log, cleaned by :func:`clean_line`.
    :returns: The counts, or None when the line isn't one of :data:`LINE_SUMMARIES`.
    """
    for pattern, count in LINE_SUMMARIES:
        match = pattern.match(line)
        if match:
            return count(match)
    return None


def unittest_counts(line: str, ran: int) -> Counts | None:
    """
    Count a unittest run from its verdict, the ``OK`` or ``FAILED`` line after ``Ran 148 tests in 0.9s``.

    :param line: A line of the log, cleaned by :func:`clean_line`.
    :param ran: The number in the ``Ran`` line, which includes the skipped tests.
    :returns: The counts, or None when the line isn't a verdict.
    """
    match = UNITTEST_VERDICT.match(line)
    if not match:
        return None
    details = {}
    detail_text = match.group(1) or ""
    for name, number in UNITTEST_DETAIL.findall(detail_text):
        details[name.strip()] = int(number)
    not_run = details.get("skipped", 0) + details.get("expected failures", 0)
    failures = details.get("failures", 0) + details.get("errors", 0)
    failed = failures + details.get("unexpected successes", 0)
    run = ran - not_run
    return Counts(run=run, passed=run - failed)


def count_log(text: str) -> Counts | None:
    """
    Count the tests in one job's log, adding up every summary in it.

    :param text: The job's whole log.
    :returns: The counts, or None when the log has no summary, or repeats other jobs' counts
        (see :data:`MERGED_REPORT`).
    """
    if MERGED_REPORT in text:
        return None
    total = Counts()
    found = False
    ran = None
    for raw_line in text.splitlines():
        line = clean_line(raw_line)
        ran_match = UNITTEST_RAN.match(line)
        if ran_match:
            ran = int(ran_match.group(1))
            continue
        if ran is not None:
            verdict = unittest_counts(line, ran)
            if verdict:
                total.add(verdict)
                found = True
                ran = None
                continue
        counts = line_counts(line)
        if counts:
            total.add(counts)
            found = True
    if not found:
        return None
    return total

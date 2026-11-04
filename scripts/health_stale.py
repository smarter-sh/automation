#!/usr/bin/env python3
"""The ``stale`` scan: open pull requests and issues older than a number of calendar months.

Part of :mod:`organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library and ``gh``.
"""

import subprocess  # nosec B404: only for CalledProcessError
from datetime import datetime, timezone

from github_cli import api_pages
from health_common import (
    add_finding,
    finding_sort_key,
    is_organization_repository,
    list_repositories,
    new_results,
    record_error,
)
from scan_helpers import months_before


def scan_stale(months: int = 1, now: datetime | None = None) -> dict:
    """List open issues and pull requests created before the calendar-month cutoff.

    :param months: The positive calendar-month age threshold.
    :param now: Optional timezone-aware reference time, used by tests.
    :returns: The scan results with stale items, cutoff, and API error counts.
    :raises ValueError: If the threshold is invalid or ``now`` is naive.
    """
    results = new_results("stale")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_now = now.astimezone(timezone.utc)
    cutoff = months_before(utc_now, months)
    results["cutoff"] = cutoff.isoformat()
    results["months"] = months
    for item in list_repositories(results):
        repository = item.get("full_name")
        if not is_organization_repository(repository):
            continue
        if item.get("archived"):
            continue
        try:
            issues = api_pages(f"repos/{repository}/issues?state=open", None)
        except (subprocess.CalledProcessError, ValueError) as error:
            record_error(results, "Open issues", error)
            continue
        for issue in issues:
            check_stale_issue(results, repository, issue, cutoff)
    results["findings"].sort(key=finding_sort_key)
    return results


def has_issue_metadata(issue: dict) -> bool:
    """Check that an issue has the fields the stale report needs, of the right types.

    :param issue: One issue or pull request from the issues endpoint.
    :returns: Whether its creation time, URL, title, and number are usable.
    """
    if not isinstance(issue.get("created_at"), str):
        return False
    if not isinstance(issue.get("html_url"), str):
        return False
    if not isinstance(issue.get("title"), str):
        return False
    return isinstance(issue.get("number"), int)


def parse_timestamp(value: str) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp.

    :param value: The timestamp, such as ``2026-11-02T12:00:00Z``.
    :returns: The timezone-aware time, or ``None`` if it is invalid or has no timezone.
    """
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def check_stale_issue(results: dict, repository: str, issue: dict, cutoff: datetime) -> None:
    """Report one issue or pull request if it was opened before the cutoff.

    :param results: The mutable scan results.
    :param repository: The repository's ``owner/name`` identifier.
    :param issue: One issue or pull request from the issues endpoint.
    :param cutoff: The UTC time before which an open item is stale.
    """
    if not has_issue_metadata(issue):
        record_error(results, "Issue metadata")
        return
    created_at = parse_timestamp(issue["created_at"])
    if created_at is None:
        record_error(results, "Issue timestamp")
        return
    if created_at.astimezone(timezone.utc) >= cutoff:
        return
    if "pull_request" in issue:
        category = "Pull request"
    else:
        category = "Issue"
    opened_on = created_at.date().isoformat()
    detail = f"#{issue['number']}: {issue['title']} (opened {opened_on})"
    add_finding(results, repository, category, detail, issue["html_url"])

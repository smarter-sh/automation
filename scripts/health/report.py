#!/usr/bin/env python3
"""Write an organization health scan's public summary (counts only) and its Slack message (the details).

Part of :mod:`health.organization_health`, which runs it; see that module for the scans and how
they are run. It uses only Python's standard library.
"""

from pathlib import Path

from health.common import DISPLAY_NAMES
from scan_helpers import slack_escape

SLACK_FINDINGS_LIMIT = 100


def slack_finding_line(finding: dict) -> str:
    """Format one finding as a Slack bullet.

    :param finding: One finding from :func:`add_finding`.
    :returns: The Slack mrkdwn line, with the API-provided text escaped.
    """
    repository = slack_escape(finding["repository"])
    category = slack_escape(finding["category"])
    detail = slack_escape(finding["detail"])
    path = ""
    if finding.get("path"):
        escaped_path = slack_escape(finding["path"])
        path = f" (`{escaped_path}`)"
    return f"• <{finding['url']}|{repository}> · {category}{path}: {detail}"


def slack_error_lines(errors: dict) -> list[str]:
    """Format the scan's error counts for Slack.

    :param errors: The results' error counts, by category.
    :returns: A warning line and one bullet per error category.
    """
    failed_requests = sum(errors.values())
    lines = [f"⚠️ Scan incomplete: {failed_requests} GitHub API request(s) failed."]
    for error, count in sorted(errors.items()):
        escaped_error = slack_escape(error)
        lines.append(f"• {escaped_error}: {count}")
    return lines


def slack_message(results: dict, run_url: str) -> str:
    """Format detailed findings for Slack.

    :param results: Results returned by one of the scan functions.
    :param run_url: The workflow run's URL.
    :returns: A Slack mrkdwn report with findings and any scan errors.
    :raises KeyError: If ``results`` contains an unsupported scan kind.
    """
    title = DISPLAY_NAMES[results["kind"]]
    lines = [f"*{title}*"]
    if results["kind"] == "stale":
        lines.append(
            f"Open pull requests and issues older than {results['months']} month(s) (before {results['cutoff']})."
        )
    findings = results["findings"]
    errors = results["errors"]
    if not findings and not errors:
        lines.append("No findings.")
    else:
        for finding in findings[:SLACK_FINDINGS_LIMIT]:
            lines.append(slack_finding_line(finding))
        omitted = len(findings) - SLACK_FINDINGS_LIMIT
        if omitted > 0:
            lines.append(f"• {omitted} more finding(s) omitted; see the run summary counts.")
        if errors:
            lines.extend(slack_error_lines(errors))
    lines.append(f"<{run_url}|View workflow run>")
    return "\n".join(lines)


def write_report(results: dict, run_url: str, summary_path: str, slack_path: str) -> None:
    """Write a names-free Actions summary and a detailed Slack report.

    :param results: Results returned by one of the scan functions.
    :param run_url: The workflow run's URL.
    :param summary_path: The path to the GitHub Actions summary file.
    :param slack_path: The path to the temporary Slack message file.
    :raises OSError: If either report file cannot be written.
    """
    title = DISPLAY_NAMES[results["kind"]]
    finding_count = len(results["findings"])
    error_count = sum(results["errors"].values())
    summary = [
        f"## {title}",
        "",
        f"- Repositories checked: {results['repositories_scanned']}.",
        f"- Findings: {finding_count}.",
        f"- API/data errors: {error_count}.",
    ]
    if results["kind"] == "stale":
        threshold = f"- Age threshold: older than {results['months']} calendar month(s), before {results['cutoff']}."
        summary.insert(2, threshold)
    summary.append(
        "- Detailed findings are prepared for #general; "
        "repository and issue details are omitted from this public summary."
    )
    summary.append(f"- Run: {run_url}")
    summary_text = "\n".join(summary) + "\n"
    slack_text = slack_message(results, run_url) + "\n"
    Path(summary_path).write_text(summary_text, encoding="utf-8")
    Path(slack_path).write_text(slack_text, encoding="utf-8")

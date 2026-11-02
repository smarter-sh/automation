#!/usr/bin/env python3
"""Review the age metadata of GitHub Actions secrets across smarter-sh.

The scanner reads organization, repository, and environment secret metadata through the GitHub
API. It never requests or writes secret values. Findings are written to a temporary JSON file
for reporting: the Actions summary contains counts only, while the Slack report contains secret
names and their last-updated dates.

Run the scan and report from the repository root::

    python3 -E -s scripts/secret_age_review.py run --months 6 --results results.json
    python3 -E -s scripts/secret_age_review.py report --results results.json \\
        --run-url URL --summary summary.md --slack-output slack.txt [--dry-run]

The scan uses only the Python standard library and the ``gh`` command, authenticated through
``GH_TOKEN``. Tests replace the GitHub API calls and never contact GitHub or Slack.
"""

import argparse
import calendar
import json
import re
import subprocess  # nosec B404: only for CalledProcessError
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from github_cli import ORGANIZATION, error_line, run_json

PAGE_SIZE = 100
SCOPES = {
    "organization": "Organization secrets",
    "repository": "Repository secrets",
    "environment": "Environment secrets",
}


def subtract_months(now: datetime, months: int) -> datetime:
    """Return the calendar date and time ``months`` before an aware datetime.

    :param now: The cutoff reference time, with a timezone.
    :param months: A positive whole number of calendar months.
    :returns: The same local time and timezone on the clamped calendar date.
    :raises ValueError: If ``now`` is naive or ``months`` is not a valid positive offset.
    """
    if months < 1:
        raise ValueError("months must be a positive integer")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_month_index = now.year * 12 + now.month - 1
    target_month_index = current_month_index - months
    year, month_offset = divmod(target_month_index, 12)
    if year < 1:
        raise ValueError("months is too large for the current date")
    month = month_offset + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def updated_datetime(value: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp as an aware UTC datetime.

    :param value: An ISO-8601 timestamp returned by GitHub.
    :returns: The parsed timestamp normalized to UTC.
    :raises ValueError: If the timestamp is invalid or has no timezone.
    """
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("GitHub returned a timestamp without a timezone")
    return parsed.astimezone(timezone.utc)


def api_items(endpoint: str, key: str | None = None) -> list[dict]:
    """Fetch every page from a GitHub REST list endpoint.

    :param endpoint: The API endpoint, optionally with query parameters.
    :param key: The object key containing the list, or ``None`` for a top-level list.
    :returns: The concatenated list of response objects.
    :raises subprocess.CalledProcessError: If a GitHub API request fails.
    :raises ValueError: If GitHub returns an unexpected response shape.
    """
    items = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        response = run_json("gh", "api", f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}")
        if key is None:
            values = response
        elif isinstance(response, dict):
            values = response.get(key)
        else:
            values = None
        if not isinstance(values, list):
            raise ValueError("GitHub returned an unexpected list response")
        if any(not isinstance(item, dict) for item in values):
            raise ValueError("GitHub returned an unexpected list item")
        items.extend(values)
        if len(values) < PAGE_SIZE:
            return items
        page += 1


def api_status(error: subprocess.CalledProcessError) -> str:
    """Return only the HTTP status code from a failed ``gh`` request, if present.

    :param error: The failed ``gh`` command.
    :returns: Its HTTP status code, or ``"unknown"`` if no code is present.
    """
    match = re.search(r"\bHTTP (\d{3})\b", error_line(error))
    return match.group(1) if match else "unknown"


def add_error(results: dict, scope: str, status: str = "invalid response") -> None:
    """Count a failed metadata request without recording repository or secret names.

    :param results: The mutable scan results.
    :param scope: The kind of metadata request that failed.
    :param status: A status code or safe error category.
    """
    key = f"{scope} ({status})"
    results["errors"][key] = results["errors"].get(key, 0) + 1


def collect_items(results: dict, endpoint: str, key: str | None, scope: str) -> list[dict]:
    """Fetch one complete list, recording request failures for the final report.

    :param results: The mutable scan results.
    :param endpoint: The GitHub REST list endpoint.
    :param key: The object key containing the list, or ``None`` for a top-level list.
    :param scope: A safe label used when recording request failures.
    :returns: All response items, or an empty list after a recorded failure.
    """
    try:
        return api_items(endpoint, key)
    except subprocess.CalledProcessError as error:
        add_error(results, scope, api_status(error))
    except ValueError:
        add_error(results, scope)
    return []


def add_findings(results: dict, items: list[dict], scope: str, location: str, cutoff: datetime) -> None:
    """Record old secrets, or mark malformed metadata as an incomplete scan.

    :param results: The mutable scan results.
    :param items: Secret metadata returned by GitHub.
    :param scope: The secret scope, as a key in :data:`SCOPES`.
    :param location: The organization, repository, or repository/environment label.
    :param cutoff: The UTC timestamp before which a secret is considered outdated.
    """
    for item in items:
        try:
            name = item["name"]
            updated_at = item["updated_at"]
            if not isinstance(name, str) or not isinstance(updated_at, str):
                raise ValueError("invalid secret metadata")
            if updated_datetime(updated_at) < cutoff:
                finding = {"scope": scope, "location": location, "name": name, "updated_at": updated_at}
                if scope == "organization":
                    finding["visibility"] = item.get("visibility", "unknown")
                results["findings"].append(finding)
        except (KeyError, ValueError):
            add_error(results, SCOPES[scope])


def scan(months: int, now: datetime | None = None) -> dict:
    """Read organization, repository, and environment secret metadata.

    :param months: The positive calendar-month age threshold.
    :param now: Optional timezone-aware reference time, used by tests.
    :returns: Scan counts, outdated secret metadata, and API error counts.
    :raises ValueError: If the threshold is invalid or ``now`` is naive.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = subtract_months(now.astimezone(timezone.utc), months)
    results = {
        "months": months,
        "cutoff": cutoff.isoformat(),
        "repositories_scanned": 0,
        "environments_scanned": 0,
        "findings": [],
        "errors": {},
    }

    org_secrets = collect_items(results, f"orgs/{ORGANIZATION}/actions/secrets", "secrets", SCOPES["organization"])
    add_findings(results, org_secrets, "organization", ORGANIZATION, cutoff)

    repositories = collect_items(results, f"orgs/{ORGANIZATION}/repos?type=all", None, "Repository inventory")
    results["repositories_scanned"] = len(repositories)
    for repository in repositories:
        name = repository.get("full_name")
        if not isinstance(name, str) or not name.startswith(f"{ORGANIZATION}/"):
            add_error(results, "Repository inventory")
            continue

        secrets = collect_items(results, f"repos/{name}/actions/secrets", "secrets", SCOPES["repository"])
        add_findings(results, secrets, "repository", name, cutoff)

        environments = collect_items(results, f"repos/{name}/environments", "environments", "Environment listing")
        results["environments_scanned"] += len(environments)
        for environment in environments:
            environment_name = environment.get("name")
            if not isinstance(environment_name, str):
                add_error(results, "Environment listing")
                continue
            encoded_name = quote(environment_name, safe="")
            env_secrets = collect_items(
                results,
                f"repos/{name}/environments/{encoded_name}/secrets",
                "secrets",
                SCOPES["environment"],
            )
            add_findings(results, env_secrets, "environment", f"{name} / {environment_name}", cutoff)

    results["findings"].sort(key=lambda item: (item["scope"], item["location"], item["name"]))
    return results


def slack_text(results: dict, run_url: str) -> str:
    """Format the detailed findings for the requested Slack channel.

    :param results: The scan results from :func:`scan`.
    :param run_url: The workflow run's URL.
    :returns: A Slack mrkdwn report with outdated names and last-updated dates.
    """
    months = results["months"]
    cutoff = updated_datetime(results["cutoff"]).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"*GitHub Actions secrets not updated in more than {months} months*", f"Cutoff: {cutoff}."]
    if not results["findings"] and not results["errors"]:
        lines.append("No outdated secrets found.")
    else:
        for scope in ("organization", "repository", "environment"):
            findings = [item for item in results["findings"] if item["scope"] == scope]
            if findings:
                lines.append(f"\n*{SCOPES[scope]}*")
                for item in findings:
                    suffix = f" · visibility: {item['visibility']}" if scope == "organization" else ""
                    updated = updated_datetime(item["updated_at"]).strftime("%Y-%m-%d")
                    lines.append(
                        f"• {slack_escape(item['location'])} · {slack_escape(item['name'])}"
                        f" · last updated {updated}{suffix}"
                    )
        if results["errors"]:
            total = sum(results["errors"].values())
            lines.append(f"\n⚠️ Scan incomplete: {total} GitHub API request(s) failed.")
            for label, count in sorted(results["errors"].items()):
                lines.append(f"• {slack_escape(label)}: {count}")
    lines.append(f"\n<{run_url}|View workflow run>")
    return "\n".join(lines)


def slack_escape(value: str) -> str:
    """Escape API-controlled text so it cannot create Slack links or mentions.

    :param value: A secret name, location, or other API-provided label.
    :returns: The value with HTML-sensitive characters escaped.
    """
    return escape(value, quote=False)


def write_report(results: dict, run_url: str, summary_path: str, slack_path: str, dry_run: bool) -> None:
    """Write a names-free public summary and the detailed Slack message to a temporary file.

    :param results: The scan results from :func:`scan`.
    :param run_url: The workflow run's URL.
    :param summary_path: The path to the GitHub Actions summary file.
    :param slack_path: The path to the temporary Slack message file.
    :param dry_run: Whether the workflow will skip posting to Slack.
    :raises OSError: If either report file cannot be written.
    """
    findings = results["findings"]
    counts = {scope: 0 for scope in SCOPES}
    for item in findings:
        scope = item["scope"]
        counts[scope] += 1
    total = sum(counts.values())
    lines = [
        "## GitHub Actions secret age review",
        "",
        f"- Threshold: not updated in more than {results['months']} months (before {results['cutoff']}).",
        f"- Repositories checked: {results['repositories_scanned']}.",
        f"- Environments checked: {results['environments_scanned']}.",
        f"- Outdated secrets: {total} (organization: {counts['organization']}, "
        f"repository: {counts['repository']}, environment: {counts['environment']}).",
        f"- Metadata request failures: {sum(results['errors'].values())}.",
        f"- The Slack report contains detailed findings; secret names are omitted from this public run summary.",
        f"- Run: {run_url}",
    ]
    if dry_run:
        lines.append("- Dry run: nothing was posted to Slack.")
    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(slack_path).write_text(slack_text(results, run_url) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the scan or write its summary and Slack message.

    :param argv: Optional command-line arguments, excluding the program name.
    :returns: 0 on success; 1 when the scan encountered GitHub API errors.
    :raises OSError: If scan results or reports cannot be read or written.
    :raises ValueError: If the saved scan results are invalid JSON.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("run", help="read secret metadata from GitHub")
    scan_parser.add_argument("--months", type=int, required=True)
    scan_parser.add_argument("--results", required=True)
    report_parser = commands.add_parser("report", help="write the summary and Slack message")
    report_parser.add_argument("--results", required=True)
    report_parser.add_argument("--run-url", required=True)
    report_parser.add_argument("--summary", required=True)
    report_parser.add_argument("--slack-output", required=True)
    report_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.months < 1:
            parser.error("--months must be a positive integer")
        results = scan(args.months)
        Path(args.results).write_text(json.dumps(results), encoding="utf-8")
        return 1 if results["errors"] else 0

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    write_report(results, args.run_url, args.summary, args.slack_output, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

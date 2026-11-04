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
import json
import subprocess  # nosec B404: only for CalledProcessError
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from github_cli import ORGANIZATION, api_pages, http_status
from scan_helpers import months_before, slack_escape

SCOPES = {
    "organization": "Organization secrets",
    "repository": "Repository secrets",
    "environment": "Environment secrets",
}


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


def add_error(results: dict, scope: str, status: str = "invalid response") -> None:
    """Count a failed metadata request without recording repository or secret names.

    :param results: The mutable scan results.
    :param scope: The kind of metadata request that failed.
    :param status: A status code or safe error category.
    """
    key = f"{scope} ({status})"
    errors = results["errors"]
    errors[key] = errors.get(key, 0) + 1


def collect_items(results: dict, endpoint: str, key: str | None, scope: str) -> list[dict]:
    """Fetch one complete list, recording request failures for the final report.

    :param results: The mutable scan results.
    :param endpoint: The GitHub REST list endpoint.
    :param key: The object key containing the list, or ``None`` for a top-level list.
    :param scope: A safe label used when recording request failures.
    :returns: All response items, or an empty list after a recorded failure.
    """
    try:
        return api_pages(endpoint, key)
    except subprocess.CalledProcessError as error:
        add_error(results, scope, http_status(error))
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
            finding = _old_secret(item, scope, location, cutoff)
        except (KeyError, ValueError):
            add_error(results, SCOPES[scope])
            continue
        if finding:
            results["findings"].append(finding)


def _old_secret(item: dict, scope: str, location: str, cutoff: datetime) -> dict | None:
    """Turn one secret's metadata into a finding, when the secret is older than the cutoff.

    :param item: The secret's metadata, as GitHub returned it.
    :param scope: The secret scope, as a key in :data:`SCOPES`.
    :param location: The organization, repository, or repository/environment label.
    :param cutoff: The UTC timestamp before which a secret is considered outdated.
    :returns: The finding, or ``None`` when the secret was updated since the cutoff.
    :raises KeyError: If the metadata has no name or update time.
    :raises ValueError: If the name or update time is malformed.
    """
    name = item["name"]
    updated_at = item["updated_at"]
    if not isinstance(name, str) or not isinstance(updated_at, str):
        raise ValueError("invalid secret metadata")
    updated = updated_datetime(updated_at)
    if updated >= cutoff:
        return None
    finding = {"scope": scope, "location": location, "name": name, "updated_at": updated_at}
    if scope == "organization":
        finding["visibility"] = item.get("visibility", "unknown")
    return finding


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
    now_utc = now.astimezone(timezone.utc)
    cutoff = months_before(now_utc, months)
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
        is_organization_repository = isinstance(name, str) and name.startswith(f"{ORGANIZATION}/")
        if not is_organization_repository:
            add_error(results, "Repository inventory")
            continue
        _scan_repository(results, name, cutoff)

    results["findings"].sort(key=lambda item: (item["scope"], item["location"], item["name"]))
    return results


def _scan_repository(results: dict, name: str, cutoff: datetime) -> None:
    """Read one repository's secret metadata, and its environments'.

    :param results: The mutable scan results.
    :param name: The repository, as ``owner/name``.
    :param cutoff: The UTC timestamp before which a secret is considered outdated.
    """
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
        endpoint = f"repos/{name}/environments/{encoded_name}/secrets"
        env_secrets = collect_items(results, endpoint, "secrets", SCOPES["environment"])
        add_findings(results, env_secrets, "environment", f"{name} / {environment_name}", cutoff)


def slack_text(results: dict, run_url: str) -> str:
    """Format the detailed findings for the requested Slack channel.

    :param results: The scan results from :func:`scan`.
    :param run_url: The workflow run's URL.
    :returns: A Slack mrkdwn report with outdated names and last-updated dates.
    """
    months = results["months"]
    cutoff_time = updated_datetime(results["cutoff"])
    cutoff = cutoff_time.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"*GitHub Actions secrets not updated in more than {months} months*", f"Cutoff: {cutoff}."]
    if not results["findings"] and not results["errors"]:
        lines.append("No outdated secrets found.")
    else:
        for scope in ("organization", "repository", "environment"):
            lines += _slack_scope_lines(results["findings"], scope)
        if results["errors"]:
            lines += _slack_error_lines(results["errors"])
    lines.append(f"\n<{run_url}|View workflow run>")
    return "\n".join(lines)


def _slack_scope_lines(findings: list[dict], scope: str) -> list[str]:
    """Format one scope's findings for Slack: a heading, then one line per outdated secret.

    :param findings: Every finding from :func:`scan`.
    :param scope: The secret scope, as a key in :data:`SCOPES`.
    :returns: The lines, or none when the scope has no findings.
    """
    scope_findings = [item for item in findings if item["scope"] == scope]
    if not scope_findings:
        return []
    lines = [f"\n*{SCOPES[scope]}*"]
    for item in scope_findings:
        suffix = f" · visibility: {item['visibility']}" if scope == "organization" else ""
        updated_time = updated_datetime(item["updated_at"])
        updated = updated_time.strftime("%Y-%m-%d")
        location = slack_escape(item["location"])
        name = slack_escape(item["name"])
        lines.append(f"• {location} · {name} · last updated {updated}{suffix}")
    return lines


def _slack_error_lines(errors: dict) -> list[str]:
    """Format the failed metadata requests for Slack: a total, then one line per kind of failure.

    :param errors: The failure counts from :func:`scan`, by label.
    :returns: The lines.
    """
    total = sum(errors.values())
    lines = [f"\n⚠️ Scan incomplete: {total} GitHub API request(s) failed."]
    for label, count in sorted(errors.items()):
        lines.append(f"• {slack_escape(label)}: {count}")
    return lines


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
    failures = sum(results["errors"].values())
    lines = [
        "## GitHub Actions secret age review",
        "",
        f"- Threshold: not updated in more than {results['months']} months (before {results['cutoff']}).",
        f"- Repositories checked: {results['repositories_scanned']}.",
        f"- Environments checked: {results['environments_scanned']}.",
        f"- Outdated secrets: {total} (organization: {counts['organization']}, "
        f"repository: {counts['repository']}, environment: {counts['environment']}).",
        f"- Metadata request failures: {failures}.",
        "- The Slack report contains detailed findings; secret names are omitted from this public run summary.",
        f"- Run: {run_url}",
    ]
    if dry_run:
        lines.append("- Dry run: nothing was posted to Slack.")
    summary = "\n".join(lines) + "\n"
    slack = slack_text(results, run_url) + "\n"
    Path(summary_path).write_text(summary, encoding="utf-8")
    Path(slack_path).write_text(slack, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run the scan or write its summary and Slack message.

    :param argv: Optional command-line arguments, excluding the program name.
    :returns: 0 on success; 1 when the scan encountered GitHub API errors.
    :raises OSError: If scan results or reports cannot be read or written.
    :raises ValueError: If the saved scan results are invalid JSON.
    """
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.months < 1:
            parser.error("--months must be a positive integer")
        results = scan(args.months)
        text = json.dumps(results)
        Path(args.results).write_text(text, encoding="utf-8")
        return 1 if results["errors"] else 0

    text = Path(args.results).read_text(encoding="utf-8")
    results = json.loads(text)
    write_report(results, args.run_url, args.summary, args.slack_output, args.dry_run)
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the command line's parser: ``run`` or ``report``.

    :returns: The parser, with the subcommand's name in ``command``.
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
    return parser


if __name__ == "__main__":
    sys.exit(main())

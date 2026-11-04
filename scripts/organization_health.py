#!/usr/bin/env python3
"""Report repository security, workflow, package, and stale-item health.

The four scans read metadata from every repository in the ``smarter-sh`` organization:

``security``
    Check Actions token defaults, branch protection, secret scanning, and Dependabot settings
    of the public repositories.
``workflow``
    Check workflow files for full-SHA action pins, explicit token permissions, and job timeouts.
``packages``
    Discover npm, PyPI, and Go modules from package manifests and release workflows; compare
    stable versions and attempt a package install or module download.
``stale``
    Report open pull requests and issues older than a calendar-month threshold.

The scans are read-only apart from installing packages into temporary runner directories.
Findings are saved as JSON for the workflow summary and Slack report. The public Actions summary
contains counts only; detailed repository and item information is written only to Slack.

Run a scan from the repository root::

    python3 -E -s scripts/organization_health.py run security --results results.json
    python3 -E -s scripts/organization_health.py run stale --months 1 --results results.json
    python3 -E -s scripts/organization_health.py report --results results.json \\
        --run-url URL --summary summary.md --slack-output slack.txt

This module is the command line. Each scan is in its own module: ``health_security``,
``health_workflows``, ``health_packages`` and ``health_stale``. ``health_report`` writes the
summary and the Slack message, and ``health_common`` holds what they share.

The script uses the Python standard library and ``gh`` (authenticated through ``GH_TOKEN``).
Tests replace all GitHub, registry, and package-manager calls.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from health_common import new_results
from health_packages import scan_packages
from health_report import write_report
from health_security import scan_security
from health_stale import scan_stale
from health_workflows import scan_workflows


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser, with its ``run`` and ``report`` commands.

    :returns: The argument parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("run", help="scan the organization")
    scan_parser.add_argument("kind", choices=("security", "workflow", "packages", "stale"))
    scan_parser.add_argument("--results", required=True)
    scan_parser.add_argument("--months", type=int, default=1)
    report_parser = commands.add_parser("report", help="write the Actions summary and Slack message")
    report_parser.add_argument("--results", required=True)
    report_parser.add_argument("--run-url", required=True)
    report_parser.add_argument("--summary", required=True)
    report_parser.add_argument("--slack-output", required=True)
    return parser


def run_scan(kind: str, months: int) -> dict:
    """Run one scan, or record a single error if there is no token to run it with.

    :param kind: The scan name, as a key in :data:`DISPLAY_NAMES`.
    :param months: The stale scan's age threshold; unused by the other scans.
    :returns: The scan results.
    """
    if not os.environ.get("GH_TOKEN"):
        # Without a token every API call fails; say so once instead of counting failures.
        results = new_results(kind)
        results["errors"]["GH_TOKEN is empty: is the PAT secret set?"] = 1
        if kind == "stale":
            results["months"] = months
            results["cutoff"] = "n/a"
        return results
    if kind == "stale":
        return scan_stale(months)
    scans = {
        "security": scan_security,
        "workflow": scan_workflows,
        "packages": scan_packages,
    }
    scan = scans[kind]
    return scan()


def log_counts(results: dict) -> None:
    """Print the scan's counts and error categories to standard error.

    :param results: The scan results.
    """
    # Counts and error categories only: the log is public.
    finding_count = len(results["findings"])
    error_count = sum(results["errors"].values())
    print(
        f"{results['repositories_scanned']} repositories checked, {finding_count} finding(s), "
        f"{error_count} API error(s).",
        file=sys.stderr,
    )
    for error, count in sorted(results["errors"].items()):
        print(f"error: {error}: {count}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run one report, writing its data to a temporary file.

    :param argv: Optional command-line arguments, excluding the program name.
    :returns: 0 on a complete scan, findings or not; 1 if any GitHub API request failed.
    :raises OSError: If the results or report files cannot be read or written.
    :raises ValueError: If the requested scan parameters or saved JSON are invalid.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "report":
        saved_results = Path(args.results).read_text(encoding="utf-8")
        results = json.loads(saved_results)
        write_report(results, args.run_url, args.summary, args.slack_output)
        return 0

    if args.kind == "stale" and args.months < 1:
        parser.error("--months must be a positive integer")
    results = run_scan(args.kind, args.months)
    results_json = json.dumps(results)
    Path(args.results).write_text(results_json, encoding="utf-8")
    log_counts(results)
    # Findings are the report's content and go to Slack; only an incomplete scan fails the run.
    if results["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

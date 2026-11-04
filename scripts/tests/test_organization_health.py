"""Tests of scripts/organization_health.py, the command line of the organization health reports, and of its report.

No test reaches GitHub: the scans are replaced by fakes.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import health_report
import organization_health as health
from tests.health_fixtures import REPOSITORY


class TestReports(unittest.TestCase):
    """Private repository/item details stay out of public Actions summaries."""

    def test_public_summary_omits_detailed_findings(self):
        results = {
            "kind": "stale",
            "repositories_scanned": 1,
            "months": 1,
            "cutoff": "2026-10-02T12:00:00+00:00",
            "findings": [
                {
                    "repository": "smarter-sh/private-repo",
                    "category": "Issue",
                    "detail": "#1: private issue title",
                    "url": "https://github.com/smarter-sh/private-repo/issues/1",
                    "path": "",
                }
            ],
            "errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            slack_path = Path(directory) / "slack.txt"
            health_report.write_report(results, "https://example.test/run", str(summary_path), str(slack_path))
            summary = summary_path.read_text(encoding="utf-8")
            slack = slack_path.read_text(encoding="utf-8")
        self.assertNotIn("private-repo", summary)
        self.assertNotIn("private issue title", summary)
        self.assertIn("private issue title", slack)

    def test_report_command_reads_saved_results_without_rescanning(self):
        results = {
            "kind": "security",
            "repositories_scanned": 1,
            "findings": [],
            "errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            results_path.write_text(json.dumps(results), encoding="utf-8")
            with (
                mock.patch.object(health, "scan_security") as scan,
                mock.patch.object(health, "write_report") as report,
            ):
                status = health.main(
                    [
                        "report",
                        "--results",
                        str(results_path),
                        "--run-url",
                        "https://example.test/run",
                        "--summary",
                        str(Path(directory) / "summary.md"),
                        "--slack-output",
                        str(Path(directory) / "slack.txt"),
                    ]
                )
        self.assertEqual(status, 0)
        scan.assert_not_called()
        report.assert_called_once()

    def test_scan_command_passes_with_findings_and_fails_for_errors(self):
        cases = (
            ({}, 0),
            ({"Branch protection (500)": 1}, 1),
        )
        for errors, expected in cases:
            results = {
                "kind": "security",
                "repositories_scanned": 1,
                "findings": [{"repository": REPOSITORY}],
                "errors": errors,
            }
            with tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch.dict(health.os.environ, {"GH_TOKEN": "test-token"}),
                    mock.patch.object(health, "scan_security", return_value=results),
                    mock.patch("sys.stderr", new_callable=io.StringIO),
                ):
                    status = health.main(["run", "security", "--results", str(Path(directory) / "results.json")])
            self.assertEqual(status, expected, errors)

    def test_scan_command_writes_results(self):
        results = {
            "kind": "security",
            "repositories_scanned": 1,
            "findings": [{"repository": REPOSITORY}],
            "errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            with (
                mock.patch.dict(health.os.environ, {"GH_TOKEN": "test-token"}),
                mock.patch.object(health, "scan_security", return_value=results),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                health.main(["run", "security", "--results", str(results_path)])
            saved = results_path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved), results)

    def test_scan_command_reports_an_empty_token_without_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            with (
                mock.patch.dict(health.os.environ, {"GH_TOKEN": ""}),
                mock.patch.object(health, "scan_security") as scan,
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                status = health.main(["run", "security", "--results", str(results_path)])
            saved = results_path.read_text(encoding="utf-8")
        results = json.loads(saved)
        self.assertEqual(status, 1)
        scan.assert_not_called()
        self.assertEqual(results["kind"], "security")
        self.assertEqual(list(results["errors"]), ["GH_TOKEN is empty: is the PAT secret set?"])
        self.assertIn("PAT secret", stderr.getvalue())

    def test_empty_token_results_still_make_a_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            with (
                mock.patch.dict(health.os.environ, {"GH_TOKEN": ""}),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                health.main(["run", "stale", "--results", str(results_path)])
            saved = results_path.read_text(encoding="utf-8")
        results = json.loads(saved)
        message = health_report.slack_message(results, "https://example.test/run")
        self.assertIn("Scan incomplete", message)


if __name__ == "__main__":
    unittest.main()

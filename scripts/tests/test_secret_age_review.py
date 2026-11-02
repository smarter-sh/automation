"""Tests for the GitHub Actions secret age review."""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import secret_age_review as review

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class TestCutoff(unittest.TestCase):
    """Calendar-month thresholds handle month ends and exact boundaries."""

    def test_subtracts_calendar_months(self):
        self.assertEqual(
            review.subtract_months(datetime(2026, 8, 31, tzinfo=timezone.utc), 6),
            datetime(2026, 2, 28, tzinfo=timezone.utc),
        )

    def test_rejects_invalid_months(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            review.subtract_months(NOW, 0)

    def test_timestamp_requires_timezone(self):
        with self.assertRaisesRegex(ValueError, "without a timezone"):
            review.updated_datetime("2026-01-01T00:00:00")

    def test_api_items_follows_pages(self):
        first_page = [{"name": str(index)} for index in range(review.PAGE_SIZE)]
        with mock.patch.object(review, "run_json", side_effect=[first_page, [{"name": "last"}]]) as api:
            items = review.api_items("orgs/smarter-sh/repos?type=all")
        self.assertEqual(len(items), review.PAGE_SIZE + 1)
        self.assertIn("repos?type=all&per_page=100&page=1", api.call_args_list[0].args[2])
        self.assertIn("repos?type=all&per_page=100&page=2", api.call_args_list[1].args[2])


class TestScan(unittest.TestCase):
    """The scan reads all secret scopes and marks API failures incomplete."""

    def test_reads_org_repo_and_environment_secrets_with_pagination(self):
        repo_list = [
            {"full_name": "smarter-sh/a"},
            {"full_name": "smarter-sh/b"},
        ]
        responses = {
            "orgs/smarter-sh/actions/secrets?per_page=100&page=1": {
                "secrets": [{"name": "ORG_OLD", "updated_at": "2025-01-01T00:00:00Z", "visibility": "all"}]
            },
            "orgs/smarter-sh/repos?type=all&per_page=100&page=1": repo_list,
            "repos/smarter-sh/a/actions/secrets?per_page=100&page=1": {
                "secrets": [{"name": "REPO_OLD", "updated_at": "2025-01-01T00:00:00Z"}]
            },
            "repos/smarter-sh/a/environments?per_page=100&page=1": {"environments": [{"name": "prod/stage"}]},
            "repos/smarter-sh/a/environments/prod%2Fstage/secrets?per_page=100&page=1": {
                "secrets": [{"name": "ENV_OLD", "updated_at": "2025-01-01T00:00:00Z"}]
            },
            "repos/smarter-sh/b/actions/secrets?per_page=100&page=1": {"secrets": []},
            "repos/smarter-sh/b/environments?per_page=100&page=1": {"environments": []},
        }
        with mock.patch.object(review, "run_json", side_effect=lambda *args: responses[args[-1]]):
            results = review.scan(6, NOW)

        self.assertEqual(results["repositories_scanned"], 2)
        self.assertEqual(results["environments_scanned"], 1)
        self.assertEqual(
            [(item["scope"], item["name"]) for item in results["findings"]],
            [("environment", "ENV_OLD"), ("organization", "ORG_OLD"), ("repository", "REPO_OLD")],
        )
        self.assertFalse(results["errors"])

    def test_exact_cutoff_is_not_old(self):
        exact_cutoff = review.subtract_months(NOW, 6).isoformat()
        with mock.patch.object(
            review,
            "run_json",
            side_effect=[{"secrets": [{"name": "RECENT", "updated_at": exact_cutoff}]}, []],
        ):
            results = review.scan(6, NOW)
        self.assertEqual(results["findings"], [])

    def test_api_errors_are_counted(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403: Forbidden")
        with mock.patch.object(review, "run_json", side_effect=error):
            results = review.scan(6, NOW)
        self.assertEqual(results["errors"]["Organization secrets (403)"], 1)
        self.assertEqual(results["errors"]["Repository inventory (403)"], 1)


class TestReport(unittest.TestCase):
    """The Slack report includes findings; the public Actions summary does not."""

    def test_summary_omits_secret_names_and_slack_report_includes_them(self):
        results = {
            "months": 6,
            "cutoff": "2026-05-02T12:00:00+00:00",
            "repositories_scanned": 1,
            "environments_scanned": 0,
            "findings": [
                {
                    "scope": "repository",
                    "location": "smarter-sh/private-repo",
                    "name": "DEPLOY_TOKEN",
                    "updated_at": "2025-01-01T00:00:00Z",
                }
            ],
            "errors": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            slack_path = Path(directory) / "slack.txt"
            review.write_report(results, "https://example.test/run", str(summary_path), str(slack_path), False)
            summary = summary_path.read_text(encoding="utf-8")
            slack = slack_path.read_text(encoding="utf-8")
        self.assertNotIn("DEPLOY_TOKEN", summary)
        self.assertNotIn("private-repo", summary)
        self.assertIn("DEPLOY_TOKEN", slack)
        self.assertIn("last updated 2025-01-01", slack)

    def test_slack_names_are_escaped(self):
        self.assertNotIn("<!here>", review.slack_escape("<!here>"))
        self.assertEqual("DEPLOY_TOKEN", review.slack_escape("DEPLOY_TOKEN"))


if __name__ == "__main__":
    unittest.main()

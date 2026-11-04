"""Tests of scripts/health_stale.py, the ``stale`` scan.

No test reaches GitHub.
"""

import unittest
from unittest import mock

import github_cli
import health_stale as stale
from tests.health_fixtures import NOW, REPOSITORY


class TestStaleScan(unittest.TestCase):
    """The stale report includes old open issues and pull requests only."""

    def test_uses_calendar_month_threshold_and_skips_archived(self):
        old = "2026-09-01T12:00:00Z"
        recent = "2026-10-10T12:00:00Z"

        def fake_api(*args):
            endpoint = args[2]
            if endpoint.startswith("orgs/"):
                return [
                    {"full_name": REPOSITORY, "archived": False},
                    {"full_name": "smarter-sh/archived", "archived": True},
                ]
            if endpoint.startswith(f"repos/{REPOSITORY}/issues"):
                return [
                    {"number": 1, "title": "Old issue", "html_url": "https://example.test/1", "created_at": old},
                    {
                        "number": 2,
                        "title": "Old PR",
                        "html_url": "https://example.test/2",
                        "created_at": old,
                        "pull_request": {},
                    },
                    {"number": 3, "title": "Recent", "html_url": "https://example.test/3", "created_at": recent},
                ]
            raise AssertionError(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = stale.scan_stale(1, NOW)
        categories = {item["category"] for item in results["findings"]}
        self.assertEqual(len(results["findings"]), 2)
        self.assertEqual(categories, {"Issue", "Pull request"})


if __name__ == "__main__":
    unittest.main()

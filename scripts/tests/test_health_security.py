"""Tests of scripts/health/security.py, the ``security`` scan.

No test reaches GitHub.
"""

import unittest
from unittest import mock

import github_cli
from health import security as security
from tests.health_fixtures import REPOSITORY, api_error


class TestSecurityScan(unittest.TestCase):
    """Repository security settings are classified without reaching GitHub."""

    def api(self, endpoint):
        if endpoint.startswith("orgs/"):
            return [{"full_name": REPOSITORY, "default_branch": "main", "private": False}]
        if endpoint == f"repos/{REPOSITORY}":
            return {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}}
        if endpoint.endswith("/actions/permissions/workflow"):
            return {"default_workflow_permissions": "read"}
        if endpoint.endswith("/vulnerability-alerts") or endpoint.endswith("/automated-security-fixes"):
            return None
        if endpoint.endswith("/branches/main/protection"):
            # What the repositories use: no required pull requests, administrators exempt.
            return {
                "required_pull_request_reviews": None,
                "enforce_admins": {"enabled": False},
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
        raise AssertionError(endpoint)

    def fake_run_json(self, *args):
        """Answer a ``gh api`` call with the expected settings."""
        endpoint = args[2]
        return self.api(endpoint)

    def test_no_findings_for_expected_settings(self):
        with mock.patch.object(github_cli, "run_json", side_effect=self.fake_run_json):
            results = security.scan_security()
        self.assertEqual(results["findings"], [])
        self.assertEqual(results["errors"], {})

    def test_reports_disabled_settings(self):
        def fake_api(*args):
            endpoint = args[2]
            if endpoint.startswith("orgs/"):
                return [{"full_name": REPOSITORY, "default_branch": "main", "private": False}]
            if endpoint == f"repos/{REPOSITORY}":
                return {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}
            if endpoint.endswith("/actions/permissions/workflow"):
                return {"default_workflow_permissions": "write"}
            if endpoint.endswith("/vulnerability-alerts") or endpoint.endswith("/automated-security-fixes"):
                raise api_error(404)
            if endpoint.endswith("/branches/main/protection"):
                raise api_error(404)
            raise AssertionError(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = security.scan_security()
        self.assertEqual(len(results["findings"]), 5)

    def test_force_pushes_and_deletion_are_reported(self):
        def fake_api(*args):
            endpoint = args[2]
            if endpoint.endswith("/branches/main/protection"):
                return {"allow_force_pushes": {"enabled": True}, "allow_deletions": {"enabled": True}}
            return self.api(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = security.scan_security()
        details = [finding["detail"] for finding in results["findings"]]
        self.assertEqual(
            details,
            ["Force pushes are allowed on the default branch.", "The default branch can be deleted."],
        )

    def test_private_repositories_are_skipped(self):
        private = "smarter-sh/private-example"

        def fake_api(*args):
            endpoint = args[2]
            if endpoint.startswith("orgs/"):
                return [
                    {"full_name": REPOSITORY, "default_branch": "main", "private": False},
                    {"full_name": private, "default_branch": "main", "private": True},
                ]
            if endpoint.startswith(f"repos/{private}"):
                raise AssertionError(f"private repository was scanned: {endpoint}")
            return self.api(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = security.scan_security()
        self.assertEqual(results["repositories_scanned"], 1)
        self.assertEqual(results["findings"], [])
        self.assertEqual(results["errors"], {})

    def test_branch_protection_403s_are_errors(self):
        def fake_api(*args):
            endpoint = args[2]
            if endpoint.endswith("/branches/main/protection"):
                raise api_error(403)
            return self.api(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = security.scan_security()
        self.assertEqual(results["errors"], {"Branch protection (403)": 1})


if __name__ == "__main__":
    unittest.main()

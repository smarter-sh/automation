"""Tests of scripts/health/workflows.py, the ``workflow`` scan.

No test reaches GitHub.
"""

import base64
import unittest
from unittest import mock

import github_cli
from health import workflows as workflows
from tests.health_fixtures import REPOSITORY


class TestWorkflowFindings(unittest.TestCase):
    """Workflow analysis distinguishes pinned/read-only jobs from risky patterns."""

    def test_clean_workflow(self):
        source = (
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  test:\n"
            "    timeout-minutes: 10\n"
            "    steps:\n"
            "      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n"
        )
        self.assertEqual(workflows.workflow_findings(source), [])

    def test_reports_unpinned_action_write_permission_and_missing_timeout(self):
        source = (
            "permissions:\n"
            "  contents: write\n"
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v7\n"
        )
        findings = workflows.workflow_findings(source)
        categories = [category for category, _ in findings]
        self.assertEqual(categories, ["Unpinned action", "Token permissions", "Missing timeout"])

    def test_job_write_with_reason_is_accepted(self):
        source = (
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  publish:\n"
            "    timeout-minutes: 5\n"
            "    permissions:\n"
            "      contents: read\n"
            "      id-token: write # trusted publishing (OIDC)\n"
            "    steps:\n"
            "      - run: echo\n"
            "        with:\n"
            "          mode: write\n"
        )
        self.assertEqual(workflows.workflow_findings(source), [])

    def test_job_write_without_reason_is_reported(self):
        source = (
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  publish:\n"
            "    timeout-minutes: 5\n"
            "    permissions:\n"
            "      id-token: write\n"
        )
        detail = "Job `publish` grants `id-token: write` without a comment that says why."
        self.assertEqual(workflows.workflow_findings(source), [("Token permissions", detail)])

    def test_workflow_level_write_with_reason_is_still_reported(self):
        source = (
            "permissions:\n"
            "  contents: read\n"
            "  issues: write # comments on issues\n"
            "\n"
            "jobs:\n"
            "  test:\n"
            "    timeout-minutes: 5\n"
        )
        self.assertEqual(
            workflows.workflow_findings(source),
            [("Token permissions", "The workflow grants write permissions to every job.")],
        )

    def test_write_all_is_reported(self):
        source = "permissions: write-all\njobs:\n  test:\n    timeout-minutes: 5\n"
        self.assertEqual(
            workflows.workflow_findings(source),
            [("Token permissions", "The workflow grants write-all permissions.")],
        )

    def test_reusable_workflow_call_needs_no_timeout(self):
        source = (
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  ci:\n"
            "    name: CI\n"
            "    uses: ./.github/workflows/ci.yml\n"
            "    secrets: inherit\n"
        )
        self.assertEqual(workflows.workflow_findings(source), [])

    def test_missing_permissions_are_reported(self):
        findings = workflows.workflow_findings("jobs:\n  test:\n    timeout-minutes: 5\n")
        self.assertIn(("Token permissions", "The workflow does not declare explicit token permissions."), findings)

    def test_explicit_job_permissions_are_accepted(self):
        source = "jobs:\n  test:\n    permissions:\n      contents: read\n    timeout-minutes: 5\n"
        self.assertEqual(workflows.workflow_findings(source), [])


class TestWorkflowScan(unittest.TestCase):
    """Workflow source discovery is paginated and decoded from GitHub responses."""

    def test_scans_workflow_tree_and_file(self):
        source = (
            "permissions:\n  contents: read\n"
            "jobs:\n  test:\n    timeout-minutes: 5\n"
            "    steps:\n      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n"
        )
        encoded = base64.b64encode(source.encode()).decode()

        def fake_api(*args):
            endpoint = args[2]
            if endpoint.startswith("orgs/"):
                return [{"full_name": REPOSITORY, "default_branch": "main"}]
            if endpoint.endswith("/git/trees/main?recursive=1"):
                return {"tree": [{"path": ".github/workflows/test.yml"}]}
            if endpoint.startswith(f"repos/{REPOSITORY}/contents/"):
                return {"content": encoded}
            raise AssertionError(endpoint)

        with mock.patch.object(github_cli, "run_json", side_effect=fake_api):
            results = workflows.scan_workflows()
        self.assertEqual(results["findings"], [])
        self.assertEqual(results["errors"], {})


if __name__ == "__main__":
    unittest.main()

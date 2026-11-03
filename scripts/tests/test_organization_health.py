"""Tests for the organization-wide health reports; no test contacts external services."""

import base64
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import organization_health as health

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
REPOSITORY = "smarter-sh/example"


def api_error(status):
    """Build a fake GitHub API failure with an HTTP status."""
    return subprocess.CalledProcessError(1, ["gh"], stderr=f"HTTP {status}: fake")


class TestCutoff(unittest.TestCase):
    """Calendar month cutoffs handle month ends and reject naive times."""

    def test_calendar_month_end(self):
        august = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(health.cutoff_months(august, 6), datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc))

    def test_requires_aware_time_and_positive_months(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            health.cutoff_months(datetime(2026, 11, 2), 1)
        with self.assertRaisesRegex(ValueError, "positive"):
            health.cutoff_months(NOW, 0)


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
        self.assertEqual(health.workflow_findings(source), [])

    def test_reports_unpinned_action_write_permission_and_missing_timeout(self):
        source = (
            "permissions:\n"
            "  contents: write\n"
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v7\n"
        )
        findings = health.workflow_findings(source)
        self.assertEqual(
            [category for category, _ in findings], ["Unpinned action", "Token permissions", "Missing timeout"]
        )

    def test_missing_permissions_are_reported(self):
        findings = health.workflow_findings("jobs:\n  test:\n    timeout-minutes: 5\n")
        self.assertIn(("Token permissions", "The workflow does not declare explicit token permissions."), findings)

    def test_explicit_job_permissions_are_accepted(self):
        source = "jobs:\n  test:\n    permissions:\n      contents: read\n    timeout-minutes: 5\n"
        self.assertEqual(health.workflow_findings(source), [])


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

    def test_no_findings_for_expected_settings(self):
        with mock.patch.object(health, "run_json", side_effect=lambda *args: self.api(args[2])):
            results = health.scan_security()
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

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_security()
        self.assertEqual(len(results["findings"]), 5)

    def test_force_pushes_and_deletion_are_reported(self):
        def fake_api(*args):
            endpoint = args[2]
            if endpoint.endswith("/branches/main/protection"):
                return {"allow_force_pushes": {"enabled": True}, "allow_deletions": {"enabled": True}}
            return self.api(endpoint)

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_security()
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

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_security()
        self.assertEqual(results["repositories_scanned"], 1)
        self.assertEqual(results["findings"], [])
        self.assertEqual(results["errors"], {})

    def test_branch_protection_403s_are_errors(self):
        def fake_api(*args):
            endpoint = args[2]
            if endpoint.endswith("/branches/main/protection"):
                raise api_error(403)
            return self.api(endpoint)

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_security()
        self.assertEqual(results["errors"], {"Branch protection (403)": 1})


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

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_workflows()
        self.assertEqual(results["findings"], [])
        self.assertEqual(results["errors"], {})


class TestPackageDiscovery(unittest.TestCase):
    """Package mappings come from package manifests and release metadata."""

    def test_stable_version_check_and_sort_key(self):
        self.assertTrue(health.is_stable_version("v1.2.3"))
        self.assertFalse(health.is_stable_version("v1.2.3-beta.1"))
        self.assertFalse(health.is_stable_version("1.2"))
        self.assertEqual(health.version_key("v1.10.3"), (1, 10, 3))

    def test_discovers_only_published_npm_python_and_go_packages(self):
        root = [{"name": "package.json"}, {"name": "pyproject.toml"}, {"name": "go.mod"}]
        manifests = {
            "package.json": json.dumps({"name": "@smarter.sh/example", "version": "1.2.3"}),
            "pyproject.toml": '[project]\nname = "example-py"\n',
            "go.mod": "module github.com/smarter-sh/example\n\ngo 1.22\n",
        }
        with mock.patch.object(health, "manifest_value", side_effect=lambda _repo, path: manifests[path]):
            candidates = health.package_candidates(
                REPOSITORY, root, "v1.2.3", "npm publish\npypa/gh-action-pypi-publish"
            )
        self.assertEqual(
            {(item["ecosystem"], item["name"]) for item in candidates},
            {
                ("npm", "@smarter.sh/example"),
                ("pypi", "example-py"),
                ("go", "github.com/smarter-sh/example"),
            },
        )
        self.assertTrue(all(item["expected_version"] == "1.2.3" for item in candidates))

    def test_skips_private_and_unpublished_npm_packages(self):
        root = [{"name": "package.json"}]
        private_manifest = json.dumps({"name": "private-package", "private": True})
        unpublished_manifest = json.dumps({"name": "not-published"})
        with mock.patch.object(health, "manifest_value", return_value=private_manifest):
            self.assertEqual(health.package_candidates(REPOSITORY, root, "v1.0.0", "npm publish"), [])
        with mock.patch.object(health, "manifest_value", return_value=unpublished_manifest):
            self.assertEqual(health.package_candidates(REPOSITORY, root, "v1.0.0", "no release step"), [])

    def test_registry_latest_stable_versions(self):
        metadata = {
            "dist-tags": {"latest": "2.0.0-beta.1"},
            "versions": {"1.2.3": {}, "2.0.0-beta.1": {}},
        }
        with mock.patch.object(health, "registry_json", return_value=metadata):
            self.assertEqual(health.registry_version("npm", "sample-package"), "1.2.3")
        pypi = {
            "info": {"version": "2.0.0rc1"},
            "releases": {"1.2.3": [{}], "2.0.0rc1": [{}]},
        }
        with mock.patch.object(health, "registry_json", return_value=pypi):
            self.assertEqual(health.registry_version("pypi", "sample-package"), "1.2.3")

    def test_package_install_commands_are_faked(self):
        with mock.patch.object(health.subprocess, "run") as process:
            health.install_package("npm", "sample-package", "1.2.3")
            health.install_package("pypi", "sample-package", "1.2.3")
        self.assertEqual(process.call_count, 2)
        self.assertIn("--ignore-scripts", process.call_args_list[0].args[0])
        self.assertIn("--no-deps", process.call_args_list[1].args[0])


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

        with mock.patch.object(health, "run_json", side_effect=fake_api):
            results = health.scan_stale(1, NOW)
        self.assertEqual(len(results["findings"]), 2)
        self.assertEqual({item["category"] for item in results["findings"]}, {"Issue", "Pull request"})


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
            health.write_report(results, "https://example.test/run", str(summary_path), str(slack_path))
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
        for errors, expected in (({}, 0), ({"Branch protection (500)": 1}, 1)):
            results = {"kind": "security", "repositories_scanned": 1, "findings": [{"repository": REPOSITORY}]}
            results["errors"] = errors
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
            self.assertEqual(json.loads(results_path.read_text(encoding="utf-8")), results)

    def test_scan_command_reports_an_empty_token_without_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            with (
                mock.patch.dict(health.os.environ, {"GH_TOKEN": ""}),
                mock.patch.object(health, "scan_security") as scan,
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                status = health.main(["run", "security", "--results", str(results_path)])
            results = json.loads(results_path.read_text(encoding="utf-8"))
        self.assertEqual(status, 1)
        scan.assert_not_called()
        self.assertEqual(results["kind"], "security")
        self.assertEqual(
            list(results["errors"]),
            ["GH_TOKEN is empty: is the PAT secret set?"],
        )
        self.assertIn("PAT secret", stderr.getvalue())

    def test_empty_token_results_still_make_a_stale_report(self):
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            with (
                mock.patch.dict(health.os.environ, {"GH_TOKEN": ""}),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                health.main(["run", "stale", "--results", str(results_path)])
            message = health.slack_message(
                json.loads(results_path.read_text(encoding="utf-8")), "https://example.test/run"
            )
        self.assertIn("Scan incomplete", message)


if __name__ == "__main__":
    unittest.main()

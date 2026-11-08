"""Tests of scripts/health/packages.py, the ``packages`` scan.

No test reaches GitHub, a registry or a package manager.
"""

import json
import unittest
from unittest import mock

from health import packages as health_packages
from tests.health_fixtures import REPOSITORY, api_error


class TestPackageDiscovery(unittest.TestCase):
    """Package mappings come from package manifests and release metadata."""

    def test_stable_version_check_and_sort_key(self):
        self.assertTrue(health_packages.is_stable_version("v1.2.3"))
        self.assertFalse(health_packages.is_stable_version("v1.2.3-beta.1"))
        self.assertFalse(health_packages.is_stable_version("1.2"))
        self.assertEqual(health_packages.version_key("v1.10.3"), (1, 10, 3))

    def test_discovers_only_published_npm_python_and_go_packages(self):
        root = [{"name": "package.json"}, {"name": "pyproject.toml"}, {"name": "go.mod"}]
        manifests = {
            "package.json": json.dumps({"name": "@smarter.sh/example", "version": "1.2.3"}),
            "pyproject.toml": '[project]\nname = "example-py"\n',
            "go.mod": "module github.com/smarter-sh/example\n\ngo 1.22\n",
        }
        with mock.patch.object(health_packages, "manifest_value", side_effect=lambda _repo, path: manifests[path]):
            candidates = health_packages.package_candidates(
                REPOSITORY, root, "v1.2.3", "npm publish\npypa/gh-action-pypi-publish"
            )
        packages = {(item["ecosystem"], item["name"]) for item in candidates}
        expected_versions = {item["expected_version"] for item in candidates}
        self.assertEqual(
            packages,
            {
                ("npm", "@smarter.sh/example"),
                ("pypi", "example-py"),
                ("go", "github.com/smarter-sh/example"),
            },
        )
        self.assertEqual(expected_versions, {"1.2.3"})

    def test_skips_private_and_unpublished_npm_packages(self):
        root = [{"name": "package.json"}]
        private_manifest = json.dumps({"name": "private-package", "private": True})
        unpublished_manifest = json.dumps({"name": "not-published"})
        with mock.patch.object(health_packages, "manifest_value", return_value=private_manifest):
            private_candidates = health_packages.package_candidates(REPOSITORY, root, "v1.0.0", "npm publish")
        with mock.patch.object(health_packages, "manifest_value", return_value=unpublished_manifest):
            unpublished_candidates = health_packages.package_candidates(REPOSITORY, root, "v1.0.0", "no release step")
        self.assertEqual(private_candidates, [])
        self.assertEqual(unpublished_candidates, [])

    def test_discovers_public_npm_workspace_packages_without_a_version(self):
        root = [{"name": "package.json"}, {"name": "packages", "type": "dir"}]
        manifests = {
            "package.json": json.dumps({"name": "console", "private": True, "workspaces": ["packages/*"]}),
            "packages/chat/package.json": json.dumps(
                {"name": "@smarter.sh/ui-chat", "publishConfig": {"access": "public"}}
            ),
            "packages/common/package.json": json.dumps({"name": "@smarter/common", "private": True}),
        }
        folder = [
            {"path": "packages/chat", "type": "dir"},
            {"path": "packages/common", "type": "dir"},
            {"path": "packages/no-manifest", "type": "dir"},
            {"path": "packages/README.md", "type": "file"},
        ]

        def manifest(_repository, path):
            if path not in manifests:
                raise api_error(404)
            return manifests[path]

        with (
            mock.patch.object(health_packages, "manifest_value", side_effect=manifest),
            mock.patch.object(health_packages, "api_pages", return_value=folder) as pages,
        ):
            candidates = health_packages.package_candidates(REPOSITORY, root, "v1.0.18", "npm publish")
        pages.assert_called_once_with(f"repos/{REPOSITORY}/contents/packages")
        self.assertEqual(
            candidates,
            [
                {
                    "ecosystem": "npm",
                    "name": "@smarter.sh/ui-chat",
                    "repository": REPOSITORY,
                    "expected_version": None,
                }
            ],
        )

    def test_workspace_folder_patterns(self):
        yarn_manifest = json.dumps({"workspaces": {"packages": ["packages/*"]}})
        self.assertEqual(health_packages.workspace_folders(yarn_manifest), ["packages/*"])
        self.assertEqual(health_packages.workspace_folders(json.dumps({"name": "x"})), [])
        self.assertEqual(health_packages.workspace_package_paths(REPOSITORY, "tools/cli/"), ["tools/cli"])
        self.assertEqual(health_packages.workspace_package_paths(REPOSITORY, "packages/**"), [])

    def test_workspace_package_skips_the_release_comparison_but_installs(self):
        results = health_packages.new_results("packages")
        candidate = {"ecosystem": "npm", "name": "@smarter.sh/ui-chat", "expected_version": None}
        with (
            mock.patch.object(health_packages, "registry_version", return_value="1.0.3"),
            mock.patch.object(health_packages, "install_package") as install,
        ):
            health_packages.check_package(results, REPOSITORY, candidate)
        install.assert_called_once_with("npm", "@smarter.sh/ui-chat", "1.0.3")
        self.assertEqual(results["findings"], [])

    def test_root_package_still_reports_a_version_mismatch(self):
        results = health_packages.new_results("packages")
        candidate = {"ecosystem": "npm", "name": "sample-package", "expected_version": "1.2.3"}
        with (
            mock.patch.object(health_packages, "registry_version", return_value="1.2.0"),
            mock.patch.object(health_packages, "install_package"),
        ):
            health_packages.check_package(results, REPOSITORY, candidate)
        categories = [finding["category"] for finding in results["findings"]]
        self.assertEqual(categories, ["npm version mismatch"])

    def test_registry_latest_stable_versions(self):
        metadata = {
            "dist-tags": {"latest": "2.0.0-beta.1"},
            "versions": {"1.2.3": {}, "2.0.0-beta.1": {}},
        }
        with mock.patch.object(health_packages, "registry_json", return_value=metadata):
            npm_version = health_packages.registry_version("npm", "sample-package")
        self.assertEqual(npm_version, "1.2.3")
        pypi = {
            "info": {"version": "2.0.0rc1"},
            "releases": {"1.2.3": [{}], "2.0.0rc1": [{}]},
        }
        with mock.patch.object(health_packages, "registry_json", return_value=pypi):
            pypi_version = health_packages.registry_version("pypi", "sample-package")
        self.assertEqual(pypi_version, "1.2.3")

    def test_package_install_commands_are_faked(self):
        with mock.patch.object(health_packages.subprocess, "run") as process:
            health_packages.install_package("npm", "sample-package", "1.2.3")
            health_packages.install_package("pypi", "sample-package", "1.2.3")
        self.assertEqual(process.call_count, 2)
        self.assertIn("--ignore-scripts", process.call_args_list[0].args[0])
        self.assertIn("--no-deps", process.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()

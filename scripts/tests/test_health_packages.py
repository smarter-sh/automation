"""Tests of scripts/health/packages.py, the ``packages`` scan.

No test reaches GitHub, a registry or a package manager.
"""

import json
import unittest
from unittest import mock

from health import packages as health_packages
from tests.health_fixtures import REPOSITORY


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

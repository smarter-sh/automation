"""
Tests of scripts/github_cli.py, the git and gh runner that the workflows' scripts share.

They never run git or gh: ``subprocess.run`` is replaced by a fake.
"""

import subprocess  # nosec B404: only to build CompletedProcess and CalledProcessError
import unittest
from unittest import mock

import github_cli


def completed(stdout: str) -> subprocess.CompletedProcess:
    """A finished command that printed stdout."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TestRun(unittest.TestCase):
    """Run() calls subprocess without a shell, and returns the output."""

    def test_strips_by_default(self):
        with mock.patch.object(github_cli.subprocess, "run", return_value=completed(" url\n")) as fake:
            self.assertEqual(github_cli.run("gh", "pr", "create", input_text="body"), "url")
        self.assertEqual(fake.call_args.args[0], ["gh", "pr", "create"])
        self.assertEqual(fake.call_args.kwargs["input"], "body")
        self.assertTrue(fake.call_args.kwargs["check"])

    def test_keeps_white_space(self):
        with mock.patch.object(github_cli.subprocess, "run", return_value=completed(" M a\n")):
            self.assertEqual(github_cli.run("git", "status", strip=False), " M a\n")

    def test_run_json(self):
        with mock.patch.object(github_cli.subprocess, "run", return_value=completed('{"a": 1}')):
            self.assertEqual(github_cli.run_json("gh", "api", "x"), {"a": 1})
        with mock.patch.object(github_cli.subprocess, "run", return_value=completed("")):
            self.assertIsNone(github_cli.run_json("gh", "api", "x"))


class TestErrorLine(unittest.TestCase):
    """Error_line() picks the reason that gh printed last."""

    def test_last_line(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="warning\nHTTP 403: Forbidden\n\n")
        self.assertEqual(github_cli.error_line(error), "HTTP 403: Forbidden")

    def test_no_stderr(self):
        error = subprocess.CalledProcessError(2, ["gh"])
        self.assertIn("exit status 2", github_cli.error_line(error))


class TestRepositories(unittest.TestCase):
    """Repositories() lists the organization's repositories, sorted, with a default branch."""

    def test_list(self):
        answer = (
            '[{"nameWithOwner": "smarter-sh/b", "defaultBranchRef": {"name": "main"}},'
            ' {"nameWithOwner": "smarter-sh/a", "defaultBranchRef": null}]'
        )
        with mock.patch.object(github_cli, "run", return_value=answer) as fake:
            found = github_cli.repositories()
        self.assertEqual(
            found,
            [{"name": "smarter-sh/a", "default_branch": "main"}, {"name": "smarter-sh/b", "default_branch": "main"}],
        )
        self.assertIn("--no-archived", fake.call_args.args)


if __name__ == "__main__":
    unittest.main()

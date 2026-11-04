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


class TestHttpStatus(unittest.TestCase):
    """Http_status() keeps the status only."""

    def test_status(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403: Resource not accessible (smarter-sh/a)")
        self.assertEqual(github_cli.http_status(error), "403")

    def test_no_status(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="connection reset")
        self.assertEqual(github_cli.http_status(error), "unknown")


class TestApiObject(unittest.TestCase):
    """Api_object() returns an object, {} for no content, and refuses anything else."""

    def test_object(self):
        with mock.patch.object(github_cli, "run_json", return_value={"a": 1}) as fake:
            self.assertEqual(github_cli.api_object("repos/smarter-sh/a"), {"a": 1})
        self.assertEqual(fake.call_args.args, ("gh", "api", "repos/smarter-sh/a"))

    def test_no_content(self):
        with mock.patch.object(github_cli, "run_json", return_value=None):
            self.assertEqual(github_cli.api_object("x"), {})

    def test_not_an_object(self):
        with mock.patch.object(github_cli, "run_json", return_value=[]):
            with self.assertRaises(ValueError):
                github_cli.api_object("x")


class TestApiPages(unittest.TestCase):
    """Api_pages() reads pages until a short one, from a list or from a key of an object."""

    def test_follows_pages(self):
        full_page = [{"name": str(index)} for index in range(github_cli.PAGE_SIZE)]
        last_page = [{"name": "last"}]
        with mock.patch.object(github_cli, "run_json", side_effect=[full_page, last_page]) as fake:
            items = github_cli.api_pages("orgs/smarter-sh/repos?type=all")
        self.assertEqual(len(items), github_cli.PAGE_SIZE + 1)
        endpoints = [call.args[2] for call in fake.call_args_list]
        self.assertEqual(
            endpoints,
            [
                "orgs/smarter-sh/repos?type=all&per_page=100&page=1",
                "orgs/smarter-sh/repos?type=all&per_page=100&page=2",
            ],
        )

    def test_key(self):
        answer = {"total_count": 1, "secrets": [{"name": "PAT"}]}
        with mock.patch.object(github_cli, "run_json", return_value=answer) as fake:
            items = github_cli.api_pages("orgs/smarter-sh/actions/secrets", "secrets")
        self.assertEqual(items, [{"name": "PAT"}])
        self.assertEqual(fake.call_args.args[2], "orgs/smarter-sh/actions/secrets?per_page=100&page=1")

    def test_bad_page(self):
        for answer in ({"secrets": "x"}, ["x"], None):
            with self.subTest(answer=answer):
                with mock.patch.object(github_cli, "run_json", return_value=answer):
                    with self.assertRaises(ValueError):
                        github_cli.api_pages("x", "secrets")


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

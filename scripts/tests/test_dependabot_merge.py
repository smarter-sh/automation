"""
Tests of scripts/dependabot_merge.py, which the Dependabot Merge workflow runs to merge or close every open.

Dependabot pull request.

They never run gh: ``run``, ``run_json`` and ``repositories`` are replaced by fakes, and nothing sleeps.
"""

import io
import subprocess  # nosec B404: only to build a CalledProcessError
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import dependabot_merge as merge

RUN_URL = "https://github.com/smarter-sh/automation/actions/runs/1"


def pr(number, mergeable="MERGEABLE", state="CLEAN", draft=False):
    """A pull request, as gh pr list and gh pr view print it."""
    return {
        "number": number,
        "title": f"chore(deps): bump thing {number}",
        "url": f"https://github.com/smarter-sh/a/pull/{number}",
        "baseRefName": "alpha",
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": state,
    }


class FakeGitHub:
    """Stands in for gh: lists and views pull requests, reads settings, and records merges and closes."""

    def __init__(self, listed, viewed=None, settings=None, fail=()):
        self.listed = listed
        self.viewed = viewed or {}
        self.settings = settings if settings is not None else {"allow_squash_merge": True, "allow_auto_merge": True}
        self.fail = fail
        self.commands: list[tuple[str, ...]] = []

    def run_json(self, *args):
        self.commands.append(args)
        if args[:3] == ("gh", "pr", "list"):
            return self.listed
        if args[:3] == ("gh", "pr", "view"):
            return self.view(int(args[3]))
        if args[:2] == ("gh", "api"):
            return self.settings
        raise AssertionError(args)

    def view(self, number):
        """What gh pr view prints: the next of viewed's answers (the last one repeats), or else what gh pr list did."""
        answers = self.viewed.get(number)
        if isinstance(answers, list):
            if len(answers) > 1:
                return answers.pop(0)
            return answers[0]
        if answers:
            return answers
        for item in self.listed:
            if item["number"] == number:
                return item
        raise AssertionError(number)

    def run(self, *args, check=True, input_text=None, strip=True):
        self.commands.append(args)
        if args[:3] in self.fail:
            raise subprocess.CalledProcessError(1, list(args), stderr="GraphQL: Base branch was modified")
        return ""

    def ran(self, *prefix):
        """The commands that start with prefix."""
        return [command for command in self.commands if command[: len(prefix)] == prefix]

    def process(self, dry_run=False):
        """Run process_repository against this fake."""
        with (
            mock.patch.object(merge, "run_json", self.run_json),
            mock.patch.object(merge, "run", self.run),
            redirect_stdout(io.StringIO()),
        ):
            return merge.process_repository("smarter-sh/a", RUN_URL, dry_run, sleep=lambda _: None)


class TestDecide(unittest.TestCase):
    """Decide() maps GitHub's state to an action."""

    def test_table(self):
        cases = [
            (pr(1), False, True, "merged"),
            (pr(1, state="HAS_HOOKS"), False, True, "merged"),
            (pr(1, "CONFLICTING", "DIRTY"), True, True, "closed"),
            (pr(1, "CONFLICTING", "DIRTY"), False, True, "rebasing"),
            (pr(1, state="UNSTABLE"), False, True, "failing"),
            (pr(1, state="BLOCKED"), False, True, "auto_merge"),
            (pr(1, state="BEHIND"), False, False, "blocked"),
            (pr(1, draft=True), False, True, "draft"),
            (pr(1, "UNKNOWN", "UNKNOWN"), False, True, "unknown"),
            (pr(1, state="SOMETHING_NEW"), False, True, "unknown"),
        ]
        for state, conflicted, auto, expected in cases:
            self.assertEqual(merge.decide(state, conflicted, auto), expected, (state, conflicted, auto))


class TestMergeMethod(unittest.TestCase):
    """Merge_method() prefers squash, then merge, then rebase."""

    def test_methods(self):
        for settings, expected in (
            ({"allow_squash_merge": True, "allow_auto_merge": True}, ("--squash", True)),
            ({"allow_squash_merge": False, "allow_merge_commit": True}, ("--merge", False)),
            (
                {"allow_squash_merge": False, "allow_merge_commit": False, "allow_rebase_merge": True},
                ("--rebase", False),
            ),
            ({}, ("--squash", False)),
        ):
            with mock.patch.object(merge, "run_json", return_value=settings):
                self.assertEqual(merge.merge_method("smarter-sh/a"), expected)


class TestProcess(unittest.TestCase):
    """Process_repository() acts on each pull request, oldest first."""

    def test_actions(self):
        fake = FakeGitHub(
            [pr(3, state="UNSTABLE"), pr(1), pr(2, "CONFLICTING", "DIRTY"), pr(4, state="BLOCKED")],
        )
        results = fake.process()
        self.assertEqual(
            [(result.number, result.action) for result in results],
            [(1, "merged"), (2, "closed"), (3, "failing"), (4, "auto_merge")],
        )
        self.assertTrue(fake.ran("gh", "pr", "merge", "1", "--repo", "smarter-sh/a", "--squash", "--delete-branch"))
        close = fake.ran("gh", "pr", "close", "2")[0]
        self.assertIn("@dependabot reopen", close[-1])
        self.assertNotIn("--delete-branch", close)
        self.assertTrue(fake.ran("gh", "pr", "merge", "4", "--repo", "smarter-sh/a", "--squash", "--auto"))
        self.assertFalse(fake.ran("gh", "pr", "merge", "3"))
        for command in fake.commands:
            self.assertNotIn("--admin", command)

    def test_sibling_conflict_is_left(self):
        fake = FakeGitHub([pr(1), pr(2)], viewed={2: pr(2, "CONFLICTING", "DIRTY")})
        results = fake.process()
        self.assertEqual([result.action for result in results], ["merged", "rebasing"])
        self.assertFalse(fake.ran("gh", "pr", "close"))

    def test_waits_for_unknown(self):
        fake = FakeGitHub([pr(1, "UNKNOWN", "UNKNOWN")], viewed={1: [pr(1, "UNKNOWN", "UNKNOWN"), pr(1)]})
        self.assertEqual(fake.process()[0].action, "merged")
        fake = FakeGitHub([pr(1, "UNKNOWN", "UNKNOWN")], viewed={1: [pr(1, "UNKNOWN", "UNKNOWN")]})
        self.assertEqual(fake.process()[0].action, "unknown")
        self.assertEqual(len(fake.ran("gh", "pr", "view")), 4)

    def test_dry_run(self):
        fake = FakeGitHub([pr(1), pr(2, "CONFLICTING", "DIRTY")])
        results = fake.process(dry_run=True)
        self.assertEqual([result.action for result in results], ["merged", "closed"])
        self.assertFalse(fake.ran("gh", "pr", "merge"))
        self.assertFalse(fake.ran("gh", "pr", "close"))

    def test_merge_fails(self):
        fake = FakeGitHub([pr(1)], fail=(("gh", "pr", "merge"),))
        result = fake.process()[0]
        self.assertEqual(result.action, "merged")
        self.assertIn("Base branch was modified", result.error)
        self.assertTrue(merge.failed(result))
        self.assertIn("merged failed", merge.describe(result, False)[0])

    def test_no_pull_requests(self):
        fake = FakeGitHub([])
        self.assertEqual(fake.process(), [])
        self.assertFalse(fake.ran("gh", "api"))

    def test_process_all(self):
        repos = [{"name": "smarter-sh/a", "default_branch": "main"}, {"name": "smarter-sh/b", "default_branch": "main"}]
        merged = [merge.Result("smarter-sh/a", 1, "t", "u", "alpha", "merged")]
        error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404")
        with (
            mock.patch.object(merge, "repositories", return_value=repos),
            mock.patch.object(merge, "process_repository", side_effect=[merged, error]),
        ):
            results = merge.process_all("smarter-sh", RUN_URL, False)
        self.assertEqual([result.action for result in results], ["merged", "error"])
        self.assertEqual(results[1].error, "HTTP 404")


class TestReport(unittest.TestCase):
    """The summary and the Slack message, and the exit code."""

    def results(self):
        return [
            merge.Result("smarter-sh/a", 1, "bump x", "https://x/1", "alpha", "merged"),
            merge.Result("smarter-sh/a", 2, "bump y", "https://x/2", "alpha", "closed"),
            merge.Result("smarter-sh/b", 3, "bump z|w", "https://x/3", "main", "auto_merge"),
            merge.Result("smarter-sh/b", 4, "bump v", "https://x/4", "main", "failing"),
        ]

    def test_slack(self):
        message = merge.slack_message(self.results(), RUN_URL, False)
        self.assertIn("<https://x/2|a#2> bump y: closed: merge conflicts", message)
        self.assertIn("b#4", message)
        self.assertNotIn("a#1", message)
        self.assertIn("1 merged, 1 set to merge once their checks pass", message)
        self.assertIn("would be closed", merge.slack_message(self.results(), RUN_URL, True))
        self.assertIn("no open Dependabot pull requests", merge.slack_message([], RUN_URL, False))

    def test_summary(self):
        summary = merge.summary_markdown(self.results(), RUN_URL, True)
        self.assertIn("(dry run)", summary)
        self.assertIn("bump z\\|w", summary)
        self.assertIn("(no open Dependabot pull requests)", merge.summary_markdown([], RUN_URL, False))

    def test_main(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "results.json")
            summary_path = str(Path(directory) / "s.md")
            missing_path = str(Path(directory) / "missing.json")
            with mock.patch.object(merge, "process_all", return_value=self.results()):
                self.assertEqual(merge.main(["run", "--output", path]), 0)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(merge.main(["report", "--results", path, "--summary", summary_path]), 0)
            failed = self.results()
            failed.append(merge.Result("smarter-sh/c", 0, "", "u", "", "error", "HTTP 404"))
            merge.save(failed, path)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(merge.main(["report", "--results", path]), 1)
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(merge.main(["report", "--results", missing_path]), 1)
            error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401")
            with mock.patch.object(merge, "process_all", side_effect=error), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(merge.main(["run", "--output", path]), 1)


if __name__ == "__main__":
    unittest.main()

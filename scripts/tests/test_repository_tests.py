"""
Tests of scripts/repository_tests.py, which the Repository Tests workflow runs to start and watch every repository's.

test workflow.

They never run gh: ``run_json`` and ``repositories`` are replaced by fakes, and nothing sleeps.
"""

import io
import subprocess  # nosec B404: only to build a CalledProcessError
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import repository_tests as tests

RUN_URL = "https://github.com/smarter-sh/automation/actions/runs/1"

REPOSITORIES = [
    {"name": "smarter-sh/a", "default_branch": "main"},
    {"name": "smarter-sh/b", "default_branch": "main"},
    {"name": "smarter-sh/c", "default_branch": "main"},
    {"name": "smarter-sh/d", "default_branch": "main"},
]

WORKFLOWS = {
    "smarter-sh/a": [{"id": 1, "path": ".github/workflows/test.yml", "state": "active"}],
    "smarter-sh/b": [
        {"id": 2, "path": ".github/workflows/tests.yml", "state": "active"},
        {"id": 3, "path": ".github/workflows/build.yml", "state": "active"},
    ],
    "smarter-sh/c": [{"id": 4, "path": ".github/workflows/test.yml", "state": "disabled_manually"}],
    "smarter-sh/d": [{"id": 5, "path": ".github/workflows/build.yml", "state": "active"}],
}


class FakeApi:
    """Stands in for run_json(): answers the workflows, dispatch and run endpoints."""

    def __init__(self, dispatch_answer=None, runs=None, refuse=()):
        self.calls: list[tuple[str, ...]] = []
        self.dispatch_answer = dispatch_answer
        self.runs = runs or {}
        self.refuse = refuse
        self.polls = 0

    def __call__(self, *args: str):
        self.calls.append(args)
        path = next(arg for arg in args if arg.startswith("repos/"))
        repository = "/".join(path.split("/")[1:3])
        if path.endswith("/actions/workflows"):
            return {"workflows": WORKFLOWS[repository]}
        if path.endswith("/dispatches"):
            if repository in self.refuse:
                raise subprocess.CalledProcessError(1, list(args), stderr="HTTP 422: no workflow_dispatch trigger")
            if self.dispatch_answer is not None:
                return self.dispatch_answer(repository)
            return None
        if "/runs?" in path:
            return {"workflow_runs": [{"id": 77, "html_url": f"https://github.com/{repository}/actions/runs/77"}]}
        if "/actions/runs/" in path:
            self.polls += 1
            return self.runs.get(repository, {"status": "completed", "conclusion": "success"})
        raise AssertionError(path)


def details(repository):
    """A dispatch answer with the run's details."""
    return {"workflow_run_id": 10, "html_url": f"https://github.com/{repository}/actions/runs/10"}


class TestStart(unittest.TestCase):
    """Start_all() finds each test workflow and starts it, or says why not."""

    def start(self, api, dry_run=False, ref=""):
        with (
            mock.patch.object(tests, "repositories", return_value=REPOSITORIES),
            mock.patch.object(tests, "run_json", api),
            redirect_stdout(io.StringIO()),
        ):
            return tests.start_all("smarter-sh", ref, dry_run, sleep=lambda _: None)

    def test_dispatch_with_details(self):
        api = FakeApi(dispatch_answer=details)
        results = self.start(api, ref="alpha")
        self.assertEqual([result.repository for result in results], ["smarter-sh/a", "smarter-sh/b", "smarter-sh/c"])
        self.assertEqual(results[0].status, "queued")
        self.assertEqual(results[0].run_id, 10)
        self.assertEqual(results[1].workflow, ".github/workflows/tests.yml")
        self.assertEqual(results[0].ref, "alpha")
        self.assertEqual(results[2].status, "not_started")
        self.assertIn("disabled manually", results[2].reason)
        dispatches = [call for call in api.calls if call[-1] == "return_run_details=true"]
        self.assertEqual(len(dispatches), 2)
        self.assertIn("ref=alpha", dispatches[0])

    def test_dispatch_finds_run(self):
        results = self.start(FakeApi())
        self.assertEqual(results[0].run_id, 77)
        self.assertEqual(results[0].ref, "main")

    def test_dispatch_refused(self):
        results = self.start(FakeApi(refuse=("smarter-sh/a",), dispatch_answer=details))
        self.assertEqual(results[0].status, "not_started")
        self.assertIn("HTTP 422", results[0].reason)
        self.assertEqual(results[1].status, "queued")

    def test_dry_run(self):
        api = FakeApi()
        results = self.start(api, dry_run=True)
        self.assertEqual([result.status for result in results], ["would_start", "would_start", "not_started"])
        self.assertFalse([call for call in api.calls if "POST" in call])

    def test_run_never_appears(self):
        api = FakeApi()
        api_call = api.__call__

        def no_runs(*args):
            if any("/runs?" in arg for arg in args):
                return {"workflow_runs": []}
            return api_call(*args)

        result = tests.Result("smarter-sh/a", ".github/workflows/test.yml", "main")
        with mock.patch.object(tests, "run_json", no_runs):
            tests.dispatch(result, 1, sleep=lambda _: None)
        self.assertEqual(result.status, "not_started")
        self.assertIn("never appeared", result.reason)


class TestWait(unittest.TestCase):
    """Wait_all() polls until every run completes, or the deadline."""

    def test_completes(self):
        results = [
            tests.Result("smarter-sh/a", "t", "main", run_id=1, status="queued"),
            tests.Result("smarter-sh/b", "t", "main", run_id=2, status="queued"),
            tests.Result("smarter-sh/c", "t", "main", status="not_started"),
        ]
        api = FakeApi(runs={"smarter-sh/b": {"status": "completed", "conclusion": "failure"}})
        with mock.patch.object(tests, "run_json", api), redirect_stdout(io.StringIO()):
            tests.wait_all(results, 10, 1, sleep=lambda _: None)
        self.assertEqual([result.status for result in results], ["success", "failure", "not_started"])

    def test_deadline(self):
        results = [tests.Result("smarter-sh/a", "t", "main", run_id=1, status="queued")]
        api = FakeApi(runs={"smarter-sh/a": {"status": "in_progress"}})
        ticks = iter(range(0, 10_000, 600))
        with mock.patch.object(tests, "run_json", api), redirect_stdout(io.StringIO()):
            tests.wait_all(results, 30, 60, sleep=lambda _: None, clock=lambda: next(ticks))
        self.assertEqual(results[0].status, "waiting")
        self.assertTrue(results[0].failed)
        self.assertGreater(api.polls, 1)

    def test_poll_error_retries(self):
        results = [tests.Result("smarter-sh/a", "t", "main", run_id=1, status="queued")]
        answers = iter(
            [
                subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
                {"status": "completed", "conclusion": "success"},
            ]
        )

        def flaky(*args):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with (
            mock.patch.object(tests, "run_json", flaky),
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            tests.wait_all(results, 10, 1, sleep=lambda _: None)
        self.assertEqual(results[0].status, "success")


class TestReport(unittest.TestCase):
    """The summary and the Slack message, and the exit code."""

    def test_all_passed(self):
        results = [tests.Result("smarter-sh/a", "t", "main", url="u", status="success")]
        self.assertIn("all 1 test suites passed", tests.slack_message(results, RUN_URL))

    def test_failures(self):
        results = [
            tests.Result("smarter-sh/a", ".github/workflows/test.yml", "main", url="https://x/1", status="failure"),
            tests.Result("smarter-sh/b", "t", "main", status="not_started", reason="HTTP 422"),
            tests.Result("smarter-sh/c", "t", "main", status="success"),
        ]
        message = tests.slack_message(results, RUN_URL)
        self.assertIn("1 of 3 test suites passed", message)
        self.assertIn(":x: smarter-sh/a: failed (<https://x/1|run>)", message)
        self.assertIn("not started: HTTP 422", message)
        self.assertNotIn("smarter-sh/c", message)
        summary = tests.summary_markdown(results, RUN_URL)
        self.assertIn("1 of 3 passed", summary)
        self.assertIn("| smarter-sh/a | test.yml | main |", summary)

    def test_dry_run(self):
        results = [
            tests.Result("smarter-sh/a", "t", "main", status="would_start"),
            tests.Result("smarter-sh/b", "t", "main", status="not_started", reason="the workflow is disabled"),
        ]
        message = tests.slack_message(results, RUN_URL)
        self.assertIn("(dry run): 1 of 2 test suites would start", message)
        self.assertIn("disabled", message)

    def test_none(self):
        self.assertIn("No repository", tests.slack_message([], RUN_URL))
        self.assertIn("no repository has a test workflow", tests.summary_markdown([], RUN_URL))

    def test_main(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "results.json")
            with (
                mock.patch.object(
                    tests, "start_all", return_value=[tests.Result("smarter-sh/a", "t", "main", status="success")]
                ),
                mock.patch.object(tests, "wait_all"),
            ):
                self.assertEqual(tests.main(["run", "--output", path]), 0)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    tests.main(["report", "--results", path, "--summary", str(Path(directory) / "s.md")]), 0
                )
            tests.save([tests.Result("smarter-sh/a", "t", "main", status="failure")], path)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(tests.main(["report", "--results", path]), 1)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(tests.main(["report", "--results", str(Path(directory) / "missing.json")]), 1)
            error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401")
            with mock.patch.object(tests, "start_all", side_effect=error), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(tests.main(["run", "--output", path]), 1)
            self.assertEqual(tests.load(path), [])


if __name__ == "__main__":
    unittest.main()

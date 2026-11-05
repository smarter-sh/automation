"""Test scripts/error_watch/watch.py, the steps that the error watch workflow runs.

Slack and Loki are never called: ``post`` and the clock are replaced.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from error_watch import watch
from error_watch.incident import Incident, State, format_time, load_state, save_state

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


def results(count: int) -> dict:
    """A probe's results, with one finding logged ``count`` times."""
    last_seen = int((NOW - timedelta(minutes=1)).timestamp() * 1_000_000_000)
    finding = {
        "key": "traceback|KeyError|smarter/app.py|run",
        "kind": "traceback",
        "label": "KeyError in smarter/app.py, run()",
        "detail": "'…'",
        "count": count,
        "first_seen": last_seen,
        "last_seen": last_seen,
        "components": ["smarter"],
    }
    findings = [finding] if count else []
    return {"probed_at": format_time(NOW), "window_minutes": 30, "pods": 3, "lines": 900, "findings": findings}


class StepTest(unittest.TestCase):
    """Runs the steps in a temporary folder, with the clock fixed."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.state = self.path / "state.json"
        self.results = self.path / "results.json"
        self.output = self.path / "output.txt"
        self.summary = self.path / "summary.md"
        patcher = mock.patch.object(watch, "now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_step(self, argv: list[str], environ: dict | None = None) -> tuple[int, str]:
        """Run a step; return its exit code and output."""
        out = io.StringIO()
        with mock.patch.dict("os.environ", environ or {}, clear=True), redirect_stdout(out):
            code = watch.main(argv)
        return code, out.getvalue()

    def update(self, count: int, *extra: str, environ: dict | None = None) -> tuple[int, str]:
        """Run the update step on results with one finding."""
        self.results.write_text(json.dumps(results(count)), encoding="utf-8")
        argv = [
            "update",
            "--state",
            str(self.state),
            "--results",
            str(self.results),
            "--channel",
            "smarter",
            "--where",
            "ubc.smarter.sh",
            "--run-url",
            "https://run",
            "--summary",
            str(self.summary),
            *extra,
        ]
        return self.run_step(argv, environ)


class TestDue(StepTest):
    """The due step writes probe and window to $GITHUB_OUTPUT."""

    def due(self, *extra: str) -> str:
        """Run the due step; return its outputs."""
        argv = ["due", "--state", str(self.state), "--github-output", str(self.output), *extra]
        self.run_step(argv)
        return self.output.read_text(encoding="utf-8")

    def test_first_run_is_due(self):
        self.assertEqual(self.due(), "probe=true\nwindow=30\n")

    def test_not_due_soon_after_a_probe(self):
        save_state(State(last_probe=format_time(NOW - timedelta(minutes=10))), self.state)
        self.assertEqual(self.due(), "probe=false\nwindow=30\n")
        self.output.unlink()
        self.assertEqual(self.due("--force"), "probe=true\nwindow=30\n")

    def test_due_every_ten_minutes_during_a_problem(self):
        problem = Incident(opened=format_time(NOW), last_post=format_time(NOW))
        save_state(State(last_probe=format_time(NOW - timedelta(minutes=10)), incident=problem), self.state)
        self.assertEqual(self.due(), "probe=true\nwindow=30\n")


class TestUpdate(StepTest):
    """The update step posts, and saves the state only once Slack has the message."""

    def test_open_posts_and_saves_the_thread(self):
        with mock.patch.object(watch, "post", return_value={"channel": "C1", "ts": "1.2"}) as post:
            code, _ = self.update(7, environ={"SLACK_BOT_TOKEN": "xoxb-test"})
        self.assertEqual(code, 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["thread_ts"], "")
        state = load_state(self.state)
        self.assertEqual(state.incident.thread_ts, "1.2")
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn("KeyError in smarter/app.py, run()", summary)
        self.assertNotIn("'…'", summary)

    def test_resolve_replies_in_the_thread_and_broadcasts(self):
        problem = Incident(opened=format_time(NOW), last_post=format_time(NOW), channel="C1", thread_ts="1.2")
        problem.keys["traceback|KeyError|smarter/app.py|run"] = "KeyError"
        save_state(State(incident=problem), self.state)
        with mock.patch.object(watch, "post", return_value={"channel": "C1", "ts": "1.3"}) as post:
            code, _ = self.update(0, environ={"SLACK_BOT_TOKEN": "xoxb-test"})
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.args[0], "C1")
        self.assertEqual(post.call_args.kwargs, {"thread_ts": "1.2", "broadcast": True})
        self.assertIsNone(load_state(self.state).incident)

    def test_slack_failure_saves_nothing(self):
        with mock.patch.object(watch, "post", side_effect=RuntimeError("not_in_channel")):
            code, _ = self.update(7, environ={"SLACK_BOT_TOKEN": "xoxb-test"})
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())

    def test_dry_run_prints_and_saves_nothing(self):
        with mock.patch.object(watch, "post") as post:
            code, out = self.update(7, "--dry-run", environ={"SLACK_BOT_TOKEN": "xoxb-test"})
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("Repeated errors in ubc.smarter.sh", out)
        self.assertFalse(self.state.exists())

    def test_no_token_moves_on_without_posting(self):
        with mock.patch.object(watch, "post") as post:
            code, _ = self.update(7)
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIsNotNone(load_state(self.state).incident)


if __name__ == "__main__":
    unittest.main()

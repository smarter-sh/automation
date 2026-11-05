"""Test scripts/error_watch/incident.py, which follows a problem from its first Slack message to its last."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from error_watch.incident import Incident, State, apply, decide, format_time, load_state, message, save_state

NOW = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


def finding(key: str, count: int, minutes_ago: int = 1) -> dict:
    """A finding, as the probe's results hold it."""
    last_seen = NOW - timedelta(minutes=minutes_ago)
    last_seen_ns = int(last_seen.timestamp() * 1_000_000_000)
    return {
        "key": key,
        "kind": "traceback",
        "label": f"{key}Error in smarter/app.py, run()",
        "detail": "it <broke> `here`",
        "count": count,
        "first_seen": last_seen_ns,
        "last_seen": last_seen_ns,
        "components": ["smarter"],
    }


def open_state(keys: list[str], opened_minutes_ago: int = 40, posted_minutes_ago: int = 10) -> State:
    """A state with an open problem."""
    opened = NOW - timedelta(minutes=opened_minutes_ago)
    last_post = NOW - timedelta(minutes=posted_minutes_ago)
    labels = {}
    for key in keys:
        labels[key] = key
    problem = Incident(
        opened=format_time(opened), last_post=format_time(last_post), keys=labels, channel="C1", thread_ts="1.2"
    )
    return State(last_probe=format_time(NOW - timedelta(minutes=10)), incident=problem)


class TestDecide(unittest.TestCase):
    """Decide() chooses what to tell Slack."""

    def test_healthy_and_open(self):
        self.assertEqual(decide(State(), [finding("A", 4)], NOW, 5).action, "healthy")
        decision = decide(State(), [finding("A", 5), finding("B", 1)], NOW, 5)
        self.assertEqual(decision.action, "open")
        self.assertEqual([item["key"] for item in decision.repeating], ["A"])

    def test_new_error_during_a_problem(self):
        decision = decide(open_state(["A"]), [finding("A", 9), finding("B", 6)], NOW, 5)
        self.assertEqual(decision.action, "new")
        self.assertEqual([item["key"] for item in decision.new], ["B"])

    def test_ongoing_then_remind(self):
        self.assertEqual(decide(open_state(["A"]), [finding("A", 9)], NOW, 5).action, "ongoing")
        reminded_long_ago = open_state(["A"], posted_minutes_ago=30)
        self.assertEqual(decide(reminded_long_ago, [finding("A", 9)], NOW, 5).action, "remind")

    def test_lingering_error_keeps_the_problem_open(self):
        decision = decide(open_state(["A"]), [finding("A", 1, minutes_ago=5)], NOW, 5)
        self.assertEqual(decision.action, "ongoing")

    def test_resolved_after_a_quiet_period(self):
        quiet = [finding("A", 2, minutes_ago=25)]
        self.assertEqual(decide(open_state(["A"]), quiet, NOW, 5).action, "resolve")
        self.assertEqual(decide(open_state(["A"]), [], NOW, 5).action, "resolve")


class TestApply(unittest.TestCase):
    """Apply() records a decision in the state."""

    def test_open_remembers_the_thread(self):
        decision = decide(State(), [finding("A", 5)], NOW, 5)
        state = apply(State(), decision, NOW, {"channel": "C9", "ts": "9.9"})
        self.assertEqual(state.last_probe, format_time(NOW))
        self.assertEqual((state.incident.channel, state.incident.thread_ts), ("C9", "9.9"))
        self.assertIn("A", state.incident.keys)

    def test_remind_moves_last_post_and_ongoing_does_not(self):
        before = open_state(["A"], posted_minutes_ago=30)
        reminded = apply(before, decide(before, [finding("A", 9)], NOW, 5), NOW, None)
        self.assertEqual(reminded.incident.last_post, format_time(NOW))
        recent = open_state(["A"])
        ongoing = apply(recent, decide(recent, [finding("A", 9)], NOW, 5), NOW, None)
        self.assertEqual(ongoing.incident.last_post, recent.incident.last_post)

    def test_resolve_closes_it(self):
        before = open_state(["A"])
        state = apply(before, decide(before, [], NOW, 5), NOW, None)
        self.assertIsNone(state.incident)


class TestMessage(unittest.TestCase):
    """Message() writes each action's Slack message."""

    def test_open_message_escapes_the_detail(self):
        decision = decide(State(), [finding("A", 5)], NOW, 5)
        text = message(decision, State(), NOW, "ubc.smarter.sh", 30, "https://run")
        self.assertIn("Repeated errors in ubc.smarter.sh", text)
        self.assertIn("AError in smarter/app.py, run()", text)
        self.assertIn("`it &lt;broke&gt; 'here'`", text)

    def test_resolve_message_says_how_long(self):
        before = open_state(["A"], opened_minutes_ago=80)
        text = message(decide(before, [], NOW, 5), before, NOW, "ubc.smarter.sh", 30, "https://run")
        self.assertIn("Resolved", text)
        self.assertIn("1 h 20 min", text)

    def test_ongoing_and_healthy_post_nothing(self):
        before = open_state(["A"])
        ongoing = decide(before, [finding("A", 9)], NOW, 5)
        self.assertEqual(message(ongoing, before, NOW, "x", 30, "u"), "")
        healthy = decide(State(), [], NOW, 5)
        self.assertEqual(message(healthy, State(), NOW, "x", 30, "u"), "")


class TestStateFile(unittest.TestCase):
    """The state survives a save and a load, and a missing or broken file starts afresh."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "state.json"
            self.assertEqual(load_state(path), State())
            state = open_state(["A"])
            save_state(state, path)
            self.assertEqual(load_state(path), state)

    def test_broken_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(load_state(path), State())
            self.assertIn("::warning::", out.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Test scripts/pod_log/review_stream.py, which reads a Claude Code run's stream and its log folder."""

import tempfile
import unittest
from pathlib import Path

from pod_log.review_stream import (
    PodInventory,
    ReviewResult,
    content_blocks,
    events,
    final_result,
    tool_name,
    write_attempts,
)
from tests.pod_log_fixtures import (
    result_event,
    write_call,
    write_log_dir,
    write_result,
    write_stream,
)


class TestStream(unittest.TestCase):
    """Test the readers of the stream."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_a_damaged_line_is_skipped(self):
        """A line cut off mid-write, and a JSON value that isn't an object, are skipped."""
        lines = [{"type": "system"}, '{"type": "assist', "[1, 2]", result_event()]
        path = write_stream(self.root / "s.jsonl", lines)
        types = [event["type"] for event in events(path)]
        self.assertEqual(types, ["system", "result"])

    def test_a_missing_stream_yields_nothing(self):
        missing = str(self.root / "missing.jsonl")
        self.assertEqual(list(events(missing)), [])
        self.assertIsNone(final_result(missing))

    def test_final_result_is_the_last_result(self):
        path = write_stream(self.root / "s.jsonl", [result_event("first"), result_event("last")])
        result = final_result(path)
        assert result is not None
        self.assertEqual(result.answer, "last")

    def test_a_run_without_a_result_did_not_finish(self):
        path = write_stream(self.root / "s.jsonl", [{"type": "system"}, write_call()])
        self.assertIsNone(final_result(path))

    def test_write_attempts_counts_every_call_of_a_write_tool(self):
        """A call counts even if it failed or its result never came, but a read tool doesn't."""
        path = write_stream(
            self.root / "s.jsonl",
            [
                write_call("create_issue", "t1"),
                write_result("boom", "t1", is_error=True),
                write_call("close_issue", "t2"),
                write_call("search_issues", "t3"),
            ],
        )
        self.assertEqual(write_attempts(path), 2)

    def test_content_blocks(self):
        self.assertEqual(content_blocks({"type": "result"}), [])
        self.assertEqual(content_blocks({"message": {"content": "text"}}), [])
        mixed = {"message": {"content": [{"type": "text"}, "x"]}}
        self.assertEqual(content_blocks(mixed), [{"type": "text"}])

    def test_tool_name(self):
        self.assertEqual(tool_name("mcp__smarter-project__create_issue"), "create_issue")
        self.assertEqual(tool_name("Read"), "Read")


class TestReviewResult(unittest.TestCase):
    """Test ReviewResult."""

    def test_from_event_keeps_fields_of_the_expected_type(self):
        event = result_event("done", api_error_status=529, num_turns=9, total_cost_usd=1, subtype=True)
        result = ReviewResult.from_event(event)
        self.assertEqual(result.answer, "done")
        self.assertEqual(result.api_error_status, 529)
        self.assertEqual(result.num_turns, 9)
        self.assertEqual(result.total_cost_usd, 1.0)
        self.assertIsNone(result.subtype)
        wrong_types = ReviewResult.from_event({"type": "result", "result": 5, "num_turns": "9"})
        self.assertIsNone(wrong_types.answer)

    def test_a_cost_of_zero_is_kept(self):
        """A whole number of dollars, which JSON writes as an int, is still a cost, even 0."""
        for cost in (0, 0.0):
            with self.subTest(cost=cost):
                result = ReviewResult.from_event(result_event(total_cost_usd=cost))
                self.assertEqual(result.total_cost_usd, cost)

    def test_describe(self):
        succeeded = ReviewResult(is_error=False, num_turns=9, total_cost_usd=1.2)
        self.assertEqual(succeeded.describe(), "turns: 9, cost: $1.20")
        failed = ReviewResult(is_error=True, subtype="error_max_turns")
        self.assertEqual(
            failed.describe(),
            "turns: ?, cost: $?, failed: subtype error_max_turns, terminal_reason ?, api_error_status none",
        )

    def test_healthy(self):
        """Only an exact verdict on the last line, of a run without an error, is healthy."""
        cases = {
            "All good.\nVerdict: healthy": True,
            "All good.\n\n**Verdict: healthy**\n\n": True,
            "`verdict: HEALTHY`": True,
            "Verdict: healthy, but look at the worker": False,
            "Verdict: needs attention": False,
            "Verdict: healthy\nmore text": False,
            "": False,
        }
        for answer, healthy in cases.items():
            with self.subTest(answer=answer):
                result = ReviewResult(is_error=False, answer=answer)
                self.assertEqual(result.healthy, healthy)
        failed = ReviewResult(is_error=True, answer="Verdict: healthy")
        self.assertFalse(failed.healthy)

    def test_transient_api_error(self):
        cases = {
            429: True,
            500: True,
            529: True,
            599: True,
            400: False,
            600: False,
            None: False,
        }
        for status, transient in cases.items():
            with self.subTest(status=status):
                result = ReviewResult(is_error=True, api_error_status=status)
                self.assertEqual(result.transient_api_error, transient)


class TestPodInventory(unittest.TestCase):
    """Test PodInventory.read()."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_read(self):
        """Pods are counted from their logs, restarts only for them, and releases only with replicas."""
        inventory = PodInventory.read(write_log_dir(self.root, worker_restarts=2))
        self.assertEqual(inventory.pods, {"smarter": 2, "smarter-beat": 1, "smarter-worker": 1})
        self.assertEqual(inventory.restarts, 2)
        self.assertEqual(inventory.releases, ["v0.40.4"])

    def test_an_unsafe_tag_is_left_out(self):
        logs = Path(write_log_dir(self.root))
        images = logs / "inventory" / "images.txt"
        images.write_text("2026-10-30T00:00:00Z 1 registry.example/smarter:<b>x</b>\n")
        self.assertEqual(PodInventory.read(str(logs)).releases, [])

    def test_missing_files_leave_facts_empty(self):
        logs = Path(write_log_dir(self.root))
        (logs / "inventory" / "pods.txt").unlink()
        (logs / "inventory" / "images.txt").unlink()
        inventory = PodInventory.read(str(logs))
        self.assertIsNone(inventory.restarts)
        self.assertEqual(inventory.releases, [])
        self.assertEqual(PodInventory.read(""), PodInventory())
        missing = str(self.root / "missing")
        self.assertEqual(PodInventory.read(missing), PodInventory())


if __name__ == "__main__":
    unittest.main()

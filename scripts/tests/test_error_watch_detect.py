"""Test scripts/error_watch/detect.py, which finds tracebacks and ERROR lines in pod logs, and groups them."""

import unittest

from error_watch.detect import choose_frame, error_message, group, normalize, parse, short_path

DJANGO_ERROR = "[2026-11-05 14:00:00 +0000] - ERROR - MainProcess - Internal Server Error: /api/v1/chat/"

TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/usr/local/lib/python3.12/site-packages/django/core/handlers/base.py", line 197, in _get_response',
    "    response = wrapped_callback(request, *callback_args, **callback_kwargs)",
    '  File "/smarter/smarter/apps/chat/views.py", line 42, in post',
    "    reply = self.provider.send(messages)",
    '  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 900, in send',
    "    raise TimeoutError(message)",
    "TimeoutError: the provider did not answer in 30 seconds",
]


def lines(texts: list[str], start: int = 1_000) -> list[tuple[int, str]]:
    """Give each text a time, one nanosecond apart."""
    timed = []
    for offset, text in enumerate(texts):
        timed.append((start + offset, text))
    return timed


class TestParse(unittest.TestCase):
    """Parse() puts tracebacks back together, and collects ERROR lines."""

    def test_traceback_with_its_error_line_is_one_event(self):
        events = parse("smarter", lines([DJANGO_ERROR, *TRACEBACK]))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.kind, "traceback")
        self.assertEqual(event.label, "TimeoutError in smarter/apps/chat/views.py, post()")
        self.assertEqual(event.detail, "the provider did not answer in N seconds")

    def test_chained_exceptions_count_once_as_the_last(self):
        chained = [
            *TRACEBACK,
            "",
            "During handling of the above exception, another exception occurred:",
            "",
            "Traceback (most recent call last):",
            '  File "/smarter/smarter/apps/chat/tasks.py", line 7, in retry',
            "    raise RuntimeError('gave up')",
            "RuntimeError: gave up",
        ]
        events = parse("smarter-worker", lines(chained))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].label, "RuntimeError in smarter/apps/chat/tasks.py, retry()")

    def test_error_line_alone_is_an_event(self):
        events = parse("smarter", lines([DJANGO_ERROR, "[2026-11-05 14:00:01 +0000] - INFO - MainProcess - ok"]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "error-line")
        self.assertTrue(events[0].label.startswith("ERROR line "))
        self.assertEqual(events[0].detail, "Internal Server Error: /api/vN/chat/")

    def test_celery_error_line(self):
        text = "[2026-11-05 14:00:00,123: ERROR/ForkPoolWorker-2] Task sync[ab12] raised unexpected: KeyError('x')"
        self.assertEqual(error_message(text), "Task sync[ab12] raised unexpected: KeyError('x')")

    def test_info_lines_and_ignored_errors_are_not_events(self):
        texts = [
            "[2026-11-05 14:00:00 +0000] - INFO - MainProcess - the word ERROR, later in a message",
            "[2026-11-05 14:00:00 +0000] - ERROR - MainProcess - Invalid HTTP_HOST header: 'x'. Add it.",
        ]
        self.assertEqual(parse("smarter", lines(texts)), [])

    def test_unfinished_traceback_is_not_an_event(self):
        self.assertEqual(parse("smarter", lines(TRACEBACK[:3])), [])

    def test_detail_is_redacted(self):
        text = "[2026-11-05 14:00:00 +0000] - ERROR - MainProcess - sign-in failed for alice@example.com"
        events = parse("smarter", lines([text]))
        self.assertNotIn("alice@example.com", events[0].detail)
        self.assertNotIn("alice@example.com", events[0].label)


class TestHelpers(unittest.TestCase):
    """The helpers that key and label an error."""

    def test_normalize_takes_values_out(self):
        self.assertEqual(normalize("Task 12 failed for 'acme' at https://x.y/z"), "Task N failed for '…' at <url>")

    def test_choose_frame_prefers_the_platform_code(self):
        frames = [("/smarter/a.py", "outer"), ("/usr/lib/python3/dist-packages/b.py", "inner")]
        self.assertEqual(choose_frame(frames), ("/smarter/a.py", "outer"))

    def test_choose_frame_falls_back_to_the_innermost(self):
        frames = [("/x/site-packages/a.py", "outer"), ("/x/site-packages/b.py", "inner")]
        self.assertEqual(choose_frame(frames), ("/x/site-packages/b.py", "inner"))
        self.assertEqual(choose_frame([]), ("unknown", "unknown"))

    def test_short_path(self):
        self.assertEqual(short_path("/usr/lib/python3.12/site-packages/django/db/utils.py"), "django/db/utils.py")
        self.assertEqual(short_path("/smarter/smarter/apps/chat/views.py"), "smarter/apps/chat/views.py")


class TestGroup(unittest.TestCase):
    """Group() counts the events by key, across pods."""

    def test_counts_across_pods(self):
        events = parse("smarter", lines([DJANGO_ERROR, *TRACEBACK], start=100))
        events += parse("smarter", lines([DJANGO_ERROR, *TRACEBACK], start=500))
        events += parse("smarter-worker", lines(["[x] - CRITICAL - MainProcess - broker lost"], start=50))
        findings = group(events)
        self.assertEqual([finding.count for finding in findings], [2, 1])
        top = findings[0]
        # each event's time is its "Traceback" line's, just after the ERROR line.
        self.assertEqual(top.first_seen, 101)
        self.assertEqual(top.last_seen, 501)
        self.assertEqual(top.components, ["smarter"])


if __name__ == "__main__":
    unittest.main()

"""Test scripts/pod_log/loki.py, which replaces the downloaded pod logs with the environment's logs from Loki.

Loki is a fake: a small http server, in a thread, on a local port, that answers ``/ready``,
``/loki/api/v1/series`` and ``/loki/api/v1/query_range`` from lines held in memory, as Loki does. The
port-forward is never started: :func:`pod_log_loki.open_port_forward` is replaced with a fake process.
"""

import http.server
import io
import json
import pathlib
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from pod_log import loki as pod_log_loki
from pod_log.loki import (
    RETENTION,
    SOURCE_NAME,
    Loki,
    LokiError,
    Stream,
    logql_string,
    main,
    parse_since,
    replace_logs,
    timestamp,
    write_logs,
)

NAMESPACE = "smarter-test"
"""The namespace of the fake Loki's lines."""


def now_ns() -> int:
    """The current time, in Unix nanoseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


class FakeLoki:
    """
    Lines held in memory, by stream, which :class:`LokiHandler` serves as Loki's api does.

    Each stream is a pod's container: its labels, and its lines as ``(nanoseconds, text)``.
    """

    def __init__(self):
        self.streams: list[tuple[dict[str, str], list[tuple[int, str]]]] = []
        self.ready = True
        self.fail_query = False
        self.queries: list[dict[str, str]] = []

    def add(self, component: str, pod: str, container: str, lines: list[tuple[int, str]]):
        """Add a stream."""
        labels = {"namespace": NAMESPACE, "component": component, "pod": pod, "container": container}
        self.streams.append((labels, lines))


class LokiHandler(http.server.BaseHTTPRequestHandler):
    """The fake Loki's http handler.

    Its server's ``fake`` holds the lines.
    """

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        """Print nothing: the tests' output stays clean."""

    def answer(self, status: int, body: object):
        """Send a JSON answer."""
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # pylint: disable=invalid-name
        """Answer one of Loki's api paths."""
        fake: FakeLoki = self.server.fake  # type: ignore[attr-defined]
        url = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/ready":
            self.send_response(200 if fake.ready else 503)
            self.end_headers()
            return
        if url.path == "/loki/api/v1/series":
            labels = [stream_labels for stream_labels, _ in fake.streams]
            self.answer(200, {"status": "success", "data": labels})
            return
        if url.path == "/loki/api/v1/query_range":
            fake.queries.append(params)
            if fake.fail_query:
                self.answer(500, {"status": "error"})
                return
            after_pod = params["query"].split('pod="')[1]
            pod = after_pod.split('"')[0]
            start = int(params["start"])
            end = int(params["end"])
            limit = int(params["limit"])
            matching = []
            for stream_labels, lines in fake.streams:
                if stream_labels["pod"] != pod:
                    continue
                for line_time, text in lines:
                    if start <= line_time <= end:
                        matching.append((line_time, text, stream_labels))
            matching.sort(key=lambda line: line[0])
            page = matching[:limit]
            result = []
            for line_time, text, stream_labels in page:
                result.append({"stream": stream_labels, "values": [[str(line_time), text]]})
            self.answer(200, {"status": "success", "data": {"resultType": "streams", "result": result}})
            return
        self.answer(404, {"status": "error"})


class FakeLokiTestCase(unittest.TestCase):
    """Start the fake Loki on a local port, for each test."""

    def setUp(self):
        self.fake = FakeLoki()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LokiHandler)
        self.server.fake = self.fake  # type: ignore[attr-defined]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.loki = Loki(f"http://127.0.0.1:{port}")
        self.base_url = f"http://127.0.0.1:{port}"


class TestHelpers(unittest.TestCase):
    """Test parse_since(), timestamp() and logql_string()."""

    def test_parse_since(self):
        self.assertEqual(parse_since("25h"), timedelta(hours=25))
        self.assertEqual(parse_since("90m"), timedelta(minutes=90))
        self.assertEqual(parse_since(" 2d "), timedelta(days=2))
        self.assertEqual(parse_since(""), RETENTION, "empty: all that Loki keeps")
        self.assertEqual(parse_since("30d"), RETENTION, "never more than Loki keeps")
        for wrong in ("25", "h", "25x", "-1h"):
            with self.subTest(since=wrong):
                with self.assertRaises(ValueError):
                    parse_since(wrong)

    def test_timestamp(self):
        when = datetime(2026, 11, 2, 14, 0, 0, tzinfo=timezone.utc)
        seconds = int(when.timestamp())
        value = seconds * 1_000_000_000 + 123456789
        self.assertEqual(timestamp(value), "2026-11-02T14:00:00.123456789Z", "as kubectl logs --timestamps writes it")

    def test_logql_string(self):
        self.assertEqual(logql_string('a"b\\c'), '"a\\"b\\\\c"')


class TestLoki(FakeLokiTestCase):
    """Test the Loki client against the fake Loki."""

    def test_ready(self):
        self.assertTrue(self.loki.ready())
        self.fake.ready = False
        self.assertFalse(self.loki.ready())
        self.assertFalse(Loki("http://127.0.0.1:1").ready(), "nothing listens on port 1")

    def test_streams(self):
        self.fake.add("smarter-worker", "worker-b", "worker", [])
        self.fake.add("smarter", "app-a", "smarter", [])
        self.fake.add("smarter", "app-a", "sidecar", [])
        streams = self.loki.streams(NAMESPACE, 0, now_ns())
        names = [(stream.component, stream.pod) for stream in streams]
        self.assertEqual(names, [("smarter", "app-a"), ("smarter-worker", "worker-b")])

    def test_read_merges_containers_in_time_order(self):
        base = now_ns() - 10_000
        self.fake.add("smarter", "app-a", "smarter", [(base + 2, "second"), (base + 4, "fourth")])
        self.fake.add("smarter", "app-a", "sidecar", [(base + 1, "first"), (base + 3, "third")])
        stream = Stream(component="smarter", pod="app-a")
        self.loki.read(stream, NAMESPACE, base, now_ns())
        texts = [text for _, text in stream.lines]
        self.assertEqual(texts, ["first", "second", "third", "fourth"])
        self.assertFalse(stream.truncated)

    def test_read_pages(self):
        """Pages continue from the last line's time, without losing or repeating lines that share it."""
        base = now_ns() - 10_000
        lines = [(base + 1, "a"), (base + 2, "b"), (base + 3, "c1"), (base + 3, "c2"), (base + 4, "d"), (base + 5, "e")]
        self.fake.add("smarter", "app-a", "smarter", lines)
        stream = Stream(component="smarter", pod="app-a")
        with mock.patch.object(pod_log_loki, "PAGE_SIZE", 3):
            self.loki.read(stream, NAMESPACE, base, now_ns())
        texts = [text for _, text in stream.lines]
        self.assertEqual(texts, ["a", "b", "c1", "c2", "d", "e"])
        self.assertGreater(len(self.fake.queries), 1)
        self.assertEqual(self.fake.queries[0]["direction"], "forward")

    def test_read_stops_at_the_line_limit(self):
        base = now_ns() - 10_000
        lines = [(base + i, f"line {i}") for i in range(1, 11)]
        self.fake.add("smarter", "app-a", "smarter", lines)
        stream = Stream(component="smarter", pod="app-a")
        with mock.patch.object(pod_log_loki, "PAGE_SIZE", 4), mock.patch.object(pod_log_loki, "MAX_LINES_PER_POD", 6):
            self.loki.read(stream, NAMESPACE, base, now_ns())
        self.assertEqual(len(stream.lines), 6)
        self.assertTrue(stream.truncated)

    def test_errors(self):
        self.fake.fail_query = True
        with self.assertRaisesRegex(LokiError, "HTTP 500"):
            self.loki.read(Stream(component="smarter", pod="app-a"), NAMESPACE, 0, now_ns())
        with self.assertRaisesRegex(LokiError, "could not be reached"):
            Loki("http://127.0.0.1:1").get("/loki/api/v1/series", {})
        with self.assertRaisesRegex(LokiError, "HTTP 404"):
            self.loki.get("/no/such/path", {})


class TestFiles(unittest.TestCase):
    """Test write_logs() and replace_logs()."""

    def test_write_and_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            log_dir = root / "logs"
            (log_dir / "inventory").mkdir(parents=True)
            (log_dir / "inventory" / "pods.txt").write_text("kept", encoding="utf-8")
            (log_dir / "smarter").mkdir()
            (log_dir / "smarter" / "app-a.log").write_text("from kubectl", encoding="utf-8")
            (log_dir / "smarter" / "app-a.previous.log").write_text("from kubectl", encoding="utf-8")
            (log_dir / "redactions.json").write_text("{}", encoding="utf-8")
            loki_dir = root / "logs.loki"
            stream = Stream(component="smarter", pod="app-a", lines=[(1_793_628_000_000_000_000, "from loki")])
            gone = Stream(component="mariadb", pod="smarter-mariadb-0", lines=[(1_793_628_000_000_000_001, "db")])
            self.assertEqual(write_logs([stream, gone], loki_dir), 2)
            replace_logs(log_dir, loki_dir)
            app_log = (log_dir / "smarter" / "app-a.log").read_text(encoding="utf-8")
            self.assertEqual(app_log, "2026-11-02T14:00:00.000000000Z from loki\n")
            self.assertFalse((log_dir / "smarter" / "app-a.previous.log").exists(), "kubectl's logs are gone")
            self.assertTrue((log_dir / "mariadb" / "smarter-mariadb-0.log").exists())
            self.assertEqual((log_dir / "inventory" / "pods.txt").read_text(encoding="utf-8"), "kept")
            self.assertTrue((log_dir / "redactions.json").exists(), "files at the top are kept")


class TestRun(FakeLokiTestCase):
    """Test run() and main(), with the fake Loki in place of the port-forward."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_dir = pathlib.Path(tmp.name) / "logs"
        (self.log_dir / "inventory").mkdir(parents=True)
        (self.log_dir / "smarter").mkdir()
        (self.log_dir / "smarter" / "app-a.log").write_text("from kubectl\n", encoding="utf-8")
        self.process = mock.Mock(spec=subprocess.Popen)
        self.process.poll.return_value = None
        port = self.server.server_address[1]
        patches = [
            mock.patch.object(pod_log_loki, "free_port", return_value=port),
            mock.patch.object(pod_log_loki, "open_port_forward", return_value=self.process),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def source(self) -> str:
        """The log source that run() wrote."""
        return (self.log_dir / "inventory" / SOURCE_NAME).read_text(encoding="utf-8")

    def test_replaces_the_logs(self):
        recent = now_ns() - 60 * 1_000_000_000
        self.fake.add("smarter", "app-a", "smarter", [(recent, "from loki")])
        self.fake.add("mariadb", "smarter-mariadb-0", "mariadb", [(recent, "db ready")])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(pod_log_loki.run("ctx", NAMESPACE, self.log_dir, "25h"))
        app_log = (self.log_dir / "smarter" / "app-a.log").read_text(encoding="utf-8")
        self.assertIn("from loki", app_log)
        self.assertTrue((self.log_dir / "mariadb" / "smarter-mariadb-0.log").exists())
        source = self.source()
        self.assertTrue(source.startswith("loki:"))
        self.assertIn("pods: 2", source)
        self.assertIn("lines: 2", source)
        self.assertNotIn("from loki", output.getvalue(), "no log line is printed")
        self.assertFalse((self.log_dir.parent / "logs.loki").exists())
        self.process.terminate.assert_called_once()

    def test_truncation_is_noted(self):
        recent = now_ns() - 60 * 1_000_000_000
        lines = [(recent + i, f"line {i}") for i in range(5)]
        self.fake.add("smarter", "app-a", "smarter", lines)
        with mock.patch.object(pod_log_loki, "MAX_LINES_PER_POD", 3), redirect_stdout(io.StringIO()):
            pod_log_loki.run("ctx", NAMESPACE, self.log_dir, "25h")
        self.assertIn("truncated: smarter/app-a.log, at 3 lines", self.source())

    def test_keeps_kubectl_logs_when_loki_fails(self):
        cases = {
            "no streams": lambda: None,
            "query fails": lambda: setattr(self.fake, "fail_query", True),
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                self.fake.streams = []
                self.fake.fail_query = False
                if name == "query fails":
                    self.fake.add("smarter", "app-a", "smarter", [(now_ns() - 1000, "x")])
                prepare()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertFalse(pod_log_loki.run("ctx", NAMESPACE, self.log_dir, "25h"))
                self.assertIn("::warning::", output.getvalue())
                app_log = (self.log_dir / "smarter" / "app-a.log").read_text(encoding="utf-8")
                self.assertEqual(app_log, "from kubectl\n")
                self.assertTrue(self.source().startswith("kubectl:"))
                self.assertFalse((self.log_dir.parent / "logs.loki").exists())

    def test_port_forward_stops(self):
        """A namespace without Loki: kubectl port-forward exits, and the kubectl logs stay."""
        self.process.poll.return_value = 1
        with redirect_stdout(io.StringIO()):
            self.assertFalse(pod_log_loki.run("ctx", NAMESPACE, self.log_dir, "25h"))
        self.assertIn("port-forward stopped", self.source())
        self.process.terminate.assert_not_called()

    def test_main(self):
        self.fake.add("smarter", "app-a", "smarter", [(now_ns() - 1000, "from loki")])
        argv = ["pod_log/loki.py", "--context", "ctx", "--namespace", NAMESPACE, "--log-dir", str(self.log_dir)]
        with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertTrue(self.source().startswith("loki:"))
        with mock.patch("sys.argv", argv + ["--since", "soon"]), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 2)
        empty = self.log_dir.parent / "empty"
        empty.mkdir()
        argv[-1] = str(empty)
        with mock.patch("sys.argv", argv), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 2)


class TestPortForward(unittest.TestCase):
    """Test open_port_forward() and close_port_forward(), with subprocess faked."""

    def test_open(self):
        with mock.patch.object(pod_log_loki.subprocess, "Popen") as popen:
            pod_log_loki.open_port_forward("ctx", NAMESPACE, 41234)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["kubectl", "--context"])
        self.assertIn("service/smarter-loki", command)
        self.assertIn("41234:3100", command)
        self.assertEqual(command[-2:], ["--address", "127.0.0.1"])

    def test_close(self):
        running = mock.Mock(spec=subprocess.Popen)
        running.poll.return_value = None
        running.wait.side_effect = subprocess.TimeoutExpired("kubectl", 10)
        pod_log_loki.close_port_forward(running)
        running.terminate.assert_called_once()
        running.kill.assert_called_once()

    def test_free_port(self):
        self.assertGreater(pod_log_loki.free_port(), 0)


class TestWaitUntilReady(FakeLokiTestCase):
    """Test wait_until_ready()."""

    def test_not_ready_in_time(self):
        self.fake.ready = False
        process = mock.Mock(spec=subprocess.Popen)
        process.poll.return_value = None
        with mock.patch.object(pod_log_loki, "READY_TIMEOUT", 0.01):
            with self.assertRaisesRegex(LokiError, "not ready"):
                pod_log_loki.wait_until_ready(self.loki, process)


if __name__ == "__main__":
    unittest.main()

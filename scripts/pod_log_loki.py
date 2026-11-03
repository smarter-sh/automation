#!/usr/bin/env python3
"""Replace the downloaded pod logs with the environment's logs from Loki, which keeps every pod's whole history.

Usage::

    python3 -E -s scripts/pod_log_loki.py --context <kube context> --namespace <namespace> \\
        --log-dir <folder> [--since 25h]

The pod log review (``.github/workflows/podLogReview.yml``) first runs smarter's
``download-logs.sh``, which writes the environment's inventory (pods, events, images) and each running
pod's log, with ``kubectl logs``, into the log folder. ``kubectl logs`` has gaps: a pod that was replaced,
e.g. when its spot node was taken back, is gone with its log, a container that restarted more than once
lost the older part of its log, and MariaDB is not reviewed. The platform's Helm chart now keeps the last
72 hours of every pod's log in Loki, which Grafana Alloy fills (see smarter's
``helm/charts/smarter/values.yaml``). This script reads the review's period from Loki, and puts it in place
of the ``kubectl`` logs:

1. It opens a ``kubectl port-forward`` to the namespace's ``smarter-loki`` Service, on a free local port.
   Loki has no address outside the cluster, and the namespace's network policies admit only its own pods,
   but a port-forward goes through the Kubernetes api and the pod's kubelet, so it needs only the cluster
   credentials that the workflow already has, and no new host in the runner's allow list.
2. It lists the log streams of the period (each pod's ``component``, ``pod`` and ``container``), and reads
   each pod's lines, oldest first, a page of :data:`PAGE_SIZE` lines at a time, into a new folder beside
   the log folder.
3. Only if every pod was read, it replaces each deployment folder of the log folder with Loki's, and
   writes :data:`SOURCE_NAME` in ``inventory/``, which says where the logs came from. If anything fails,
   e.g. Loki is not deployed or is down, the ``kubectl`` logs stay, :data:`SOURCE_NAME` says why, and the
   run goes on: a review of fewer logs is better than none.

The files keep the layout of ``download-logs.sh``: ``<component>/<pod>.log``, each line starting with its
time, as ``kubectl logs --timestamps`` writes it. There is no ``.previous.log``: a restarted container's
lines are in the same file, in order. MariaDB's pods are in ``mariadb/``, and a pod that no longer runs has
a log too, though ``inventory/pods.txt`` does not list it.

Alloy redacts email addresses, IP addresses, tokens, keys and passwords before Loki stores a line, and
writes ``<email>``, ``<ip>`` or ``<redacted>`` in their place. ``pod_log_redact.py`` turns the first and
the last into its own placeholders, so that they are still reported as findings.

Like every script that the workflow runs, it prints only counts and file names, never a log line: the
workflow's log is public. It uses only Python's standard library:

- ``argparse`` reads the command line.
- ``subprocess`` runs ``kubectl port-forward`` in the background, and stops it at the end.
- ``socket`` finds a free local port for the port-forward.
- ``urllib.request`` sends the HTTP requests to Loki's api, and ``urllib.parse`` builds their query strings.
- ``json`` reads Loki's answers.
- ``datetime`` and ``time`` convert between times and Loki's nanosecond timestamps, and wait for the
  port-forward to be ready.
- ``pathlib`` and ``shutil`` write the logs, and move the folders into place.
"""

import argparse
import json
import pathlib
import re
import shutil
import socket
import subprocess  # nosec B404: runs kubectl, with a fixed argument list
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DESCRIPTION = "Replace the downloaded pod logs with the environment's logs from Loki."
"""The command line's description."""

LOKI_SERVICE = "service/smarter-loki"
"""The Kubernetes Service of the environment's Loki, which the platform's Helm chart creates."""

LOKI_PORT = 3100
"""The port of Loki's http api."""

RETENTION = timedelta(hours=72)
"""How long Loki keeps a line: the Helm chart's ``limits_config.retention_period``.

The longest period that can be read.
"""

PAGE_SIZE = 5000
"""The most lines that one query returns: Loki's default ``max_entries_limit_per_query``."""

MAX_LINES_PER_POD = 200_000
"""The most lines that are read for one pod, so that one runaway pod cannot fill the runner's disk or its hour."""

REQUEST_TIMEOUT = 60
"""Seconds to wait for one answer from Loki."""

READY_TIMEOUT = 30
"""Seconds to wait for the port-forward, and Loki, to answer."""

SOURCE_NAME = "log-source.txt"
"""The file in ``inventory/`` that says where the logs came from: Loki, or ``kubectl logs`` and why."""

SINCE = re.compile(r"^(?P<amount>\d+)(?P<unit>[mhd])$")
"""A period, as the workflow's ``since`` input gives it, e.g. ``25h``, ``90m`` or ``3d``."""

UNITS = {"m": "minutes", "h": "hours", "d": "days"}
"""The units of :data:`SINCE`, as ``timedelta`` names them."""


class LokiError(Exception):
    """Loki could not be reached, or did not answer as expected.

    The ``kubectl`` logs then stay.
    """


@dataclass
class Stream:
    """One pod's logs in Loki: its labels, and the lines that were read."""

    component: str
    """The pod's ``app.kubernetes.io/name``, e.g. ``smarter-worker``: the log's folder."""

    pod: str
    """The pod's name: the log's file name."""

    lines: list[tuple[int, str]] = field(default_factory=list)
    """Each line's time, in Unix nanoseconds, and its text, oldest first."""

    truncated: bool = False
    """Whether :data:`MAX_LINES_PER_POD` stopped the reading before the end of the period."""


def parse_since(since: str) -> timedelta:
    """
    Turn the workflow's ``since`` input into a period, at most :data:`RETENTION`.

    :param since: e.g. ``25h``, ``90m`` or ``3d``, or an empty string, which means everything that
        Loki keeps.
    :returns: The period.
    :raises ValueError: If ``since`` is not empty and is not a number followed by ``m``, ``h`` or ``d``.
    """
    text = since.strip()
    if not text:
        return RETENTION
    match = SINCE.match(text)
    if match is None:
        raise ValueError(f"since must be like 25h, 90m or 3d, not {since!r}")
    amount = int(match.group("amount"))
    unit = UNITS[match.group("unit")]
    period = timedelta(**{unit: amount})
    if period > RETENTION:
        return RETENTION
    return period


def nanoseconds(when: datetime) -> int:
    """
    Convert a time into Unix nanoseconds, the unit of Loki's api.

    :param when: The time, with its time zone.
    :returns: The nanoseconds since 1970-01-01T00:00:00Z.
    """
    return int(when.timestamp() * 1_000_000_000)


def timestamp(value: int) -> str:
    """
    Format Unix nanoseconds as ``kubectl logs --timestamps`` writes a line's time.

    :param value: The time, in Unix nanoseconds.
    :returns: e.g. ``2026-11-02T14:00:00.123456789Z``: RFC 3339, in UTC, with nanoseconds.
    """
    seconds = value // 1_000_000_000
    fraction = value % 1_000_000_000
    when = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{when:%Y-%m-%dT%H:%M:%S}.{fraction:09d}Z"


def logql_string(value: str) -> str:
    """
    Quote a label value for a LogQL selector, whose strings follow Go's escapes.

    :param value: The value, e.g. a namespace.
    :returns: The value in double quotes, with backslashes and double quotes escaped.
    """
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    return f'"{escaped}"'


class Loki:
    """
    The http api of one Loki, at a base url, e.g. ``http://127.0.0.1:41234``.

    :param base_url: Loki's base url, without a trailing slash.
    """

    def __init__(self, base_url: str):
        """
        Remember Loki's base url.

        Nothing is sent until a method asks.

        :param base_url: Loki's base url, without a trailing slash, e.g. ``http://127.0.0.1:41234``.
        """
        self.base_url = base_url

    def get(self, path: str, params: dict[str, str]) -> dict:
        """
        Send a GET to Loki's api, and return its JSON answer.

        :param path: The api path, e.g. ``/loki/api/v1/query_range``.
        :param params: The query string.
        :returns: The answer, which Loki marks ``"status": "success"``.
        :raises LokiError: If Loki cannot be reached, answers an error, or answers something that is not
            a successful JSON object.
        """
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        try:
            # nosec B310: the url is the local port-forward's, http://127.0.0.1:<port>, built here.
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:  # nosec B310
                body = response.read()
        except urllib.error.HTTPError as e:
            raise LokiError(f"Loki answered HTTP {e.code} to {path}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LokiError(f"Loki could not be reached: {type(e).__name__}") from e
        try:
            answer = json.loads(body)
        except ValueError as e:
            raise LokiError(f"Loki's answer to {path} is not JSON") from e
        if not isinstance(answer, dict) or answer.get("status") != "success":
            raise LokiError(f"Loki's answer to {path} is not a success")
        return answer

    def ready(self) -> bool:
        """
        Ask whether Loki is ready to answer queries.

        :returns: True if ``/ready`` answers HTTP 200.
        """
        url = f"{self.base_url}/ready"
        try:
            # nosec B310: the url is the local port-forward's, built here.
            with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def streams(self, namespace: str, start: int, end: int) -> list[Stream]:
        """
        List the pods that logged in a period, with their components.

        :param namespace: The environment's namespace, e.g. ``smarter-ubc-prod``.
        :param start: The period's start, in Unix nanoseconds.
        :param end: The period's end, in Unix nanoseconds.
        :returns: One stream per pod, sorted by component, then pod. A pod with several containers is
            one stream: its log holds every container's lines.
        :raises LokiError: If Loki cannot list them.
        """
        selector = "{namespace=" + logql_string(namespace) + "}"
        params = {"match[]": selector, "start": str(start), "end": str(end)}
        answer = self.get("/loki/api/v1/series", params)
        found: dict[str, Stream] = {}
        for labels in answer.get("data") or []:
            if not isinstance(labels, dict):
                continue
            pod = labels.get("pod")
            if not pod:
                continue
            component = labels.get("component") or "unknown"
            if pod not in found:
                found[pod] = Stream(component=component, pod=pod)
        streams = list(found.values())
        streams.sort(key=stream_order)
        return streams

    def read(self, stream: Stream, namespace: str, start: int, end: int) -> None:
        """
        Read one pod's lines of a period, oldest first, a page at a time, into the stream.

        Each page starts at the time of the last line of the page before. Lines that have that same time
        were already read, so they are skipped, rather than lost or repeated. The reading stops at the end
        of the period, or at :data:`MAX_LINES_PER_POD` lines.

        Side effects: one HTTP GET to Loki per page.

        :param stream: The pod, whose ``lines`` and ``truncated`` this fills.
        :param namespace: The environment's namespace.
        :param start: The period's start, in Unix nanoseconds.
        :param end: The period's end, in Unix nanoseconds.
        :raises LokiError: If a page cannot be read.
        """
        selector = "{namespace=" + logql_string(namespace) + ", pod=" + logql_string(stream.pod) + "}"
        page_start = start
        seen_at_page_start: set[str] = set()
        while True:
            params = {
                "query": selector,
                "start": str(page_start),
                "end": str(end),
                "limit": str(PAGE_SIZE),
                "direction": "forward",
            }
            answer = self.get("/loki/api/v1/query_range", params)
            page = page_lines(answer)
            new_lines = []
            for line in page:
                if line[0] == page_start and line[1] in seen_at_page_start:
                    continue
                new_lines.append(line)
            stream.lines.extend(new_lines)
            if len(stream.lines) >= MAX_LINES_PER_POD:
                del stream.lines[MAX_LINES_PER_POD:]
                stream.truncated = True
                return
            if len(page) < PAGE_SIZE or not new_lines:
                return
            last_time = page[-1][0]
            seen_at_page_start = set()
            for line_time, text in page:
                if line_time == last_time:
                    seen_at_page_start.add(text)
            page_start = last_time


def stream_order(stream: Stream) -> tuple[str, str]:
    """
    Give the key that sorts streams by component, then pod, for ``sort()``.

    :param stream: A stream.
    :returns: Its component and its pod.
    """
    return (stream.component, stream.pod)


def page_lines(answer: dict) -> list[tuple[int, str]]:
    """
    Read the lines of one ``query_range`` answer, oldest first.

    Loki groups an answer's lines by stream, e.g. one per container of the pod, and orders them only
    within a stream, so they are sorted again here.

    :param answer: Loki's answer.
    :returns: Each line's time, in Unix nanoseconds, and its text.
    :raises LokiError: If a line's time is not a number.
    """
    data = answer.get("data") or {}
    lines = []
    for stream in data.get("result") or []:
        for value in stream.get("values") or []:
            if len(value) < 2:
                continue
            try:
                line_time = int(value[0])
            except (TypeError, ValueError) as e:
                raise LokiError("a line of Loki's answer has no time") from e
            lines.append((line_time, str(value[1])))
    lines.sort(key=line_order)
    return lines


def line_order(line: tuple[int, str]) -> int:
    """
    Give the key that sorts lines by time, for ``sort()``.

    Python's sort keeps lines with the same time in the order they came, so a container's lines that
    share a time stay in Loki's order.

    :param line: A line's time, in Unix nanoseconds, and its text.
    :returns: Its time.
    """
    return line[0]


def free_port() -> int:
    """
    Find a local port that nothing listens on, for the port-forward.

    :returns: The port's number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


def open_port_forward(context: str, namespace: str, port: int) -> subprocess.Popen:
    """
    Start ``kubectl port-forward`` from a local port to the namespace's Loki, in the background.

    :param context: The kubeconfig context of the cluster.
    :param namespace: The environment's namespace.
    :param port: The local port.
    :returns: The running ``kubectl``, which :func:`close_port_forward` stops.
    """
    command = [
        "kubectl",
        "--context",
        context,
        "--namespace",
        namespace,
        "port-forward",
        LOKI_SERVICE,
        f"{port}:{LOKI_PORT}",
        "--address",
        "127.0.0.1",
    ]
    # its output names the pod and the ports only, but is discarded all the same: the log is public.
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603


def close_port_forward(process: subprocess.Popen) -> None:
    """
    Stop a port-forward that :func:`open_port_forward` started.

    :param process: The running ``kubectl``.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def wait_until_ready(loki: Loki, process: subprocess.Popen) -> None:
    """
    Wait for the port-forward and Loki to answer.

    :param loki: Loki, through the port-forward.
    :param process: The port-forward's ``kubectl``.
    :raises LokiError: If ``kubectl`` exits, e.g. because the namespace has no Loki, or Loki is not ready
        within :data:`READY_TIMEOUT` seconds.
    """
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LokiError("kubectl port-forward stopped: the namespace may have no Loki")
        if loki.ready():
            return
        time.sleep(1)
    raise LokiError(f"Loki was not ready within {READY_TIMEOUT} seconds")


def write_logs(streams: list[Stream], folder: pathlib.Path) -> int:
    """
    Write each pod's lines to ``<component>/<pod>.log``, as ``kubectl logs --timestamps`` writes them.

    :param streams: The pods, with their lines.
    :param folder: The folder to write into, which is created.
    :returns: The number of lines written.
    """
    written = 0
    for stream in streams:
        component_folder = folder / stream.component
        component_folder.mkdir(parents=True, exist_ok=True)
        path = component_folder / f"{stream.pod}.log"
        with path.open("w", encoding="utf-8") as log:
            for line_time, text in stream.lines:
                log.write(f"{timestamp(line_time)} {text}\n")
                written += 1
    return written


def replace_logs(log_dir: pathlib.Path, loki_dir: pathlib.Path) -> None:
    """
    Replace every deployment folder of the log folder with the folders that were read from Loki.

    ``inventory/``, and the files at the top of the log folder, are kept.

    :param log_dir: The log folder that ``download-logs.sh`` wrote.
    :param loki_dir: The folder that :func:`write_logs` wrote.
    """
    for child in log_dir.iterdir():
        if child.is_dir() and child.name != "inventory":
            shutil.rmtree(child)
    for child in loki_dir.iterdir():
        shutil.move(str(child), str(log_dir / child.name))


def write_source(log_dir: pathlib.Path, text: str) -> None:
    """
    Write :data:`SOURCE_NAME`, which tells the review where the logs came from.

    :param log_dir: The log folder.
    :param text: What to write, e.g. ``loki: ...``, ending with a newline.
    """
    inventory = log_dir / "inventory"
    inventory.mkdir(parents=True, exist_ok=True)
    (inventory / SOURCE_NAME).write_text(text, encoding="utf-8")


def read_from_loki(loki: Loki, namespace: str, period: timedelta, loki_dir: pathlib.Path) -> str:
    """
    Read every pod's logs of a period from Loki, and write them into a folder.

    :param loki: Loki, through the port-forward.
    :param namespace: The environment's namespace.
    :param period: How far back to read.
    :param loki_dir: The folder to write the logs into.
    :returns: The text of :data:`SOURCE_NAME`, with the period, the pods and the lines.
    :raises LokiError: If any pod cannot be read, or Loki has no log of the period.
    """
    end_time = datetime.now(timezone.utc)
    start_time = end_time - period
    start = nanoseconds(start_time)
    end = nanoseconds(end_time)
    streams = loki.streams(namespace, start, end)
    if not streams:
        raise LokiError("Loki has no log of this namespace in the period")
    for stream in streams:
        loki.read(stream, namespace, start, end)
    lines = write_logs(streams, loki_dir)

    source = [
        "loki: every pod's log of the period, including pods that no longer run, from the environment's Loki.",
        f"period: {start_time:%Y-%m-%dT%H:%M:%SZ} to {end_time:%Y-%m-%dT%H:%M:%SZ}",
        f"pods: {len(streams)}",
        f"lines: {lines}",
    ]
    for stream in streams:
        if stream.truncated:
            source.append(f"truncated: {stream.component}/{stream.pod}.log, at {MAX_LINES_PER_POD} lines")
    return "\n".join(source) + "\n"


def run(context: str, namespace: str, log_dir: pathlib.Path, since: str) -> bool:
    """
    Replace the log folder's ``kubectl`` logs with Loki's, or keep them and say why.

    Side effects: a ``kubectl port-forward`` for the duration, HTTP GETs to Loki through it, a temporary
    folder beside the log folder, the log folder's deployment folders replaced, and :data:`SOURCE_NAME`
    written. It prints counts only.

    :param context: The kubeconfig context of the cluster.
    :param namespace: The environment's namespace.
    :param log_dir: The log folder that ``download-logs.sh`` wrote.
    :param since: The workflow's ``since`` input, e.g. ``25h``, or empty for everything Loki keeps.
    :returns: True if the logs now come from Loki, False if the ``kubectl`` logs were kept.
    """
    period = parse_since(since)
    loki_dir = log_dir.parent / f"{log_dir.name}.loki"
    shutil.rmtree(loki_dir, ignore_errors=True)
    port = free_port()
    process = open_port_forward(context, namespace, port)
    loki = Loki(f"http://127.0.0.1:{port}")
    try:
        wait_until_ready(loki, process)
        source = read_from_loki(loki, namespace, period, loki_dir)
    except LokiError as e:
        print(f"::warning::The logs were not read from Loki, so the review reads kubectl's: {e}")
        write_source(log_dir, f"kubectl: the running pods' logs, from kubectl logs. Loki could not be read: {e}\n")
        shutil.rmtree(loki_dir, ignore_errors=True)
        return False
    finally:
        close_port_forward(process)

    replace_logs(log_dir, loki_dir)
    shutil.rmtree(loki_dir, ignore_errors=True)
    write_source(log_dir, source)
    pods = len(list(log_dir.glob("*/*.log")))
    print(f"replaced the logs with Loki's: {pods} pod(s), {period} back")
    return True


def main() -> int:
    """
    Read the command line, and replace the logs with Loki's.

    :returns: 0, also when the ``kubectl`` logs were kept, so that the review still runs; 2 if the
        command line is wrong.
    """
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--context", required=True, help="the kubeconfig context of the cluster")
    parser.add_argument("--namespace", required=True, help="the environment's namespace")
    parser.add_argument("--log-dir", required=True, help="the folder that download-logs.sh wrote")
    parser.add_argument("--since", default="", help="how far back to read, e.g. 25h; empty: all that Loki keeps")
    args = parser.parse_args()
    log_dir = pathlib.Path(args.log_dir)
    if not (log_dir / "inventory").is_dir():
        print(f"{log_dir} has no inventory: run download-logs.sh first.", file=sys.stderr)
        return 2
    try:
        parse_since(args.since)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    run(args.context, args.namespace, log_dir, args.since)
    return 0


if __name__ == "__main__":
    sys.exit(main())

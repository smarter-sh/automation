#!/usr/bin/env python3
"""Probe a deployed environment's logs for repeated errors, and keep Slack posted until they stop.

Usage::

    python3 -E -s scripts/error_watch/watch.py due --state <file> [--force]
    python3 -E -s scripts/error_watch/watch.py probe --context <kube context> --namespace <namespace> \\
        --window-minutes 30 --results <file>
    python3 -E -s scripts/error_watch/watch.py update --state <file> --results <file> --channel smarter \\
        --where ubc.smarter.sh --run-url <url> [--min-repeats 5] [--summary <file>] [--dry-run]

The error watch (``.github/workflows/errorWatch.yml``) runs these three steps:

``due``
    Reads the saved state (:mod:`error_watch.incident`), and says whether a probe is due now, and how far
    back it reads (:mod:`error_watch.schedule`), in ``$GITHUB_OUTPUT``: ``probe=true`` and
    ``window=30``. Most runs stop here.
``probe``
    Reads every pod's log of the period from the environment's Loki, through a ``kubectl port-forward``,
    as ``pod_log/loki.py`` does, finds the tracebacks and ERROR lines (:mod:`error_watch.detect`), and
    writes every error it found, counted, to the results file. It prints only counts.
``update``
    Decides what the findings mean for the open problem, if any (:func:`error_watch.incident.decide`),
    posts the Slack message, if one is due, and saves the new state. If Slack refuses the message, the
    state is not saved and the step fails, so the next run tries again. Without ``SLACK_BOT_TOKEN``,
    nothing is posted, and the state moves on. ``--dry-run`` prints the message, and saves nothing.

The results file holds the redacted messages, for Slack, so it is never printed or uploaded: this
repository's workflow logs are public. The run's summary names each problem by its label only: the
exception's type and the code's location, or a short id.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Run by its path, a script has only its own folder (scripts/error_watch) on sys.path. Add scripts/, so
# that it can import its package, pod_log, and the shared modules.
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from error_watch import detect, incident, schedule  # noqa: E402
from pod_log import loki as pod_log_loki  # noqa: E402
from slack_post import post  # noqa: E402

DESCRIPTION = "Probe a deployed environment's logs for repeated errors, and keep Slack posted until they stop."
"""The command line's description."""

THREAD_ACTIONS = ("new", "remind", "resolve")
"""The actions whose message replies in the open problem's thread."""

BROADCAST_ACTIONS = ("new", "resolve")
"""The thread replies that are also shown in the channel."""


def now() -> datetime:
    """
    Give the current time; a function of its own, so that the tests can replace it.

    :returns: The current time, in UTC.
    """
    return datetime.now(timezone.utc)


def write_output(path: str, values: dict[str, str]) -> None:
    """
    Append a step's outputs to ``$GITHUB_OUTPUT``, or print them when there is no such file.

    :param path: The file, or empty.
    :param values: The outputs' names and values.
    """
    lines = []
    for name, value in values.items():
        lines.append(f"{name}={value}")
    text = "\n".join(lines) + "\n"
    if not path:
        print(text, end="")
        return
    with open(path, "a", encoding="utf-8") as output:
        output.write(text)


def command_due(args: argparse.Namespace) -> int:
    """
    Say whether a probe is due, and how far back it reads.

    :param args: The command line: ``state``, ``force`` and ``github_output``.
    :returns: 0.
    """
    state = incident.load_state(Path(args.state))
    current_time = now()
    incident_open = state.incident is not None
    probe_interval = schedule.interval(current_time, incident_open)
    last_probe = incident.parse_time(state.last_probe)
    due = args.force or schedule.is_due(current_time, last_probe, probe_interval)
    window = schedule.window(probe_interval)
    window_minutes = int(window.total_seconds() // 60)
    interval_minutes = int(probe_interval.total_seconds() // 60)

    if due:
        print(f"A probe is due: every {interval_minutes} minutes now, reading {window_minutes} minutes back.")
    else:
        print(f"No probe is due: the last was at {state.last_probe}, every {interval_minutes} minutes now.")
    probe_text = "true" if due else "false"
    write_output(args.github_output, {"probe": probe_text, "window": str(window_minutes)})
    return 0


def read_findings(loki: pod_log_loki.Loki, namespace: str, window: timedelta) -> dict:
    """
    Read every pod's log of a period from Loki, and count its errors.

    :param loki: Loki, through the port-forward.
    :param namespace: The environment's namespace.
    :param window: How far back to read.
    :returns: The results: when the logs were read, the period, the pods and lines read, and every finding.
    :raises pod_log.loki.LokiError: If Loki cannot be read.
    """
    end_time = now()
    start_time = end_time - window
    start = pod_log_loki.nanoseconds(start_time)
    end = pod_log_loki.nanoseconds(end_time)
    streams = loki.streams(namespace, start, end)
    events = []
    lines = 0
    for stream in streams:
        loki.read(stream, namespace, start, end)
        lines += len(stream.lines)
        stream_events = detect.parse(stream.component, stream.lines)
        events.extend(stream_events)
    findings = detect.group(events)
    return {
        "probed_at": incident.format_time(end_time),
        "window_minutes": int(window.total_seconds() // 60),
        "pods": len(streams),
        "lines": lines,
        "findings": findings_as_dicts(findings),
    }


def findings_as_dicts(findings: list[detect.Finding]) -> list[dict]:
    """
    Turn findings into dicts, to save them as JSON.

    :param findings: The findings.
    :returns: One dict per finding, in the same order.
    """
    finding_dicts = []
    for finding in findings:
        finding_dicts.append(asdict(finding))
    return finding_dicts


def command_probe(args: argparse.Namespace) -> int:
    """
    Read the logs from Loki, and write the errors found to the results file.

    Side effects: a ``kubectl port-forward`` for the duration, and HTTP GETs to Loki through it.

    :param args: The command line: ``context``, ``namespace``, ``window_minutes`` and ``results``.
    :returns: 0, or 1 if Loki could not be read.
    """
    window = timedelta(minutes=args.window_minutes)
    port = pod_log_loki.free_port()
    process = pod_log_loki.open_port_forward(args.context, args.namespace, port)
    loki = pod_log_loki.Loki(f"http://127.0.0.1:{port}")
    try:
        pod_log_loki.wait_until_ready(loki, process)
        results = read_findings(loki, args.namespace, window)
    except pod_log_loki.LokiError as e:
        print(f"::error::The logs could not be read from Loki: {e}")
        return 1
    finally:
        pod_log_loki.close_port_forward(process)

    results_path = Path(args.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_text = json.dumps(results, indent=2)
    results_path.write_text(results_text + "\n", encoding="utf-8")
    findings = results["findings"]
    print(f"read {results['lines']} line(s) of {results['pods']} pod(s), {args.window_minutes} minutes back")
    print(f"found {len(findings)} distinct error(s)")
    return 0


def send(text: str, decision: incident.Decision, state: incident.State, channel: str) -> dict | None:
    """
    Post a decision's message to Slack: a new message for a new problem, a reply in its thread otherwise.

    :param text: The message.
    :param decision: What was decided.
    :param state: The state before the probe, whose incident has the thread.
    :param channel: The channel to post a new problem to.
    :returns: Slack's answer, or None if there is no ``SLACK_BOT_TOKEN``.
    :raises RuntimeError: If Slack refused the message.
    :raises OSError: If Slack could not be reached.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("::warning::SLACK_BOT_TOKEN is not set, so nothing is posted.")
        return None
    thread_ts = ""
    target = channel
    open_incident = state.incident
    replies = decision.action in THREAD_ACTIONS
    if replies and open_incident is not None and open_incident.thread_ts:
        thread_ts = open_incident.thread_ts
        target = open_incident.channel or channel
    broadcast = decision.action in BROADCAST_ACTIONS
    answer = post(target, text, token, thread_ts=thread_ts, broadcast=broadcast)
    print(f"posted the '{decision.action}' message to Slack")
    return answer


def summary_text(decision: incident.Decision, results: dict, min_repeats: int, where: str, dry_run: bool) -> str:
    """
    Write the run's summary, which is public: labels and counts only, never a message from the logs.

    :param decision: What was decided.
    :param results: The probe's results.
    :param min_repeats: How many times an error must be logged to be a problem.
    :param where: The environment's name.
    :param dry_run: Whether this is a dry run.
    :returns: The summary, in GitHub's Markdown.
    """
    dry_run_note = " (dry run: nothing posted or saved)" if dry_run else ""
    lines = [
        f"## Error watch of {where}",
        "",
        f"Read {results['lines']} line(s) of {results['pods']} pod(s), {results['window_minutes']} minutes back.",
        f"Decision: **{decision.action}**{dry_run_note}.",
        "",
    ]
    repeating = []
    for finding in results["findings"]:
        if finding["count"] >= min_repeats:
            repeating.append(finding)
    if not repeating:
        lines.append(f"No error was logged {min_repeats} times or more.")
        return "\n".join(lines) + "\n"
    lines.append("| Error | Count | Components |")
    lines.append("| --- | ---: | --- |")
    for finding in repeating:
        components = ", ".join(finding["components"])
        lines.append(f"| {finding['label']} | {finding['count']} | {components} |")
    return "\n".join(lines) + "\n"


def command_update(args: argparse.Namespace) -> int:
    """
    Decide what the probe's findings mean, tell Slack if it is due, and save the state.

    :param args: The command line.
    :returns: 0, or 1 if Slack refused the message, in which case the state is not saved.
    """
    state_path = Path(args.state)
    state = incident.load_state(state_path)
    results_text = Path(args.results).read_text(encoding="utf-8")
    results = json.loads(results_text)
    probed_at = datetime.fromisoformat(results["probed_at"])
    decision = incident.decide(state, results["findings"], probed_at, args.min_repeats)
    text = incident.message(decision, state, probed_at, args.where, results["window_minutes"], args.run_url)
    print(f"decision: {decision.action}")

    if args.summary:
        summary = summary_text(decision, results, args.min_repeats, args.where, args.dry_run)
        with open(args.summary, "a", encoding="utf-8") as summary_file:
            summary_file.write(summary)
    if args.dry_run:
        if text:
            print("The Slack message would be:")
            print(text)
        return 0

    posted = None
    if text:
        try:
            posted = send(text, decision, state, args.channel)
        except (OSError, RuntimeError, ValueError) as e:
            print(f"::error::The Slack message was not posted, so the next run tries again: {e}")
            return 1
    new_state = incident.apply(state, decision, probed_at, posted)
    incident.save_state(new_state, state_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Describe the command line.

    :returns: The parser, with one subcommand per step.
    """
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest="command", required=True)

    due = commands.add_parser("due", help="say whether a probe is due")
    due.add_argument("--state", required=True, help="the saved state's file")
    due.add_argument("--force", action="store_true", help="probe now, whatever the schedule")
    due.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""), help="the step's output file")
    due.set_defaults(handler=command_due)

    probe = commands.add_parser("probe", help="read the logs from Loki, and count the errors")
    probe.add_argument("--context", required=True, help="the kubeconfig context of the cluster")
    probe.add_argument("--namespace", required=True, help="the environment's namespace")
    probe.add_argument("--window-minutes", type=int, required=True, help="how far back to read")
    probe.add_argument("--results", required=True, help="the file to write the errors to")
    probe.set_defaults(handler=command_probe)

    update = commands.add_parser("update", help="tell Slack, if it is due, and save the state")
    update.add_argument("--state", required=True, help="the saved state's file")
    update.add_argument("--results", required=True, help="the probe's results")
    update.add_argument("--channel", required=True, help="the Slack channel of a new problem")
    update.add_argument("--where", required=True, help="the environment's name, e.g. ubc.smarter.sh")
    update.add_argument("--run-url", required=True, help="the workflow run's page")
    update.add_argument("--min-repeats", type=int, default=5, help="how many times an error must repeat")
    update.add_argument("--summary", default="", help="the run's summary file, $GITHUB_STEP_SUMMARY")
    update.add_argument("--dry-run", action="store_true", help="print the message, and save nothing")
    update.set_defaults(handler=command_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    Run one step.

    :param argv: The command line, without the program's name; None reads ``sys.argv``.
    :returns: The step's exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())

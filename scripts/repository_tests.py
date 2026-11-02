#!/usr/bin/env python3
"""Start the test workflow of every smarter-sh repository, wait for each run, and report the results.

This is the core of ``.github/workflows/repositoryTests.yml``, the nightly Repository Tests
workflow. Each repository of the organization tests itself on its pushes and pull requests, so a
repository that nobody touched for a while is never retested, though its dependencies, its
runners' images and the services it calls all move on. This job retests every one, every night.

Usage::

    python3 -E -s scripts/repository_tests.py run --output results.json [--ref BRANCH] [--dry-run]
    python3 -E -s scripts/repository_tests.py report --results results.json --run-url URL \\
        --summary "$GITHUB_STEP_SUMMARY" | python3 -I scripts/slack_post.py --channel general

- ``run`` lists the organization's repositories that aren't archived, finds each one's test
  workflow (``.github/workflows/tests.yml``, or ``test.yml``; see :data:`WORKFLOW_FILES`), starts it
  with a ``workflow_dispatch`` event on the repository's default branch (or ``--ref``), then polls
  every run until it completes, or until ``--timeout-minutes``. It writes one :class:`Result` per
  repository, as a JSON list. A dry run lists the workflows it would start, and starts none.
- ``report`` appends one Markdown table to the run's page, and prints one Slack message: every
  suite passed, or the ones that failed, with links. It exits with 1 when a suite failed, timed
  out or couldn't be started.

A repository without a test workflow is left out. One whose workflow is disabled, or has no
``workflow_dispatch`` trigger, is reported as not started, which doesn't fail the run: it needs a
maintainer's decision, not a fix.
"""

import argparse
import json
import subprocess  # nosec B404: only for CalledProcessError
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from github_cli import ORGANIZATION, error_line, repositories, run_json

WORKFLOW_FILES = (".github/workflows/tests.yml", ".github/workflows/test.yml")
"""The paths of a repository's test workflow, in the order they are looked for."""

STATUS_TEXT = {
    "success": ("passed", ":white_check_mark:"),
    "failure": ("failed", ":x:"),
    "cancelled": ("cancelled", ":x:"),
    "timed_out": ("timed out on GitHub", ":x:"),
    "startup_failure": ("failed to start", ":x:"),
    "action_required": ("waiting for approval", ":warning:"),
    "waiting": ("still running when this job stopped waiting", ":hourglass_flowing_sand:"),
    "not_started": ("not started", ":grey_question:"),
    "would_start": ("would start (dry run)", ":information_source:"),
}
"""Each :attr:`Result.status`'s words, for the run's page and Slack, and its emoji.

The first six
are GitHub's own conclusions of a completed run.
"""

CLOCK_MARGIN = timedelta(minutes=1)
"""How far before the dispatch request a run may say it was created: a runner's clock can be ahead of GitHub's."""

FAILED = ("failure", "cancelled", "timed_out", "startup_failure", "waiting")
"""The statuses that fail this job's run."""


@dataclass
class Result:
    """
    One repository's test run.

    :attr:`status` is a completed run's conclusion (``success``, ``failure``, ...), or one of
    this job's own: ``waiting`` (still running at the deadline), ``not_started`` (with
    :attr:`reason`), ``would_start`` (a dry run).
    """

    #: The repository, as ``owner/name``.
    repository: str
    #: The test workflow's path, e.g. ``.github/workflows/tests.yml``.
    workflow: str
    #: The branch it ran on.
    ref: str
    #: The run's id, or 0 before it is known.
    run_id: int = 0
    #: The run's page.
    url: str = ""
    #: See the class's description.
    status: str = "not_started"
    #: Why the run wasn't started, or empty.
    reason: str = ""

    @property
    def failed(self) -> bool:
        """Whether this repository fails the job's run."""
        return self.status in FAILED


def test_workflow(repository: str) -> dict | None:
    """
    Find a repository's test workflow.

    :param repository: The repository, as ``owner/name``.
    :returns: GitHub's workflow object (``id``, ``path``, ``state``, ...) or None when it has none.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    workflows = (run_json("gh", "api", f"repos/{repository}/actions/workflows") or {}).get("workflows", [])
    by_path = {workflow["path"]: workflow for workflow in workflows}
    for path in WORKFLOW_FILES:
        if path in by_path:
            return by_path[path]
    return None


def dispatch(result: Result, workflow_id: int, sleep=time.sleep) -> None:
    """
    Start a repository's test workflow, and find its run.

    It asks GitHub for the new run's id (``return_run_details``). Where GitHub's answer has none,
    it looks for the newest ``workflow_dispatch`` run of the workflow on the branch that was
    created after the request, a few times, since a run takes a moment to appear.

    :param result: The repository's result. Its :attr:`Result.run_id`, :attr:`Result.url` and
        :attr:`Result.status` (``queued``) are set, or ``not_started`` with a reason when GitHub
        refused, e.g. a workflow without a ``workflow_dispatch`` trigger (HTTP 422).
    :param workflow_id: The workflow's id.
    :param sleep: ``time.sleep``, which the tests replace.
    """
    started = datetime.now(timezone.utc).replace(microsecond=0) - CLOCK_MARGIN
    try:
        answer = run_json(
            "gh", "api", "--method", "POST", f"repos/{result.repository}/actions/workflows/{workflow_id}/dispatches",
            "-f", f"ref={result.ref}", "-F", "return_run_details=true",
        )  # fmt: skip
    except subprocess.CalledProcessError as error:
        result.status, result.reason = "not_started", error_line(error)
        return
    if isinstance(answer, dict) and answer.get("workflow_run_id"):
        result.run_id = int(answer["workflow_run_id"])
        result.url = answer.get("html_url") or f"https://github.com/{result.repository}/actions/runs/{result.run_id}"
        result.status = "queued"
        return
    created = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(6):
        sleep(5)
        runs = (
            run_json(
                "gh", "api", f"repos/{result.repository}/actions/workflows/{workflow_id}/runs"
                f"?event=workflow_dispatch&branch={result.ref}&created=%3E%3D{created}&per_page=1",
            ) or {}
        ).get("workflow_runs", [])  # fmt: skip
        if runs:
            result.run_id, result.url, result.status = int(runs[0]["id"]), runs[0]["html_url"], "queued"
            return
    result.status, result.reason = "not_started", "started, but its run never appeared"


def start_all(organization: str, ref: str, dry_run: bool, sleep=time.sleep) -> list[Result]:
    """
    Find and start every repository's test workflow.

    :param organization: The organization, e.g. ``smarter-sh``.
    :param ref: The branch to test, or empty for each repository's default branch.
    :param dry_run: Start nothing: every startable workflow is reported as ``would_start``.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: One result per repository that has a test workflow, sorted by repository.
    """
    results = []
    for repository in repositories(organization):
        name = repository["name"]
        try:
            workflow = test_workflow(name)
        except subprocess.CalledProcessError as error:
            results.append(Result(name, "", ref or repository["default_branch"], reason=error_line(error)))
            continue
        if not workflow:
            continue
        result = Result(name, workflow["path"], ref or repository["default_branch"])
        results.append(result)
        if workflow.get("state") != "active":
            result.reason = f"the workflow is {workflow.get('state', 'not active').replace('_', ' ')}"
        elif dry_run:
            result.status = "would_start"
        else:
            dispatch(result, int(workflow["id"]), sleep)
        print(f"{name}: {result.workflow} on {result.ref}: {result.status} {result.reason or result.url}".rstrip())
    return results


def wait_all(
    results: list[Result], timeout_minutes: float, poll_seconds: float, sleep=time.sleep, clock=time.monotonic
) -> None:
    """
    Poll every started run until it completes, or until the deadline.

    :param results: The results; those with a :attr:`Result.run_id` that haven't completed are
        polled, and their :attr:`Result.status` set to the run's conclusion, or ``waiting`` at the
        deadline.
    :param timeout_minutes: How long to wait, in all.
    :param poll_seconds: How long to sleep between polls.
    :param sleep: ``time.sleep``, which the tests replace.
    :param clock: ``time.monotonic``, which the tests replace.
    """
    deadline = clock() + timeout_minutes * 60
    pending = [result for result in results if result.run_id and result.status in ("queued", "in_progress")]
    while pending:
        for result in list(pending):
            try:
                answer = run_json("gh", "api", f"repos/{result.repository}/actions/runs/{result.run_id}") or {}
            except subprocess.CalledProcessError as error:
                print(f"{result.repository}: couldn't read run {result.run_id}: {error_line(error)}", file=sys.stderr)
                continue
            if answer.get("status") == "completed":
                result.status = answer.get("conclusion") or "failure"
                pending.remove(result)
                print(f"{result.repository}: {result.status} {result.url}")
        if not pending:
            break
        if clock() >= deadline:
            for result in pending:
                result.status = "waiting"
            break
        sleep(poll_seconds)


def summary_markdown(results: list[Result], run_url: str) -> str:
    """
    Write the run's job summary: one table row per repository.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The summary, in GitHub Markdown.
    """
    passed = sum(1 for result in results if result.status == "success")
    lines = [
        f"## Repository tests: {passed} of {len(results)} passed",
        "",
        "| Repository | Workflow | Branch | Result | Run |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        text, emoji = STATUS_TEXT.get(result.status, (result.status, ":grey_question:"))
        reason = f" ({result.reason})" if result.reason else ""
        run_link = f"[run]({result.url})" if result.url else ""
        lines.append(
            f"| {result.repository} | {result.workflow.rsplit('/', 1)[-1]} | {result.ref} | {emoji} {text}{reason} | {run_link} |"
        )
    if not results:
        lines.append("| (no repository has a test workflow) | | | | |")
    lines += ["", f"[The run]({run_url})"]
    return "\n".join(lines) + "\n"


def slack_message(results: list[Result], run_url: str) -> str:
    """
    Write the run's one Slack message, in Slack's mrkdwn.

    Every suite passed, or one line per repository that didn't, then a count of the ones that did.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The message.
    """
    passed = [result for result in results if result.status == "success"]
    if any(result.status == "would_start" for result in results):
        would = sum(1 for result in results if result.status == "would_start")
        lines = [f"*Repository tests (dry run): {would} of {len(results)} test suites would start*"]
        lines += [
            f":grey_question: {result.repository}: not started: {result.reason}"
            for result in results
            if result.status == "not_started"
        ]
        return "\n".join(lines + [f"<{run_url}|The run>"])
    if results and len(passed) == len(results):
        return f":white_check_mark: *Repository tests: all {len(results)} test suites passed.* <{run_url}|The run>"
    lines = [f"*Repository tests: {len(passed)} of {len(results)} test suites passed*"]
    for result in results:
        if result in passed:
            continue
        text, emoji = STATUS_TEXT.get(result.status, (result.status, ":grey_question:"))
        reason = f": {result.reason}" if result.reason else ""
        link = f" (<{result.url}|run>)" if result.url else ""
        lines.append(f"{emoji} {result.repository}: {text}{reason}{link}")
    if not results:
        lines.append(":warning: No repository has a test workflow.")
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def save(results: list[Result], path: str) -> None:
    """
    Write the results as a JSON list.

    :param results: The results.
    :param path: The file to write.
    """
    Path(path).write_text(json.dumps([asdict(result) for result in results], indent=2) + "\n", encoding="utf-8")


def load(path: str) -> list[Result]:
    """
    Read the results that :func:`save` wrote.

    :param path: The file.
    :returns: The results, or an empty list when the file is missing or isn't JSON.
    """
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        Result(**{key: value for key, value in item.items() if key in Result.__dataclass_fields__}) for item in items
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Read the command line: ``run`` or ``report``.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The parsed arguments, with the subcommand's name in ``command``.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="Start every test workflow and wait for the runs.")
    run_parser.add_argument("--organization", default=ORGANIZATION)
    run_parser.add_argument("--ref", default="", help="The branch to test. Empty: each repository's default.")
    run_parser.add_argument("--timeout-minutes", type=float, default=75)
    run_parser.add_argument("--poll-seconds", type=float, default=60)
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--dry-run", action="store_true")

    report = commands.add_parser("report", help="Summarize the results.")
    report.add_argument("--results", required=True)
    report.add_argument("--run-url", default="")
    report.add_argument("--summary", help="A file to append the Markdown summary to.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Run one subcommand.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The exit code: 0, or 1 when ``run`` couldn't list the repositories, or when
        ``report`` found no result, or a suite that failed, timed out or never finished.
    """
    args = parse_args(argv)
    if args.command == "run":
        try:
            results = start_all(args.organization, args.ref, args.dry_run)
        except subprocess.CalledProcessError as error:
            print(f"repository_tests.py: couldn't list the repositories: {error_line(error)}", file=sys.stderr)
            save([], args.output)
            return 1
        save(results, args.output)
        wait_all(results, args.timeout_minutes, args.poll_seconds)
        save(results, args.output)
        return 0
    results = load(args.results)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(summary_markdown(results, args.run_url))
    print(slack_message(results, args.run_url))
    return 1 if not results or any(result.failed for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())

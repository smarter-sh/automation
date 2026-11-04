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
  workflow (``.github/workflows/tests.yml``, ``test.yml``, ``ci.yml`` or ``checks.yml``, the first
  one found; see :data:`WORKFLOW_FILES`), starts it
  with a ``workflow_dispatch`` event on the repository's default branch (or ``--ref``), then polls
  every run until it completes, or until ``--timeout-minutes``. It writes one :class:`Result` per
  repository, as a JSON list. A dry run lists the workflows it would start, and starts none.
- ``report`` appends one Markdown table to the run's page, and prints one Slack message: every
  suite passed, or the ones that failed, with links. It exits with 1 when a suite failed, timed
  out or couldn't be started.

A repository without a test workflow isn't tested, and isn't counted, but the run's page lists it,
so that a suite under another name doesn't go unnoticed. One whose workflow is disabled, or has no
``workflow_dispatch`` trigger, is reported as not started, which doesn't fail the run: it needs a
maintainer's decision, not a fix.
"""

import argparse
import subprocess  # nosec B404: only for CalledProcessError
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from github_cli import ORGANIZATION, error_line, repositories, run_json
from result_files import load_results, save_results

WORKFLOW_FILES = (
    ".github/workflows/tests.yml",
    ".github/workflows/test.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/checks.yml",
)
"""The paths of a repository's test workflow, in the order they are looked for.

The first one a repository has is its test workflow: smarter-python has ``tests.yml`` and
``ci.yml``, and only ``tests.yml`` runs.
"""

NO_WORKFLOW = "no_workflow"
"""The :attr:`Result.status` of a repository that has none of :data:`WORKFLOW_FILES`."""

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
    :attr:`reason`), ``would_start`` (a dry run), ``no_workflow`` (nothing to test, see
    :data:`NO_WORKFLOW`).
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
        """Return whether this repository fails the job's run.

        :returns: Whether :attr:`status` is one of the failed statuses.
        """
        return self.status in FAILED


def tested(results: list[Result]) -> list[Result]:
    """
    Keep the repositories that have a test workflow.

    :param results: Every repository's result.
    :returns: The results whose status isn't :data:`NO_WORKFLOW`.
    """
    return [result for result in results if result.status != NO_WORKFLOW]


def untested(results: list[Result]) -> list[Result]:
    """
    Keep the repositories that have no test workflow.

    :param results: Every repository's result.
    :returns: The results whose status is :data:`NO_WORKFLOW`.
    """
    return [result for result in results if result.status == NO_WORKFLOW]


def test_workflow(repository: str) -> dict | None:
    """
    Find a repository's test workflow.

    :param repository: The repository, as ``owner/name``.
    :returns: GitHub's workflow object (``id``, ``path``, ``state``, ...) or None when it has none.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    answer = run_json("gh", "api", f"repos/{repository}/actions/workflows") or {}
    workflows = answer.get("workflows", [])
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
    now = datetime.now(timezone.utc)
    started = now.replace(microsecond=0) - CLOCK_MARGIN
    try:
        answer = run_json(
            "gh", "api", "--method", "POST", f"repos/{result.repository}/actions/workflows/{workflow_id}/dispatches",
            "-f", f"ref={result.ref}", "-F", "return_run_details=true",
        )  # fmt: skip
    except subprocess.CalledProcessError as error:
        result.status = "not_started"
        result.reason = error_line(error)
        return
    if isinstance(answer, dict) and answer.get("workflow_run_id"):
        result.run_id = int(answer["workflow_run_id"])
        default_url = f"https://github.com/{result.repository}/actions/runs/{result.run_id}"
        result.url = answer.get("html_url") or default_url
        result.status = "queued"
        return
    created = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    found = _find_dispatched_run(result, workflow_id, created, sleep)
    if found:
        result.run_id = int(found["id"])
        result.url = found["html_url"]
        result.status = "queued"
        return
    result.status = "not_started"
    result.reason = "started, but its run never appeared"


def _find_dispatched_run(result: Result, workflow_id: int, created: str, sleep) -> dict | None:
    """
    Look for the newest ``workflow_dispatch`` run of a workflow, a few times, since a run takes a moment to appear.

    :param result: The repository's result, for its repository and branch.
    :param workflow_id: The workflow's id.
    :param created: The earliest time the run may say it was created, e.g. ``2026-01-31T04:00:00Z``.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: GitHub's run object (``id``, ``html_url``, ...), or None when none appeared.
    """
    for _ in range(6):
        sleep(5)
        answer = run_json(
            "gh", "api", f"repos/{result.repository}/actions/workflows/{workflow_id}/runs"
            f"?event=workflow_dispatch&branch={result.ref}&created=%3E%3D{created}&per_page=1",
        ) or {}  # fmt: skip
        runs = answer.get("workflow_runs", [])
        if runs:
            return runs[0]
    return None


def start_all(organization: str, ref: str, dry_run: bool, sleep=time.sleep) -> list[Result]:
    """
    Find and start every repository's test workflow.

    :param organization: The organization, e.g. ``smarter-sh``.
    :param ref: The branch to test, or empty for each repository's default branch.
    :param dry_run: Start nothing: every startable workflow is reported as ``would_start``.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: One result per repository, sorted by repository. One without a test workflow has the
        status :data:`NO_WORKFLOW`.
    """
    results = []
    for repository in repositories(organization):
        name = repository["name"]
        branch = ref or repository["default_branch"]
        try:
            workflow = test_workflow(name)
        except subprocess.CalledProcessError as error:
            results.append(Result(name, "", branch, reason=error_line(error)))
            continue
        if not workflow:
            results.append(Result(name, "", branch, status=NO_WORKFLOW))
            print(f"{name}: no test workflow")
            continue
        result = Result(name, workflow["path"], branch)
        results.append(result)
        _start(result, workflow, dry_run, sleep)
        detail = result.reason or result.url
        line = f"{name}: {result.workflow} on {result.ref}: {result.status} {detail}"
        print(line.rstrip())
    return results


def _start(result: Result, workflow: dict, dry_run: bool, sleep) -> None:
    """
    Start a repository's test workflow, unless it isn't active or this is a dry run.

    :param result: The repository's result. Its :attr:`Result.status` and :attr:`Result.reason` are set.
    :param workflow: GitHub's workflow object (``id``, ``path``, ``state``, ...).
    :param dry_run: Start nothing: report the workflow as ``would_start``.
    :param sleep: ``time.sleep``, which the tests replace.
    """
    state = workflow.get("state", "not active")
    if state != "active":
        state_words = state.replace("_", " ")
        result.reason = f"the workflow is {state_words}"
    elif dry_run:
        result.status = "would_start"
    else:
        dispatch(result, int(workflow["id"]), sleep)


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
            if _poll(result):
                pending.remove(result)
        if not pending:
            break
        if clock() >= deadline:
            for result in pending:
                result.status = "waiting"
            break
        sleep(poll_seconds)


def _poll(result: Result) -> bool:
    """
    Read a started run once, and record its conclusion when it has completed.

    :param result: The repository's result, with a :attr:`Result.run_id`. Its :attr:`Result.status` is set to the
        run's conclusion when the run has completed.
    :returns: Whether the run has completed. A failed read is reported, and counts as not completed.
    """
    try:
        answer = run_json("gh", "api", f"repos/{result.repository}/actions/runs/{result.run_id}") or {}
    except subprocess.CalledProcessError as error:
        print(f"{result.repository}: couldn't read run {result.run_id}: {error_line(error)}", file=sys.stderr)
        return False
    if answer.get("status") != "completed":
        return False
    result.status = answer.get("conclusion") or "failure"
    print(f"{result.repository}: {result.status} {result.url}")
    return True


def summary_markdown(results: list[Result], run_url: str) -> str:
    """
    Write the run's job summary.

    One table row per repository that has a test workflow, then a line that lists the ones that have none.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The summary, in GitHub Markdown.
    """
    suites = tested(results)
    passed = sum(1 for result in suites if result.status == "success")
    lines = [
        f"## Repository tests: {passed} of {len(suites)} passed",
        "",
        "| Repository | Workflow | Branch | Result | Run |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in suites:
        text, emoji = _status_words(result.status)
        reason = f" ({result.reason})" if result.reason else ""
        run_link = f"[run]({result.url})" if result.url else ""
        workflow_file = _file_name(result.workflow)
        lines.append(f"| {result.repository} | {workflow_file} | {result.ref} | {emoji} {text}{reason} | {run_link} |")
    if not suites:
        lines.append("| (no repository has a test workflow) | | | | |")
    skipped = untested(results)
    if skipped:
        names = ", ".join(result.repository for result in skipped)
        file_names = [_file_name(path) for path in WORKFLOW_FILES]
        workflow_names = ", ".join(file_names)
        lines += [
            "",
            f"Not tested, {len(skipped)} repositories without a test workflow ({workflow_names}): {names}",
        ]
    lines += ["", f"[The run]({run_url})"]
    return "\n".join(lines) + "\n"


def slack_message(results: list[Result], run_url: str) -> str:
    """
    Write the run's one Slack message, in Slack's mrkdwn.

    Every suite passed, or one line per repository that didn't, then a count of the ones that did.
    Repositories without a test workflow are left out: the run's page lists them.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The message.
    """
    results = tested(results)
    passed = [result for result in results if result.status == "success"]
    would_start = [result for result in results if result.status == "would_start"]
    if would_start:
        return _dry_run_slack_message(results, len(would_start), run_url)
    if results and len(passed) == len(results):
        return f":white_check_mark: *Repository tests: all {len(results)} test suites passed.* <{run_url}|The run>"
    lines = [f"*Repository tests: {len(passed)} of {len(results)} test suites passed*"]
    for result in results:
        if result in passed:
            continue
        text, emoji = _status_words(result.status)
        reason = f": {result.reason}" if result.reason else ""
        link = f" (<{result.url}|run>)" if result.url else ""
        lines.append(f"{emoji} {result.repository}: {text}{reason}{link}")
    if not results:
        lines.append(":warning: No repository has a test workflow.")
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def _dry_run_slack_message(results: list[Result], would_start: int, run_url: str) -> str:
    """
    Write a dry run's Slack message: how many suites would start, and why the others wouldn't.

    :param results: The results of the repositories that have a test workflow.
    :param would_start: How many of them would start.
    :param run_url: The workflow run's url.
    :returns: The message, in Slack's mrkdwn.
    """
    lines = [f"*Repository tests (dry run): {would_start} of {len(results)} test suites would start*"]
    for result in results:
        if result.status == "not_started":
            lines.append(f":grey_question: {result.repository}: not started: {result.reason}")
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def _status_words(status: str) -> tuple[str, str]:
    """
    Put a :attr:`Result.status` in words.

    :param status: The status.
    :returns: ``(words, emoji)`` from :data:`STATUS_TEXT`, or the status itself for one it doesn't know.
    """
    unknown = (status, ":grey_question:")
    return STATUS_TEXT.get(status, unknown)


def _file_name(path: str) -> str:
    """
    Take a workflow's file name from its path.

    :param path: The path, e.g. ``.github/workflows/tests.yml``.
    :returns: The file name, e.g. ``tests.yml``.
    """
    return path.rsplit("/", 1)[-1]


def save(results: list[Result], path: str) -> None:
    """
    Write the results as a JSON list.

    :param results: The results.
    :param path: The file to write.
    """
    save_results(results, path)


def load(path: str) -> list[Result]:
    """
    Read the results that :func:`save` wrote.

    :param path: The file.
    :returns: The results, or an empty list when the file is missing or isn't JSON.
    """
    results = load_results(Result, path)
    if results is None:
        return []
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Read the command line: ``run`` or ``report``.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The parsed arguments, with the subcommand's name in ``command``.
    """
    docstring = __doc__ or ""
    first_line = docstring.splitlines()[0]
    parser = argparse.ArgumentParser(description=first_line)
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
        return _run(args)
    return _report(args)


def _run(args: argparse.Namespace) -> int:
    """
    Start every test workflow, wait for the runs, and save the results.

    :param args: The ``run`` subcommand's arguments.
    :returns: The exit code: 0, or 1 when the repositories couldn't be listed.
    """
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


def _report(args: argparse.Namespace) -> int:
    """
    Append the summary to the run's page, and print the Slack message.

    :param args: The ``report`` subcommand's arguments.
    :returns: The exit code: 0, or 1 when no repository was tested, or a suite failed, timed out or never finished.
    """
    results = load(args.results)
    if args.summary:
        markdown = summary_markdown(results, args.run_url)
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(markdown)
    print(slack_message(results, args.run_url))
    if not tested(results):
        return 1
    has_failed_result = any(result.failed for result in results)
    return 1 if has_failed_result else 0


if __name__ == "__main__":
    sys.exit(main())

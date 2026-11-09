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
  every run until it completes, or until ``--timeout-minutes``. Then it reads every job log of each
  completed run, and counts the tests that ran and passed (see ``suite_counts.py``). It writes one
  :class:`Result` per repository, as a JSON list. A dry run lists the workflows it would start, and
  starts none.
- ``report`` appends one Markdown table to the run's page, and prints one Slack message: one row
  per repository, with its tests run and passed, a check mark or a red X, and a total; then the
  suites that failed, with links. It exits with 1 when a suite failed, timed out or couldn't be
  started.

The counts add up every job of a run: a suite that runs on five Python versions counts five times,
and a suite split into shards counts each shard once. A repository whose log has no count (``go
test`` without ``-v``, or a workflow that only lints or builds) shows ``-``. One whose logs GitHub
wouldn't serve, even after :data:`LOG_ATTEMPTS` tries, shows ``?``.

A run can skip its test jobs when the repository's own workflow finds that the code was already
tested: smarter skips its Python shards when an earlier run passed on the same files, and
smarter-console skips its tests when a push changed no testable file. The skipped jobs' tests are
then counted from the run that last ran them: the newest earlier run of the same workflow, on the
same branch, not a pull request's, in which every skipped job ran (see :func:`earlier_tested_run`).
So the counts are the same whether a suite ran tonight or was skipped.

A repository without a test workflow isn't tested, and isn't counted, but the run's page lists it,
so that a suite under another name doesn't go unnoticed. One whose workflow is disabled, or has no
``workflow_dispatch`` trigger, is reported as not started, which doesn't fail the run: it needs a
maintainer's decision, not a fix.
"""

import argparse
import re
import subprocess  # nosec B404: only for CalledProcessError
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from github_cli import ORGANIZATION, api_pages, error_line, http_status, repositories, run, run_json
from result_files import load_results, save_results
from suite_counts import Counts, count_log

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

STATUS_SYMBOL = {
    ":white_check_mark:": "✅",
    ":x:": "❌",
    ":warning:": "⚠️",
    ":hourglass_flowing_sand:": "⏳",
    ":grey_question:": "❔",
    ":information_source:": "ℹ️",
}
"""The emoji of :data:`STATUS_TEXT` as characters, for the Slack table: Slack doesn't read ``:x:`` in a code block."""

NO_COUNT = "-"
"""What the reports show for a repository whose log has no test count."""

UNREAD_COUNT = "?"
"""What the reports show for a repository whose logs couldn't be read: its count is unknown."""

LOG_ATTEMPTS = 3
"""How many times a job's log is asked for, when GitHub answers with a server error."""

LOG_RETRY_SECONDS = 20
"""How long to wait before asking for a job's log again: GitHub's 5xx answers usually pass in seconds."""

COUNTED_STATUSES = ("success", "failure", "cancelled", "timed_out")
"""The conclusions of a run whose job logs are read for the test counts."""

LOG_JOB_CONCLUSIONS = ("success", "failure")
"""The conclusions of a job whose log is read: a skipped or cancelled job ran no tests."""

EARLIER_RUNS = 30
"""How many earlier runs of a workflow are looked at for the one that last ran its skipped jobs."""

EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
"""An expression in a job's name, e.g. ``${{ matrix.shard }}``: GitHub leaves it there when it skips the job."""

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
    #: The tests the run's jobs ran, or None when their logs have no count.
    tests_run: int | None = None
    #: The tests that passed, or None when the logs have no count.
    tests_passed: int | None = None
    #: Why the run's logs couldn't be read, e.g. ``HTTP 503``, or empty.
    count_error: str = ""
    #: The page of the earlier run whose logs gave the skipped jobs' counts, or empty.
    counts_from: str = ""

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


def count_all(results: list[Result], sleep=time.sleep) -> None:
    """
    Count the tests of every run that completed.

    :param results: The results. Those whose :attr:`Result.status` is one of
        :data:`COUNTED_STATUSES` get their :attr:`Result.tests_run` and :attr:`Result.tests_passed`.
    :param sleep: ``time.sleep``, which the tests replace.
    """
    for result in results:
        if not result.run_id:
            continue
        if result.status not in COUNTED_STATUSES:
            continue
        count_tests(result, sleep)


def count_tests(result: Result, sleep=time.sleep) -> None:
    """
    Count the tests of one run, adding up the summaries in every job's log.

    A log that can't be read leaves the counts unknown, rather than too low, and sets
    :attr:`Result.count_error`.

    :param result: The repository's result, with a :attr:`Result.run_id`. Its :attr:`Result.tests_run`
        and :attr:`Result.tests_passed` are set, or left None when no log has a count.
    :param sleep: ``time.sleep``, which the tests replace.
    """
    try:
        total = _run_counts(result, sleep)
    except subprocess.CalledProcessError as error:
        status = http_status(error)
        result.count_error = f"HTTP {status}"
        print(f"{result.repository}: couldn't count the tests: {error_line(error)}", file=sys.stderr)
        return
    except ValueError as error:
        result.count_error = "unexpected answer from GitHub"
        print(f"{result.repository}: couldn't count the tests: {error}", file=sys.stderr)
        return
    if total is None:
        print(f"{result.repository}: no test count in the logs")
        return
    result.tests_run = total.run
    result.tests_passed = total.passed
    print(f"{result.repository}: {total.run} tests run, {total.passed} passed")


def _run_counts(result: Result, sleep) -> Counts | None:
    """
    Add up the counts of every job of a run that passed or failed, and of the jobs it skipped.

    :param result: The repository's result, with a :attr:`Result.run_id`. Its
        :attr:`Result.counts_from` is set when the skipped jobs were counted from an earlier run.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: The counts, or None when no job's log has one.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    :raises ValueError: When GitHub's list of jobs isn't one.
    """
    jobs = run_jobs(result.repository, result.run_id)
    total = _jobs_counts(result.repository, jobs, sleep)
    skipped = skipped_job_names(jobs)
    if not skipped or result.status != "success":
        return total
    earlier = earlier_tested_run(result, skipped)
    if earlier is None:
        print(f"{result.repository}: no earlier run ran the skipped jobs: {', '.join(skipped)}")
        return total
    earlier_run, earlier_jobs = earlier
    result.counts_from = earlier_run.get("html_url", "")
    print(f"{result.repository}: counting the skipped jobs from {result.counts_from}")
    tested_jobs = []
    for job in earlier_jobs:
        if _matches_any(job.get("name", ""), skipped):
            tested_jobs.append(job)
    earlier_counts = _jobs_counts(result.repository, tested_jobs, sleep)
    if earlier_counts is None:
        return total
    if total is None:
        return earlier_counts
    total.add(earlier_counts)
    return total


def run_jobs(repository: str, run_id: int) -> list[dict]:
    """
    List the jobs of a run's latest attempt.

    :param repository: The repository, as ``owner/name``.
    :param run_id: The run's id.
    :returns: GitHub's jobs, each with its ``id``, ``name`` and ``conclusion``.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    :raises ValueError: When GitHub's list of jobs isn't one.
    """
    endpoint = f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest"
    return api_pages(endpoint, key="jobs")


def _jobs_counts(repository: str, jobs: list[dict], sleep) -> Counts | None:
    """
    Add up the counts of the jobs that passed or failed.

    :param repository: The repository, as ``owner/name``.
    :param jobs: The jobs, from :func:`run_jobs`.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: The counts, or None when no job's log has one.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    total = None
    for job in jobs:
        if job.get("conclusion") not in LOG_JOB_CONCLUSIONS:
            continue
        counts = _job_counts(repository, job["id"], sleep)
        if counts is None:
            continue
        if total is None:
            total = Counts()
        total.add(counts)
    return total


def skipped_job_names(jobs: list[dict]) -> list[str]:
    """
    Name the jobs that a run skipped.

    :param jobs: The run's jobs, from :func:`run_jobs`.
    :returns: Their names, as GitHub shows them, e.g. ``python (shard ${{ matrix.shard }} of 8)``.
    """
    names = []
    for job in jobs:
        if job.get("conclusion") == "skipped":
            names.append(job.get("name", ""))
    return names


def _job_name_pattern(skipped_name: str) -> re.Pattern:
    """
    Match the names that a skipped job has in a run where it ran.

    A skipped matrix job keeps its name's expressions, ``python (shard ${{ matrix.shard }} of 8)``,
    or has no matrix values at all, ``test``; where it ran, it is ``python (shard 3 of 8)`` or
    ``test (1)``.

    :param skipped_name: The skipped job's name.
    :returns: A pattern that matches the whole name of each of the job's runs.
    """
    parts = EXPRESSION.split(skipped_name)
    escaped_parts = [re.escape(part) for part in parts]
    name_pattern = ".+".join(escaped_parts)
    return re.compile(f"{name_pattern}(?: \\(.+\\))?")


def _matches_any(job_name: str, skipped_names: list[str]) -> bool:
    """
    Tell whether a job is one of the skipped ones.

    :param job_name: The job's name, in an earlier run.
    :param skipped_names: The skipped jobs' names, from :func:`skipped_job_names`.
    :returns: Whether it matches one of them.
    """
    for skipped_name in skipped_names:
        pattern = _job_name_pattern(skipped_name)
        if pattern.fullmatch(job_name):
            return True
    return False


def _ran_every_job(jobs: list[dict], skipped_names: list[str]) -> bool:
    """
    Tell whether a run ran every one of the jobs that another run skipped.

    :param jobs: The run's jobs, from :func:`run_jobs`.
    :param skipped_names: The skipped jobs' names.
    :returns: Whether each skipped job passed or failed in this run.
    """
    for skipped_name in skipped_names:
        pattern = _job_name_pattern(skipped_name)
        ran = False
        for job in jobs:
            name_matches = pattern.fullmatch(job.get("name", "")) is not None
            if name_matches and job.get("conclusion") in LOG_JOB_CONCLUSIONS:
                ran = True
        if not ran:
            return False
    return True


def earlier_tested_run(result: Result, skipped_names: list[str]) -> tuple[dict, list[dict]] | None:
    """
    Find the run that last ran the jobs that a run skipped.

    A repository's workflow skips its tests when it finds the code already tested, by an earlier
    run on the same branch: smarter keeps a pass marker per branch in its Actions cache, and
    smarter-console compares a push with the one before it. So the newest earlier run of the same
    workflow and branch that ran every skipped job tested the same code. A pull request's run is
    left out: it tests the merge with its base branch, not the branch.

    :param result: The repository's result, whose run skipped the jobs.
    :param skipped_names: The skipped jobs' names, from :func:`skipped_job_names`.
    :returns: ``(run, its jobs)``, or None when none of the last :data:`EARLIER_RUNS` runs ran them.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    workflow_file = _file_name(result.workflow)
    endpoint = (
        f"repos/{result.repository}/actions/workflows/{workflow_file}/runs"
        f"?branch={result.ref}&status=completed&per_page={EARLIER_RUNS}"
    )
    answer = run_json("gh", "api", endpoint) or {}
    runs = answer.get("workflow_runs") or []
    for run_item in runs:
        if not _is_earlier_branch_run(run_item, result.run_id):
            continue
        jobs = run_jobs(result.repository, run_item["id"])
        if _ran_every_job(jobs, skipped_names):
            return run_item, jobs
    return None


def _is_earlier_branch_run(run_item: dict, run_id: int) -> bool:
    """
    Tell whether a run may have tested a later run's code: it came before, and wasn't a pull request's.

    :param run_item: One of GitHub's ``workflow_runs``.
    :param run_id: The later run's id. Run ids grow with time.
    :returns: Whether it is an earlier run of the branch itself, that completed with a verdict.
    """
    if run_item.get("id", 0) >= run_id:
        return False
    if run_item.get("event") == "pull_request":
        return False
    return run_item.get("conclusion") in LOG_JOB_CONCLUSIONS


def _job_counts(repository: str, job_id: int, sleep) -> Counts | None:
    """
    Read one job's log, and count its tests.

    :param repository: The repository, as ``owner/name``.
    :param job_id: The job's id.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: The counts, or None when the log has none.
    :raises subprocess.CalledProcessError: When ``gh`` fails, or GitHub still answers with a
        server error after :data:`LOG_ATTEMPTS` tries.
    """
    log = _read_job_log(repository, job_id, sleep)
    return count_log(log)


def _read_job_log(repository: str, job_id: int, sleep) -> str:
    """
    Download one job's log, and ask again when GitHub answers with a server error.

    :param repository: The repository, as ``owner/name``.
    :param job_id: The job's id.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: The log.
    :raises subprocess.CalledProcessError: When ``gh`` fails for another reason, or on the last try.
    """
    endpoint = f"repos/{repository}/actions/jobs/{job_id}/logs"
    attempt = 1
    while True:
        try:
            # without this flag, gh refuses to print text with terminal color codes, which logs are full of.
            return run("gh", "api", "--allow-escape-sequences", endpoint, strip=False)
        except subprocess.CalledProcessError as error:
            is_last_attempt = attempt >= LOG_ATTEMPTS
            if is_last_attempt or not _is_server_error(error):
                raise
            print(f"{repository}: job {job_id}'s log: {error_line(error)}, trying again", file=sys.stderr)
        sleep(LOG_RETRY_SECONDS)
        attempt += 1


def _is_server_error(error: subprocess.CalledProcessError) -> bool:
    """
    Tell whether GitHub failed to serve a request, rather than refused it.

    A dropped connection (``stream error``) has no HTTP status, and is worth another try too.

    :param error: The failure that :func:`run` raised.
    :returns: Whether the status is 5xx, or missing.
    """
    status = http_status(error)
    if status == "unknown":
        return True
    return status.startswith("5")


def summary_markdown(results: list[Result], run_url: str) -> str:
    """
    Write the run's job summary.

    One table row per repository that has a test workflow, with its tests run and passed, and a
    total row; then a line that lists the repositories that have none.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The summary, in GitHub Markdown.
    """
    suites = tested(results)
    passed = suites_passed(suites)
    lines = [
        f"## Repository tests: {len(passed)} of {len(suites)} suites passed",
        "",
        "| Repository | Tests run | Passed | Result | Run |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for result in suites:
        text, emoji = _status_words(result.status)
        reason = f" ({result.reason})" if result.reason else ""
        run_link = f"[run]({result.url})" if result.url else ""
        tests_run, tests_passed = _count_cells(result)
        lines.append(f"| {result.repository} | {tests_run} | {tests_passed} | {emoji} {text}{reason} | {run_link} |")
    if suites:
        total_run, total_passed = count_totals(suites)
        total_emoji = _total_emoji(suites)
        total_result = f"{total_emoji} {len(passed)} of {len(suites)} suites passed"
        lines.append(f"| **Total** | **{total_run:,}** | **{total_passed:,}** | {total_result} | |")
    else:
        lines.append("| (no repository has a test workflow) | | | | |")
    if _has_missing_count(suites):
        lines += ["", f"{NO_COUNT}: the run's logs have no test count (`go test` without `-v`, or checks only)."]
    unread = _unread_counts(suites)
    if unread:
        lines += ["", f"{UNREAD_COUNT}: GitHub wouldn't serve the run's logs, so its tests weren't counted: {unread}"]
    earlier = _earlier_count_links(suites, markdown=True)
    if earlier:
        lines += ["", f"Tests skipped as already tested, counted from the run that ran them: {earlier}"]
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

    A headline, then a table in a code block: one row per repository, with its tests run and
    passed and a check mark or a red X, and a total row. Then one line per suite that didn't pass,
    with a link to its run. Repositories without a test workflow are left out: the run's page
    lists them.

    :param results: Every repository's result.
    :param run_url: The workflow run's url.
    :returns: The message.
    """
    results = tested(results)
    would_start = [result for result in results if result.status == "would_start"]
    if would_start:
        return _dry_run_slack_message(results, len(would_start), run_url)
    if not results:
        return f"*Repository tests*\n:warning: No repository has a test workflow.\n<{run_url}|The run>"
    passed = suites_passed(results)
    headline_emoji = _total_emoji(results)
    if len(passed) == len(results):
        headline = f"all {len(results)} test suites passed"
    else:
        headline = f"{len(passed)} of {len(results)} test suites passed"
    lines = [f"{headline_emoji} *Repository tests: {headline}*"]
    lines += _slack_table(results)
    lines += _slack_failures(results)
    if _has_missing_count(results):
        lines.append(f"_{NO_COUNT}: the run's logs have no test count_")
    unread = _unread_counts(results)
    if unread:
        lines.append(f"_{UNREAD_COUNT}: GitHub wouldn't serve the run's logs: {unread}_")
    earlier = _earlier_count_links(results, markdown=False)
    if earlier:
        lines.append(f"_Tests skipped as already tested, counted from the run that ran them: {earlier}_")
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def _slack_table(results: list[Result]) -> list[str]:
    """
    Write the Slack message's table: one row per repository, and a total row, in a code block.

    The emoji go last on each row, so that the numbers line up whatever width Slack gives an emoji.

    :param results: The results of the repositories that have a test workflow.
    :returns: The table's lines, with the code block's fences.
    """
    rows = []
    for result in results:
        name = _short_name(result.repository)
        tests_run, tests_passed = _count_cells(result)
        _, emoji = _status_words(result.status)
        symbol = STATUS_SYMBOL.get(emoji, "❔")
        rows.append((name, tests_run, tests_passed, symbol))
    total_run, total_passed = count_totals(results)
    total_emoji = _total_emoji(results)
    total_symbol = STATUS_SYMBOL[total_emoji]
    total_row = ("Total", f"{total_run:,}", f"{total_passed:,}", total_symbol)
    header = ("Repository", "Tests run", "Passed", "")

    all_rows = [header] + rows + [total_row]
    name_width = max(len(row[0]) for row in all_rows)
    run_width = max(len(row[1]) for row in all_rows)
    passed_width = max(len(row[2]) for row in all_rows)
    rule = "-" * (name_width + run_width + passed_width + 4)

    lines = ["```"]
    for row in [header] + rows:
        lines.append(_table_line(row, name_width, run_width, passed_width))
    lines.append(rule)
    lines.append(_table_line(total_row, name_width, run_width, passed_width))
    lines.append("```")
    return lines


def _table_line(row: tuple[str, str, str, str], name_width: int, run_width: int, passed_width: int) -> str:
    """
    Write one row of the Slack table, the name left-aligned and the numbers right-aligned.

    :param row: ``(name, tests run, passed, emoji)``.
    :param name_width: The name column's width.
    :param run_width: The tests run column's width.
    :param passed_width: The passed column's width.
    :returns: The line, without trailing spaces.
    """
    name, tests_run, tests_passed, symbol = row
    line = f"{name:<{name_width}}  {tests_run:>{run_width}}  {tests_passed:>{passed_width}}  {symbol}"
    return line.rstrip()


def _slack_failures(results: list[Result]) -> list[str]:
    """
    Write one line per suite that didn't pass: what happened, and a link to its run.

    :param results: The results of the repositories that have a test workflow.
    :returns: The lines, none when every suite passed.
    """
    lines = []
    for result in results:
        if result.status == "success":
            continue
        text, emoji = _status_words(result.status)
        reason = f": {result.reason}" if result.reason else ""
        link = f" (<{result.url}|run>)" if result.url else ""
        lines.append(f"{emoji} {result.repository}: {text}{reason}{link}")
    return lines


def suites_passed(results: list[Result]) -> list[Result]:
    """
    Keep the repositories whose suite passed.

    :param results: The results.
    :returns: The results whose status is ``success``.
    """
    return [result for result in results if result.status == "success"]


def count_totals(results: list[Result]) -> tuple[int, int]:
    """
    Add up the tests run and passed of every repository that has a count.

    :param results: The results.
    :returns: ``(tests run, tests passed)``.
    """
    total_run = 0
    total_passed = 0
    for result in results:
        if result.tests_run is not None:
            total_run += result.tests_run
        if result.tests_passed is not None:
            total_passed += result.tests_passed
    return total_run, total_passed


def _total_emoji(results: list[Result]) -> str:
    """
    Choose the total row's emoji: a check mark when every suite passed, a red X otherwise.

    :param results: The results of the repositories that have a test workflow.
    :returns: ``:white_check_mark:`` or ``:x:``.
    """
    passed = suites_passed(results)
    if results and len(passed) == len(results):
        return ":white_check_mark:"
    return ":x:"


def _has_missing_count(results: list[Result]) -> bool:
    """
    Tell whether a repository's row shows :data:`NO_COUNT`.

    :param results: The results of the repositories that have a test workflow.
    :returns: Whether any of them has no test count.
    """
    for result in results:
        is_missing = result.tests_run is None
        if is_missing and not result.count_error:
            return True
    return False


def _unread_counts(results: list[Result]) -> str:
    """
    Name the repositories whose logs couldn't be read, and why, for a report's footnote.

    :param results: The results of the repositories that have a test workflow.
    :returns: E.g. ``cache-results (HTTP 503), smarter-golang (HTTP 503)``, or empty when every log was read.
    """
    names = []
    for result in results:
        if not result.count_error:
            continue
        name = _short_name(result.repository)
        names.append(f"{name} ({result.count_error})")
    return ", ".join(names)


def _earlier_count_links(results: list[Result], markdown: bool) -> str:
    """
    Name the repositories whose skipped tests were counted from an earlier run, with a link to it.

    :param results: The results of the repositories that have a test workflow.
    :param markdown: Write GitHub Markdown links, or else Slack's.
    :returns: E.g. ``smarter (run)``, the run a link, or empty when no count came from an earlier run.
    """
    links = []
    for result in results:
        if not result.counts_from:
            continue
        name = _short_name(result.repository)
        if markdown:
            link = f"[run]({result.counts_from})"
        else:
            link = f"<{result.counts_from}|run>"
        links.append(f"{name} ({link})")
    return ", ".join(links)


def _count_cells(result: Result) -> tuple[str, str]:
    """
    Write a repository's tests run and passed for a report's table.

    :param result: The repository's result.
    :returns: ``(tests run, tests passed)``, each a number, :data:`NO_COUNT`, or
        :data:`UNREAD_COUNT` when the logs couldn't be read.
    """
    if result.count_error:
        return UNREAD_COUNT, UNREAD_COUNT
    tests_run = _count_text(result.tests_run)
    tests_passed = _count_text(result.tests_passed)
    return tests_run, tests_passed


def _count_text(number: int | None) -> str:
    """
    Write a test count for a report, e.g. ``7,379``.

    :param number: The count, or None when it is unknown.
    :returns: The count with thousands separators, or :data:`NO_COUNT`.
    """
    if number is None:
        return NO_COUNT
    return f"{number:,}"


def _short_name(repository: str) -> str:
    """
    Take a repository's name without its owner, to keep the Slack table narrow.

    :param repository: The repository, e.g. ``smarter-sh/smarter-cli``.
    :returns: Its name, e.g. ``smarter-cli``.
    """
    return repository.rsplit("/", 1)[-1]


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
    Start every test workflow, wait for the runs, count their tests, and save the results.

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
    # saved first, so that the report has the conclusions even if counting is cut short.
    save(results, args.output)
    count_all(results)
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

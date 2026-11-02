#!/usr/bin/env python3
"""Merge the open Dependabot pull requests of every smarter-sh repository, and close the ones with merge conflicts.

This is the core of ``.github/workflows/dependabotMerge.yml``, the weekly Dependabot Merge
workflow.

Usage::

    python3 -E -s scripts/dependabot_merge.py run --output results.json --run-url URL [--dry-run]
    python3 -E -s scripts/dependabot_merge.py report --results results.json --run-url URL \\
        --summary "$GITHUB_STEP_SUMMARY" | python3 -I scripts/slack_post.py --channel general

``run`` lists the open pull requests by ``app/dependabot`` in every repository that isn't
archived, and decides each one from what GitHub says about it (``mergeable`` and
``mergeStateStatus``), re-read just before acting, because a merge changes its siblings:

- **conflicts** (``CONFLICTING``) when the run began: closed, with a comment. Dependabot rebases
  its pull requests itself, so one that still conflicts is one it couldn't rebase.
- **conflicts that this run's merges caused**: left open, for Dependabot to rebase.
- **clean** (``CLEAN`` or ``HAS_HOOKS``: no conflict, every check passed): merged, with the
  repository's preferred method (squash, then merge, then rebase).
- **blocked or behind** (required checks still running, a required review, or a base that moved):
  set to merge itself (``--auto``) once GitHub allows it, where the repository allows auto-merge;
  otherwise left open.
- **unstable** (checks failed) or a **draft**: left open. A failing update needs a person.
- **unknown** (GitHub hasn't computed it after a few tries): left open, for the next run.

It never merges with ``--admin``, which would bypass the base branch's protection. ``report``
appends one Markdown table to the run's page, and prints one Slack message that lists every pull
request that was closed or needs a person. It exits with 1 when a ``gh`` call failed.
"""

import argparse
import json
import subprocess  # nosec B404: only for CalledProcessError
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from github_cli import ORGANIZATION, error_line, repositories, run, run_json

AUTHOR = "app/dependabot"
"""The author whose pull requests this job acts on."""

PR_FIELDS = "number,title,url,baseRefName,isDraft,mergeable,mergeStateStatus"
"""The fields that ``gh pr list`` and ``gh pr view`` return for each pull request."""

ACTION_TEXT = {
    "merged": ("merged", ":white_check_mark:"),
    "auto_merge": ("will merge once its required checks pass", ":hourglass_flowing_sand:"),
    "closed": ("closed: merge conflicts", ":no_entry_sign:"),
    "rebasing": ("conflicts after this run's merges; left for Dependabot to rebase", ":arrows_counterclockwise:"),
    "failing": ("left open: its checks fail", ":x:"),
    "blocked": ("left open: blocked, and the repository doesn't allow auto-merge", ":warning:"),
    "draft": ("left open: a draft", ":grey_question:"),
    "unknown": ("left open: GitHub hasn't computed whether it merges", ":grey_question:"),
    "error": ("failed", ":x:"),
}
"""Each :attr:`Result.action`'s words, for the run's page and Slack, and its emoji.

A dry run
reports the action it would take.
"""

QUIET = ("merged", "auto_merge")
"""The actions that a Slack message only counts, rather than lists."""


@dataclass
class Result:
    """One pull request, and what the run did with it."""

    #: The repository, as ``owner/name``.
    repository: str
    #: The pull request's number.
    number: int
    #: Its title, e.g. ``chore(deps): bump pip-tools from 7.5.0 to 7.6.2``. Written by
    #: Dependabot, so it is never put into a shell command.
    title: str
    #: Its url.
    url: str
    #: The branch it merges into.
    base: str
    #: See :data:`ACTION_TEXT`.
    action: str = "unknown"
    #: What ``gh`` said when it failed, or empty.
    error: str = ""


def merge_method(repository: str) -> tuple[str, bool]:
    """
    Pick the repository's merge method, and whether it allows auto-merge.

    :param repository: The repository, as ``owner/name``.
    :returns: ``("--squash" | "--merge" | "--rebase", allows_auto_merge)``.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    settings = run_json("gh", "api", f"repos/{repository}") or {}
    method = "--squash"
    for flag, key in (
        ("--squash", "allow_squash_merge"),
        ("--merge", "allow_merge_commit"),
        ("--rebase", "allow_rebase_merge"),
    ):
        if settings.get(key, flag == "--squash"):
            method = flag
            break
    return method, bool(settings.get("allow_auto_merge"))


def view(repository: str, number: int, sleep=time.sleep, tries: int = 4) -> dict:
    """
    Read a pull request's state, waiting while GitHub computes whether it merges.

    GitHub computes ``mergeable`` lazily, in the background, and answers ``UNKNOWN`` until it has,
    e.g. right after its base branch moved.

    :param repository: The repository, as ``owner/name``.
    :param number: The pull request.
    :param sleep: ``time.sleep``, which the tests replace.
    :param tries: How many times to ask.
    :returns: Its fields (:data:`PR_FIELDS`), the last answer when it stayed ``UNKNOWN``.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    state: dict = {}
    for attempt in range(tries):
        state = run_json("gh", "pr", "view", str(number), "--repo", repository, "--json", PR_FIELDS) or {}
        if state.get("mergeable") != "UNKNOWN" and state.get("mergeStateStatus") != "UNKNOWN":
            return state
        if attempt < tries - 1:
            sleep(5)
    return state


def decide(state: dict, conflicted_at_start: bool, auto_merge: bool) -> str:
    """
    Decide what to do with a pull request, from its state.

    :param state: Its fields (:data:`PR_FIELDS`), as just read.
    :param conflicted_at_start: Whether it already had conflicts when the run listed it.
    :param auto_merge: Whether the repository allows auto-merge.
    :returns: One of :data:`ACTION_TEXT`'s keys, but ``error``.
    """
    mergeable, merge_state = state.get("mergeable"), state.get("mergeStateStatus")
    if state.get("isDraft") or merge_state == "DRAFT":
        return "draft"
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return "closed" if conflicted_at_start else "rebasing"
    if mergeable != "MERGEABLE":
        return "unknown"
    if merge_state in ("CLEAN", "HAS_HOOKS"):
        return "merged"
    if merge_state == "UNSTABLE":
        return "failing"
    if merge_state in ("BLOCKED", "BEHIND"):
        return "auto_merge" if auto_merge else "blocked"
    return "unknown"


def close_comment(run_url: str) -> str:
    """
    Write the comment that a closed pull request gets.

    :param run_url: The workflow run's url.
    :returns: The comment, in GitHub Markdown.
    """
    return (
        "This update has merge conflicts that Dependabot didn't resolve, so The Smarter Project's weekly "
        f"[Dependabot Merge]({run_url}) workflow closed it. Dependabot opens a new pull request for the next version. "
        "To get this one back, comment `@dependabot reopen`, or `@dependabot recreate` to rebuild it from the base."
    )


def act(result: Result, method: str, run_url: str) -> None:
    """
    Merge, close or set a pull request to auto-merge, as its :attr:`Result.action` says.

    **Side effects:** merges or closes the pull request on GitHub.

    :param result: The pull request.
    :param method: The repository's merge flag, e.g. ``--squash``.
    :param run_url: The workflow run's url, which a closing comment links.
    :raises subprocess.CalledProcessError: When ``gh`` fails, e.g. when the base moved since the
        pull request was read.
    """
    number = str(result.number)
    if result.action == "merged":
        run("gh", "pr", "merge", number, "--repo", result.repository, method, "--delete-branch")
    elif result.action == "auto_merge":
        run("gh", "pr", "merge", number, "--repo", result.repository, method, "--auto")
    elif result.action == "closed":
        # Dependabot deletes its own branch; deleting it here would break `@dependabot reopen`.
        run("gh", "pr", "close", number, "--repo", result.repository, "--comment", close_comment(run_url))


def process_repository(repository: str, run_url: str, dry_run: bool, sleep=time.sleep) -> list[Result]:
    """
    Act on every open Dependabot pull request of one repository, oldest first.

    :param repository: The repository, as ``owner/name``.
    :param run_url: The workflow run's url.
    :param dry_run: Decide only: merge and close nothing.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: One result per pull request.
    :raises subprocess.CalledProcessError: When listing the pull requests fails.
    """
    listed = run_json(
        "gh", "pr", "list", "--repo", repository, "--author", AUTHOR, "--state", "open",
        "--limit", "100", "--json", PR_FIELDS,
    ) or []  # fmt: skip
    if not listed:
        return []
    method, auto_merge = merge_method(repository)
    results = []
    for item in sorted(listed, key=lambda pr: pr["number"]):
        result = Result(
            repository, int(item["number"]), item.get("title", ""), item.get("url", ""), item.get("baseRefName", "")
        )
        results.append(result)
        conflicted_at_start = item.get("mergeable") == "CONFLICTING" or item.get("mergeStateStatus") == "DIRTY"
        try:
            state = view(repository, result.number, sleep)
            result.action = decide(state, conflicted_at_start, auto_merge)
            if not dry_run:
                act(result, method, run_url)
        except subprocess.CalledProcessError as error:
            result.error = error_line(error)
            if result.action not in ("merged", "auto_merge", "closed"):
                result.action = "error"
        print(f"{repository}#{result.number}: {result.action}{' (dry run)' if dry_run else ''} {result.error}".rstrip())
    return results


def process_all(organization: str, run_url: str, dry_run: bool, sleep=time.sleep) -> list[Result]:
    """
    Act on every repository's open Dependabot pull requests.

    :param organization: The organization, e.g. ``smarter-sh``.
    :param run_url: The workflow run's url.
    :param dry_run: Decide only.
    :param sleep: ``time.sleep``, which the tests replace.
    :returns: Every pull request's result. A repository whose pull requests couldn't be listed
        gets one result, number 0, with the error.
    """
    results = []
    for repository in repositories(organization):
        name = repository["name"]
        try:
            results += process_repository(name, run_url, dry_run, sleep)
        except subprocess.CalledProcessError as error:
            results.append(Result(name, 0, "", f"https://github.com/{name}/pulls", "", "error", error_line(error)))
    return results


def failed(result: Result) -> bool:
    """Whether a result fails the job's run because a ``gh`` call failed.

    :param result: The repository pull request's result.
    :returns: Whether the result records a ``gh`` error.
    """
    return bool(result.error)


def describe(result: Result, dry_run: bool) -> tuple[str, str]:
    """
    Put a result's action in words.

    :param result: The pull request.
    :param dry_run: Say "would" before the action.
    :returns: ``(words, emoji)``.
    """
    text, emoji = ACTION_TEXT[result.action]
    if result.error:
        text = f"{text}: {result.error}" if result.action == "error" else f"{text} failed: {result.error}"
        emoji = ":x:"
    elif dry_run and result.action in ("merged", "auto_merge", "closed"):
        text = f"would be {text}"
    return text, emoji


def summary_markdown(results: list[Result], run_url: str, dry_run: bool) -> str:
    """
    Write the run's job summary: one table row per pull request.

    :param results: Every pull request's result.
    :param run_url: The workflow run's url.
    :param dry_run: Whether nothing was merged or closed.
    :returns: The summary, in GitHub Markdown.
    """
    lines = [
        "## Dependabot merge" + (" (dry run)" if dry_run else ""),
        "",
        "| Repository | Pull request | Into | Result |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        text, emoji = describe(result, dry_run)
        title = result.title.replace("|", "\\|")
        pull_request = f"[#{result.number}]({result.url}) {title}" if result.number else ""
        lines.append(f"| {result.repository} | {pull_request} | {result.base} | {emoji} {text} |")
    if not results:
        lines.append("| (no open Dependabot pull requests) | | | |")
    lines += ["", f"[The run]({run_url})"]
    return "\n".join(lines) + "\n"


def slack_message(results: list[Result], run_url: str, dry_run: bool) -> str:
    """
    Write the run's one Slack message, in Slack's mrkdwn.

    The pull requests that were closed, or need a person, each on a line with its link; then a
    count of the ones that were merged or will merge.

    :param results: Every pull request's result.
    :param run_url: The workflow run's url.
    :param dry_run: Whether nothing was merged or closed.
    :returns: The message.
    """
    title = "*Dependabot merge*" + (" (dry run)" if dry_run else "")
    if not results:
        return f":white_check_mark: {title}: no open Dependabot pull requests. <{run_url}|The run>"
    lines = [title]
    quiet = [result for result in results if result.action in QUIET and not result.error]
    for result in results:
        if result in quiet:
            continue
        text, emoji = describe(result, dry_run)
        name = result.repository.rsplit("/", 1)[-1]
        link = f"<{result.url}|{name}#{result.number}>" if result.number else f"<{result.url}|{name}>"
        lines.append(f"{emoji} {link} {result.title}: {text}".replace("  ", " "))
    merged = sum(1 for result in quiet if result.action == "merged")
    auto = len(quiet) - merged
    if quiet:
        verb = "would be merged" if dry_run else "merged"
        lines.append(
            f":white_check_mark: {merged} {verb}" + (f", {auto} set to merge once their checks pass" if auto else "")
        )
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def save(results: list[Result], path: str) -> None:
    """
    Write the results as a JSON list.

    :param results: The results.
    :param path: The file to write.
    """
    Path(path).write_text(json.dumps([asdict(result) for result in results], indent=2) + "\n", encoding="utf-8")


def load(path: str) -> list[Result] | None:
    """
    Read the results that :func:`save` wrote.

    :param path: The file.
    :returns: The results, or None when the file is missing or isn't JSON.
    """
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
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

    run_parser = commands.add_parser("run", help="Merge or close every open Dependabot pull request.")
    run_parser.add_argument("--organization", default=ORGANIZATION)
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--run-url", default="")
    run_parser.add_argument("--dry-run", action="store_true")

    report = commands.add_parser("report", help="Summarize the results.")
    report.add_argument("--results", required=True)
    report.add_argument("--run-url", default="")
    report.add_argument("--summary", help="A file to append the Markdown summary to.")
    report.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Run one subcommand.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The exit code: 0, or 1 when ``run`` couldn't list the repositories, or ``report``
        found no results file, or a ``gh`` call that failed.
    """
    args = parse_args(argv)
    if args.command == "run":
        try:
            results = process_all(args.organization, args.run_url, args.dry_run)
        except subprocess.CalledProcessError as error:
            print(f"dependabot_merge.py: couldn't list the repositories: {error_line(error)}", file=sys.stderr)
            return 1
        save(results, args.output)
        return 0
    loaded = load(args.results)
    if loaded is None:
        print(f"dependabot_merge.py: no results in {args.results}", file=sys.stderr)
        return 1
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(summary_markdown(loaded, args.run_url, args.dry_run))
    print(slack_message(loaded, args.run_url, args.dry_run))
    return 1 if any(failed(result) for result in loaded) else 0


if __name__ == "__main__":
    sys.exit(main())

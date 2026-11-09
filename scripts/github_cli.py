#!/usr/bin/env python3
"""Run ``git`` and ``gh`` for the workflows' scripts, and read the smarter-sh organization with them.

Every script that talks to GitHub runs its commands through :func:`run`, so that its tests replace
that one function with a fake, and nothing in them reaches git or GitHub. The scripts that import
this module run with ``python3 -E -s`` rather than ``-I``, because ``-I`` leaves the script's own
folder off ``sys.path``, and with it this module.

It uses only Python's standard library, because the workflows' runners have no virtual environment.
"""

import json
import re
import subprocess  # nosec B404: runs git and gh with fixed arguments

ORGANIZATION = "smarter-sh"
"""The GitHub organization whose repositories the workflows check and maintain."""

PAGE_SIZE = 100
"""How many items :func:`api_pages` asks GitHub for on each page: GitHub's largest page."""


def run(*args: str, check: bool = True, input_text: str | None = None, strip: bool = True) -> str:
    """
    Run a command, without a shell, and return what it printed.

    :param args: The command and its arguments, e.g. ``("gh", "issue", "list", ...)``.
    :param check: Raise when the command fails. ``gh pr ready`` passes False, because it fails
        when the pull request already is in the state it asks for.
    :param input_text: Text to write to the command's standard input, e.g. an issue's body for
        ``--body-file -``. Long Markdown passes through stdin rather than as an argument.
    :param strip: Remove the output's leading and trailing white space, e.g. the newline after a
        url. ``git status --porcelain`` passes False, because its first line can start with a
        space that is part of the line's status, e.g. `` M``.
    :returns: The command's standard output.
    :raises subprocess.CalledProcessError: When ``check`` is True and the command exits with a
        status other than 0. Its ``stderr`` holds the command's error message.
    """
    # errors="replace": a job's log, read with gh api, may hold bytes that aren't UTF-8.
    completed = subprocess.run(  # nosec B603: fixed commands, arguments are never interpreted by a shell
        list(args), check=check, capture_output=True, text=True, errors="replace", input=input_text
    )
    if strip:
        return completed.stdout.strip()
    return completed.stdout


def run_json(*args: str):
    """
    Run a command that prints JSON, e.g. ``gh api`` or ``gh pr list --json``, and parse it.

    :param args: The command and its arguments, as for :func:`run`.
    :returns: The parsed JSON, or None when the command printed nothing (e.g. ``gh api`` on a
        ``204 No Content``).
    :raises subprocess.CalledProcessError: When the command fails.
    :raises ValueError: When it printed something that isn't JSON.
    """
    text = run(*args)
    if not text:
        return None
    return json.loads(text)


def error_line(error: subprocess.CalledProcessError) -> str:
    """
    Pick the line of a failed command's output that says what went wrong.

    ``gh`` and ``git`` print their reason last, e.g. ``HTTP 403: Resource not accessible by
    personal access token``, and neither ever prints the token.

    :param error: The failure that :func:`run` raised.
    :returns: The last non-empty line of its standard error, or the exception's own text.
    """
    stderr = error.stderr or ""
    stripped = stderr.strip()
    lines = stripped.splitlines()
    if not lines:
        return str(error)
    return lines[-1]


def http_status(error: subprocess.CalledProcessError) -> str:
    """
    Pick the HTTP status out of a failed ``gh api`` call, and nothing else of its message.

    The reports that use it are public, so they name the status only, never what GitHub said about
    a repository or a secret.

    :param error: The failure that :func:`run` raised.
    :returns: The status, e.g. ``"403"``, or ``"unknown"`` when the message has none.
    """
    message = error_line(error)
    match = re.search(r"\bHTTP (\d{3})\b", message)
    if match:
        return match.group(1)
    return "unknown"


def api_object(endpoint: str) -> dict:
    """
    Read one object from the GitHub API, e.g. a repository or a file's contents.

    :param endpoint: The endpoint, e.g. ``repos/smarter-sh/smarter``.
    :returns: The object, or ``{}`` when GitHub answered with nothing (``204 No Content``).
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    :raises ValueError: When GitHub answered with something other than an object.
    """
    answer = run_json("gh", "api", endpoint)
    if answer is None:
        return {}
    if not isinstance(answer, dict):
        raise ValueError("GitHub returned an unexpected object response")
    return answer


def api_pages(endpoint: str, key: str | None = None) -> list[dict]:
    """
    Read every page of a GitHub API list, e.g. an organization's repositories.

    :param endpoint: The endpoint, with or without a query, e.g. ``orgs/smarter-sh/repos?type=all``.
    :param key: The key that holds the list in each page's object, e.g. ``secrets``, or None when
        each page is the list itself.
    :returns: The objects of every page, in order.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    :raises ValueError: When a page isn't a list of objects.
    """
    separator = "&" if "?" in endpoint else "?"
    found = []
    page = 1
    while True:
        page_endpoint = f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}"
        answer = run_json("gh", "api", page_endpoint)
        items = _page_items(answer, key)
        found.extend(items)
        # A short page is the last one.
        if len(items) < PAGE_SIZE:
            return found
        page += 1


def _page_items(answer, key: str | None) -> list[dict]:
    """
    Take the list out of one page of a GitHub API list, and check that it holds objects.

    :param answer: The page, as GitHub returned it.
    :param key: The key that holds the list, or None when the page is the list itself.
    :returns: The page's objects.
    :raises ValueError: When the page isn't a list of objects.
    """
    if key is None:
        items = answer
    elif isinstance(answer, dict):
        items = answer.get(key)
    else:
        items = None
    if not isinstance(items, list):
        raise ValueError("GitHub returned an unexpected list response")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("GitHub returned an unexpected list item")
    return items


def repositories(organization: str = ORGANIZATION) -> list[dict]:
    """
    List the organization's repositories that are not archived.

    :param organization: The organization, e.g. ``smarter-sh``.
    :returns: One ``{"name": ..., "default_branch": ...}`` per repository, sorted by name, with
        ``name`` as ``owner/name``.
    :raises subprocess.CalledProcessError: When ``gh`` fails.
    """
    items = run_json(
        "gh", "repo", "list", organization, "--no-archived", "--limit", "1000",
        "--json", "nameWithOwner,defaultBranchRef",
    )  # fmt: skip
    found = []
    for item in items or []:
        branch = item.get("defaultBranchRef") or {}
        default_branch = branch.get("name") or "main"
        found.append({"name": item["nameWithOwner"], "default_branch": default_branch})
    return sorted(found, key=lambda item: item["name"])

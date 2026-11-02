#!/usr/bin/env python3
"""Run ``git`` and ``gh`` for the workflows' scripts, and read the smarter-sh organization with them.

Every script that talks to GitHub runs its commands through :func:`run`, so that its tests replace
that one function with a fake, and nothing in them reaches git or GitHub. The scripts that import
this module run with ``python3 -E -s`` rather than ``-I``, because ``-I`` leaves the script's own
folder off ``sys.path``, and with it this module.

It uses only Python's standard library, because the workflows' runners have no virtual environment.
"""

import json
import subprocess  # nosec B404: runs git and gh with fixed arguments

ORGANIZATION = "smarter-sh"
"""The GitHub organization whose repositories the workflows check and maintain."""


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
    completed = subprocess.run(  # nosec B603: fixed commands, arguments are never interpreted by a shell
        list(args), check=check, capture_output=True, text=True, input=input_text
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
    lines = stderr.strip().splitlines()
    if not lines:
        return str(error)
    return lines[-1]


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

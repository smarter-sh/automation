#!/usr/bin/env python3
"""Check that each integration's latest release loads the latest Smarter Chat, and propose the update if not.

This is the core of ``.github/workflows/embedVersionSync.yml``, the weekly Embed Version Sync
workflow. Smarter Chat's embed script is published to npm as ``@smarter.sh/ui-chat``. Five
repositories of the smarter-sh organization bring it to another ecosystem, and each pins an exact
version of it, in files of its own (see :data:`PINS`):

- ``smarter-wordpress``, the WordPress plugin, which bundles the script in its release zip and
  records its version in ``SMARTER_CHAT_EMBED_VERSION``,
- ``smarter-drupal``, the Drupal module,
- ``smarter-gtm-template``, the Google Tag Manager template,
- ``smarter-shopify``, the Shopify theme app extension,
- ``smarter-docs-site-generators``, the Docusaurus, MkDocs, Sphinx and VitePress plugins,

which load the script from jsDelivr at the pinned version. A pin goes stale whenever
``@smarter.sh/ui-chat`` publishes a release, and nothing tells the repository.

Usage, from the root of the repository's checkout, at ``--base`` (alpha), with every tag::

    python3 -E -s scripts/embed_version_sync.py pin --repository smarter-sh/smarter-drupal --version 0.39.0
    python3 -E -s scripts/embed_version_sync.py status --repository smarter-sh/smarter-drupal \\
        --version 0.39.0 --release-branch main --output result.json
    python3 -E -s scripts/embed_version_sync.py publish --result result.json --base alpha --run-url URL [--dry-run]
    python3 -E -s scripts/embed_version_sync.py report --results DIR --version 0.39.0 --run-url URL \\
        --summary "$GITHUB_STEP_SUMMARY" | python3 -I scripts/slack_post.py --channel general

Before ``status`` runs, the workflow has already updated the checkout's pins to ``--version`` with
the repository's own command (``make embed-version``'s script, or WordPress's
``scripts/embed.sh``), or with ``pin`` for a repository that has none (Drupal). Then:

- ``pin`` rewrites every one of :data:`PINS`'s files to a version.
- ``status`` reads the pins three times: at the repository's latest stable release tag (the
  version its users get), at the checkout's ``HEAD`` (``--base``, before the update), and in the
  working tree (after it), and writes a :class:`Result` as JSON.
- ``publish`` acts on one result, with ``gh`` and ``git``, authorized by ``GH_TOKEN``. A
  repository whose ``--base`` is out of date gets one pull request from the branch :data:`BRANCH`
  into ``--base``, with the update as one ``fix`` commit, so that semantic-release publishes it.
  The pull request starts the repository's own Tests, as any other does. A later run updates the
  same pull request, and closes it once ``--base`` is current.
- ``report`` reads every repository's result, appends one Markdown table to the run's page, and
  prints one Slack message. It exits with 1 when a repository's pins couldn't be read or updated,
  or publishing failed.
"""

import argparse
import json
import os
import re
import subprocess  # nosec B404: only for CalledProcessError
import sys
from dataclasses import dataclass, field
from pathlib import Path

from github_cli import error_line, run
from result_files import load_result, load_result_folder, save_result

PACKAGE = "@smarter.sh/ui-chat"
"""The npm package whose embed script the integrations load."""

BRANCH = "embed-version-sync"
"""The branch of each repository that holds the update commit.

It belongs to this workflow: every run rebuilds it from ``--base`` and force-pushes it, so that the
pull request always holds exactly one commit, the newest update.
"""

BOT_NAME = "github-actions[bot]"
"""The author of the update commit."""

BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
"""The update commit author's email: GitHub's own address for the Actions bot, which links its avatar."""


@dataclass(frozen=True)
class Pin:
    """
    One place where a repository writes the version of the embed script that it loads.

    :param path: The file, relative to the repository's root.
    :param pattern: A regular expression whose first group is the version.
    """

    path: str
    pattern: str

    def read(self, text: str) -> str:
        """
        Read the version out of the file's text.

        :param text: The file's contents.
        :returns: The version, or empty when the pattern isn't found.
        """
        match = re.search(self.pattern, text)
        return match.group(1) if match else ""

    def write(self, text: str, version: str) -> str:
        """
        Replace every version that the pattern finds in the file's text.

        :param text: The file's contents.
        :param version: The new version.
        :returns: The new contents.
        """

        def replace_version(match: re.Match) -> str:
            """Replace the matched old version without changing surrounding text.

            :param match: A match of the pin's regular expression.
            :returns: The complete matched text with its version updated.
            """
            whole_match = match.group(0)
            old_version = match.group(1)
            return whole_match.replace(old_version, version)

        return re.sub(self.pattern, replace_version, text)


PINS: dict[str, tuple[Pin, ...]] = {
    "smarter-sh/smarter-wordpress": (
        Pin("smarter-chat/smarter-chat.php", r"define\( 'SMARTER_CHAT_EMBED_VERSION', '([^']+)' \);"),
    ),
    "smarter-sh/smarter-drupal": (
        Pin("smarter_chat.libraries.yml", r"@smarter\.sh/ui-chat@([^/]+)/dist/embed\.js"),
        Pin("src/EmbedConfig.php", r"const EMBED_VERSION = '([^']+)';"),
    ),
    "smarter-sh/smarter-gtm-template": (Pin("template.tpl", r"data\.version \|\| '([^']+)'"),),
    "smarter-sh/smarter-shopify": (
        Pin("extensions/smarter-chat/snippets/smarter-chat-script-url.liquid", r"@smarter\.sh/ui-chat@([^/]+)/"),
    ),
    "smarter-sh/smarter-docs-site-generators": (Pin("embed.json", r'"version": "([^"]+)"'),),
}
"""Each repository's pins: where it writes the embed script's version.

A repository with several (Drupal) keeps them equal with a unit test. Adding a repository takes
an entry here and one in the workflow's matrix.
"""

VERDICT_TEXT = {
    "current": ("current", ":white_check_mark:"),
    "unreleased": ("updated, waiting for a release", ":hourglass_flowing_sand:"),
    "outdated": ("out of date, pull request open", ":arrows_counterclockwise:"),
    "error": ("couldn't read or update its pins", ":x:"),
}
"""Each :attr:`Result.verdict`'s words, for the run's page and Slack, and its Slack emoji."""


@dataclass
class Result:  # pylint: disable=too-many-instance-attributes
    """
    One repository's check: the embed script's version in its latest release, in ``--base``, and after the update.

    **Verdicts** (:attr:`verdict`):

    - ``current``: the latest release loads the latest version. Nothing to do.
    - ``unreleased``: ``--base`` already loads it, but no release has shipped it yet. A maintainer
      merges ``--base`` into the release branch.
    - ``outdated``: ``--base`` loads an older version. ``publish`` opens the pull request.
    - ``error``: a pin couldn't be read, or the update left one at another version.
    """

    #: The repository, as ``owner/name``.
    repository: str
    #: The latest version of :data:`PACKAGE` on npm, e.g. ``0.39.0``.
    version: str
    #: The repository's latest stable release tag, e.g. ``v0.2.0``, or empty when it has none.
    release_tag: str = ""
    #: The versions of the pins at :attr:`release_tag`, in :data:`PINS`'s order.
    released: list[str] = field(default_factory=list)
    #: The versions of the pins at ``--base`` before the update.
    base: list[str] = field(default_factory=list)
    #: The versions of the pins after the update.
    updated: list[str] = field(default_factory=list)
    #: Every file that the update changed, sorted.
    changed: list[str] = field(default_factory=list)
    #: The url of the pull request that ``publish`` opened or updated, or empty.
    pull_request_url: str = ""
    #: Why reading the pins or publishing failed, or empty.
    error: str = ""

    @property
    def verdict(self) -> str:
        """Return the repository's state, one of :data:`VERDICT_TEXT`'s keys.

        :returns: ``"error"``, ``"current"``, ``"unreleased"``, or ``"outdated"``.
        """
        update_worked = _all_at_version(self.updated, self.version)
        if self.error or not update_worked:
            return "error"
        if _all_at_version(self.released, self.version):
            return "current"
        if _all_at_version(self.base, self.version):
            return "unreleased"
        return "outdated"

    @property
    def shipped(self) -> str:
        """Return the version that the latest release loads, for the report.

        :returns: The unique released pin versions, or ``"none"`` when there are none.
        """
        return _versions_text(self.released)

    @classmethod
    def load(cls, path: str | Path) -> "Result":
        """
        Read a result that :meth:`save` wrote.

        Keys that the class doesn't know are ignored.

        :param path: The result's JSON file.
        :returns: The result.
        """
        return load_result(cls, path)

    def save(self, path: str | Path) -> None:
        """
        Write the result as indented JSON, replacing the file if it exists.

        :param path: The file to write.
        """
        save_result(self, path)


def _all_at_version(versions: list[str], version: str) -> bool:
    """
    Tell whether every pin is at a version.

    :param versions: The pins' versions.
    :param version: The version they should be at.
    :returns: True when there is at least one pin, and every one is at ``version``.
    """
    if not versions:
        return False
    return all(pin == version for pin in versions)


def _versions_text(versions: list[str]) -> str:
    """
    Write the pins' versions for the report, each one once.

    :param versions: The pins' versions.
    :returns: The unique versions, sorted and joined with commas, or ``"none"`` when there are none.
    """
    unique_versions = set(versions)
    sorted_versions = sorted(unique_versions)
    return ", ".join(sorted_versions) or "none"


def _short_name(result: Result) -> str:
    """
    Name a result's repository without its owner.

    :param result: The repository's result.
    :returns: e.g. ``smarter-drupal`` for ``smarter-sh/smarter-drupal``.
    """
    return result.repository.rsplit("/", 1)[-1]


def pins_of(repository: str) -> tuple[Pin, ...]:
    """
    Look up a repository's pins.

    :param repository: The repository, as ``owner/name``.
    :returns: Its pins.
    :raises KeyError: When :data:`PINS` doesn't know it, with a message that says where to add it.
    """
    if repository not in PINS:
        raise KeyError(f"{repository} has no entry in PINS, in scripts/embed_version_sync.py")
    return PINS[repository]


def set_pins(repository: str, version: str, root: Path = Path(".")) -> list[str]:
    """
    Rewrite every pin of a repository's checkout to a version.

    It is the update for a repository that has no command of its own for it, e.g. Drupal.

    :param repository: The repository, as ``owner/name``.
    :param version: The new version.
    :param root: The checkout's root.
    :returns: The files it rewrote.
    """
    written = []
    for pin in pins_of(repository):
        file = root / pin.path
        text = file.read_text(encoding="utf-8")
        new = pin.write(text, version)
        if new != text:
            file.write_text(new, encoding="utf-8")
            written.append(pin.path)
    return written


def latest_release_tag(release_branch: str) -> str:
    """
    Find the repository's latest stable release: the newest tag on its release branch without a prerelease suffix.

    That is ``v0.2.0``, not ``v0.2.0-alpha.2``.

    :param release_branch: The branch that semantic-release publishes stable releases from, e.g.
        ``main``. The checkout has it as ``origin/<branch>``.
    :returns: The tag, or empty when the branch has none.
    """
    return run("git", "describe", "--tags", "--abbrev=0", "--exclude", "*-*", f"origin/{release_branch}", check=False)


def read_pins(repository: str, ref: str = "") -> list[str]:
    """
    Read a repository's pins, at a git ref or in the working tree.

    :param repository: The repository, as ``owner/name``.
    :param ref: A tag or commit, read with ``git show``, or empty for the working tree.
    :returns: Each pin's version, in :data:`PINS`'s order; empty for a pin that wasn't found.
    """
    versions = []
    for pin in pins_of(repository):
        if ref:
            text = run("git", "show", f"{ref}:{pin.path}", check=False)
        else:
            text = _read_working_file(pin.path)
        versions.append(pin.read(text))
    return versions


def _read_working_file(path: str) -> str:
    """
    Read a file of the current directory's checkout.

    :param path: The file, relative to the checkout's root.
    :returns: Its contents, or empty when it doesn't exist.
    """
    file = Path(path)
    if not file.is_file():
        return ""
    return file.read_text(encoding="utf-8")


def changed_files() -> list[str]:
    """
    List the files that the update changed in the current directory's checkout.

    :returns: The changed paths, sorted. Files that git ignores, e.g. WordPress's downloaded
        ``smarter-chat/assets/embed.js``, are not among them.
    """
    porcelain = run("git", "status", "--porcelain", strip=False)
    paths = set()
    for line in porcelain.splitlines():
        if line:
            path = line[3:].strip('"')
            paths.add(path)
    return sorted(paths)


def status(repository: str, version: str, release_branch: str) -> Result:
    """
    Build a repository's result from its checkout.

    :param repository: The repository, as ``owner/name``.
    :param version: The latest version of :data:`PACKAGE`.
    :param release_branch: The branch of its stable releases, e.g. ``main``.
    :returns: The result. A pin that reads empty, at ``--base`` or after the update, sets
        :attr:`Result.error`.
    """
    tag = latest_release_tag(release_branch)
    released = []
    if tag:
        released = read_pins(repository, tag)
    result = Result(
        repository=repository,
        version=version,
        release_tag=tag,
        released=released,
        base=read_pins(repository, "HEAD"),
        updated=read_pins(repository),
        changed=changed_files(),
    )
    result.error = _update_error(result)
    return result


def _update_error(result: Result) -> str:
    """
    Say what is wrong with the pins after the update.

    :param result: The repository's result, with :attr:`Result.updated` read.
    :returns: Which pins have no version, or which versions the update left them at, or empty when every pin is at
        :attr:`Result.version`.
    """
    missing = []
    for pin, value in zip(pins_of(result.repository), result.updated):
        if not value:
            missing.append(pin.path)
    if missing:
        return f"no version found in {', '.join(missing)}"
    left_behind = any(pin != result.version for pin in result.updated)
    if left_behind:
        return f"the update left the pins at {', '.join(result.updated)}"
    return ""


def commit_subject(result: Result) -> str:
    """
    Write the update commit's subject, which is also its pull request's title.

    A ``fix``, so that semantic-release publishes the update to the repository's users.

    :param result: The repository's result.
    :returns: e.g. ``fix(embed): load @smarter.sh/ui-chat 0.39.0``.
    """
    return f"fix(embed): load {PACKAGE} {result.version}"


def pull_request_body(result: Result, run_url: str) -> str:
    """
    Write the update pull request's body.

    :param result: The repository's result.
    :param run_url: The workflow run's url, which the body links.
    :returns: The body, in GitHub Markdown.
    """
    if result.release_tag:
        released = f"{result.release_tag} loads {result.shipped}"
    else:
        released = "There is no release yet"
    file_lines = [f"- `{path}`" for path in result.changed]
    files = "\n".join(file_lines) or "- (none)"
    base_version_text = _versions_text(result.base)
    lines = [
        f"Updates the embed script that this repository loads to the latest `{PACKAGE}` on npm, "
        f"[{result.version}](https://www.npmjs.com/package/{PACKAGE}/v/{result.version}).",
        "",
        f"{released}. This branch loads {base_version_text}.",
        "",
        "Changed files:",
        "",
        files,
        "",
        "Check the package's release notes for a change in the embed script's options, and the tests on this pull "
        "request. A merged `fix` releases the update.",
        "",
        f"Opened by The Smarter Project's weekly [Embed Version Sync]({run_url}) workflow. A later run force-pushes "
        f"the branch `{BRANCH}` with the newest update, and closes this pull request once the base is current.",
    ]
    return "\n".join(lines)


def find_pull_request(repository: str) -> dict | None:
    """
    Find the open pull request that an earlier run opened, from :data:`BRANCH`.

    :param repository: The repository, as ``owner/name``.
    :returns: It, as ``{"number": ..., "url": ...}``, or None.
    """
    text = run(
        "gh", "pr", "list", "--repo", repository, "--state", "open", "--head", BRANCH,
        "--json", "number,url", "--limit", "1",
    )  # fmt: skip
    items = json.loads(text or "[]")
    if not items:
        return None
    return items[0]


def _close_pull_request(result: Result, pull_request: dict, base: str) -> None:
    """
    Close an earlier run's pull request, and delete its branch, because ``base`` is already current.

    :param result: The repository's result.
    :param pull_request: The open pull request, as :func:`find_pull_request` found it.
    :param base: The branch that the pull request merges into, e.g. ``alpha``.
    """
    number = str(pull_request["number"])
    comment = f"{base} already loads {PACKAGE} {result.version}. Closed by the Embed Version Sync workflow."
    run("gh", "pr", "close", number, "--repo", result.repository, "--delete-branch", "--comment", comment)


def _push_update(result: Result, run_url: str) -> None:
    """
    Commit the checkout's update on :data:`BRANCH`, as the Actions bot, and force-push it.

    :param result: The repository's result.
    :param run_url: The workflow run's url, which the commit message links.
    """
    run("git", "config", "user.name", BOT_NAME)
    run("git", "config", "user.email", BOT_EMAIL)
    run("git", "checkout", "-B", BRANCH)
    run("git", "add", "--all")
    subject = commit_subject(result)
    message = f"{subject}\n\nOpened by The Smarter Project's Embed Version Sync workflow: {run_url}"
    run("git", "commit", "--quiet", "--file", "-", input_text=message)
    run("git", "push", "--force", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")


def _open_or_update_pull_request(result: Result, pull_request: dict | None, base: str, run_url: str) -> str:
    """
    Open the update's pull request, or give an earlier run's open one the newest title and body.

    :param result: The repository's result.
    :param pull_request: The open pull request, as :func:`find_pull_request` found it, or None.
    :param base: The branch that the pull request merges into, e.g. ``alpha``.
    :param run_url: The workflow run's url, which the body links.
    :returns: The pull request's url.
    """
    title = commit_subject(result)
    body = pull_request_body(result, run_url)
    if pull_request:
        run(
            "gh", "pr", "edit", str(pull_request["number"]), "--repo", result.repository,
            "--title", title, "--body-file", "-", input_text=body,
        )  # fmt: skip
        return pull_request["url"]
    return run(
        "gh", "pr", "create", "--repo", result.repository, "--base", base, "--head", BRANCH,
        "--title", title, "--body-file", "-", input_text=body,
    )  # fmt: skip


def publish(result: Result, base: str, run_url: str, dry_run: bool) -> Result:
    """
    Open, update or close the repository's pull request, as its result says.

    - A dry run, or a run without ``GH_TOKEN`` (the ``PAT`` secret), writes nothing.
    - An ``error`` writes nothing.
    - A ``current`` or ``unreleased`` repository has the pull request of an earlier run closed.
    - An ``outdated`` one gets the update commit on :data:`BRANCH`, force-pushed, and its pull
      request into ``base``, opened or updated.

    **Side effects:** writes a branch and a pull request to the repository on GitHub, and commits
    in the current directory's checkout.

    :param result: The repository's result. :attr:`Result.pull_request_url` is set when one is
        opened or updated.
    :param base: The branch that the pull request merges into, e.g. ``alpha``.
    :param run_url: The workflow run's url, which the texts link.
    :param dry_run: Report the verdict only.
    :returns: ``result``, updated.
    :raises subprocess.CalledProcessError: When ``git`` or ``gh`` fails.
    """
    verdict = result.verdict
    if dry_run:
        print(f"{result.repository}: {verdict}. Dry run: nothing is written to GitHub.")
        return result
    if not os.environ.get("GH_TOKEN"):
        print(f"::warning::{result.repository}: no GH_TOKEN (the PAT secret), so nothing is written to GitHub.")
        return result
    if verdict == "error":
        return result
    pull_request = find_pull_request(result.repository)
    if verdict != "outdated":
        if pull_request:
            _close_pull_request(result, pull_request, base)
        print(f"{result.repository}: {verdict}.")
        return result
    _push_update(result, run_url)
    result.pull_request_url = _open_or_update_pull_request(result, pull_request, base, run_url)
    print(f"{result.repository}: outdated. Pull request {result.pull_request_url}")
    return result


def _summary_row(result: Result) -> str:
    """
    Write one repository's row of the run's summary table.

    :param result: The repository's result.
    :returns: The row, in GitHub Markdown.
    """
    text, _ = VERDICT_TEXT[result.verdict]
    error = f" ({result.error})" if result.error else ""
    pull_request = f"[pull request]({result.pull_request_url})" if result.pull_request_url else ""
    release_tag = result.release_tag or "none"
    base_versions = _versions_text(result.base)
    return (
        f"| {result.repository} | {text}{error} | {release_tag} | {result.shipped} "
        f"| {base_versions} | {pull_request} |"
    )


def summary_markdown(results: list[Result], version: str, run_url: str) -> str:
    """
    Write the run's job summary: one table row per repository.

    :param results: Every repository's result.
    :param version: The latest version of :data:`PACKAGE`.
    :param run_url: The workflow run's url.
    :returns: The summary, in GitHub Markdown.
    """
    lines = [
        f"## Embed version sync with {PACKAGE} {version}",
        "",
        "| Repository | Status | Latest release | It loads | Base loads | Pull request |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(_summary_row(result))
    if not results:
        lines.append("| (no results) | | | | | |")
    lines += ["", f"[The run]({run_url})"]
    return "\n".join(lines) + "\n"


def _slack_line(result: Result) -> str:
    """
    Write the Slack line of a repository that needs attention.

    :param result: The repository's result.
    :returns: The line, in Slack's mrkdwn.
    """
    text, emoji = VERDICT_TEXT[result.verdict]
    if result.error:
        text = f"{text}; {result.error}"
    link = f" (<{result.pull_request_url}|pull request>)" if result.pull_request_url else ""
    release_tag = result.release_tag or "(none)"
    return f"{emoji} {result.repository}: {text}. Its release {release_tag} loads {result.shipped}{link}"


def slack_message(results: list[Result], version: str, run_url: str) -> str:
    """
    Write the run's one Slack message, in Slack's mrkdwn.

    A clean bill of health when every repository's latest release loads the latest version;
    otherwise one line per repository that needs attention, then the current ones.

    :param results: Every repository's result.
    :param version: The latest version of :data:`PACKAGE`.
    :param run_url: The workflow run's url.
    :returns: The message.
    """
    current = [result for result in results if result.verdict == "current"]
    if results and len(current) == len(results):
        names = ", ".join(_short_name(result) for result in results)
        return (
            f":white_check_mark: *Smarter Chat embed: clean bill of health.* The latest release of every integration "
            f"loads {PACKAGE} {version}: {names}. <{run_url}|The run>"
        )
    lines = [f"*Smarter Chat embed version sync with {PACKAGE} {version}*"]
    for result in results:
        if result not in current:
            lines.append(_slack_line(result))
    if current:
        names = ", ".join(_short_name(result) for result in current)
        lines.append(f":white_check_mark: {len(current)} more current: {names}")
    if not results:
        lines.append(":warning: No repository reported a result.")
    lines.append(f"<{run_url}|The run>")
    return "\n".join(lines)


def load_results(directory: str) -> list[Result]:
    """
    Read every repository's result that the check jobs uploaded.

    A file that isn't one is skipped.

    :param directory: The folder, searched recursively for ``*.json`` files.
    :returns: The results, sorted by repository.
    """
    return load_result_folder(Result, directory)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Read the command line: one of the subcommands ``pin``, ``status``, ``publish`` and ``report``.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The parsed arguments, with the subcommand's name in ``command``.
    """
    module_doc = __doc__ or ""
    description = module_doc.splitlines()[0]
    parser = argparse.ArgumentParser(description=description)
    commands = parser.add_subparsers(dest="command", required=True)

    pin = commands.add_parser("pin", help="Rewrite every pin of the checkout to a version.")
    pin.add_argument("--repository", required=True, help="e.g. smarter-sh/smarter-drupal")
    pin.add_argument("--version", required=True)

    status_parser = commands.add_parser("status", help="Write the checkout's result as JSON.")
    status_parser.add_argument("--repository", required=True, help="e.g. smarter-sh/smarter-drupal")
    status_parser.add_argument("--version", required=True, help=f"The latest version of {PACKAGE}.")
    status_parser.add_argument("--release-branch", default="main", help="The branch of the stable releases.")
    status_parser.add_argument("--output", required=True)

    publish_parser = commands.add_parser("publish", help="Open, update or close the pull request.")
    publish_parser.add_argument("--result", required=True)
    publish_parser.add_argument("--base", default="alpha", help="The branch that the pull request merges into.")
    publish_parser.add_argument("--run-url", default="")
    publish_parser.add_argument("--dry-run", action="store_true")

    report = commands.add_parser("report", help="Summarize every repository's result.")
    report.add_argument("--results", required=True, help="A directory holding the result files.")
    report.add_argument("--version", required=True)
    report.add_argument("--run-url", default="")
    report.add_argument("--summary", help="A file to append the Markdown summary to.")
    return parser.parse_args(argv)


def _run_pin(args: argparse.Namespace) -> int:
    """
    Run ``pin``: rewrite every pin of the checkout to ``--version``.

    :param args: The parsed arguments.
    :returns: The exit code: 0, or 1 when the repository is unknown or a file couldn't be rewritten.
    """
    try:
        written = set_pins(args.repository, args.version)
    except (KeyError, OSError) as error:
        print(f"embed_version_sync.py: {error}", file=sys.stderr)
        return 1
    files = ", ".join(written) or "no file (already there)"
    print(f"{args.repository}: {PACKAGE} {args.version} in {files}.")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """
    Run ``status``: write the checkout's result to ``--output``.

    An unknown repository's result is saved with its error.

    :param args: The parsed arguments.
    :returns: The exit code: 0.
    """
    try:
        result = status(args.repository, args.version, args.release_branch)
    except KeyError as error:
        message = str(error).strip("'\"")
        result = Result(repository=args.repository, version=args.version, error=message)
    result.save(args.output)
    release_tag = result.release_tag or "(none)"
    print(f"{result.repository}: {result.verdict}. Its release {release_tag} loads {result.shipped}.")
    return 0


def _run_publish(args: argparse.Namespace) -> int:
    """
    Run ``publish``: act on the result in ``--result``, and save it back with the pull request's url or the error.

    :param args: The parsed arguments.
    :returns: The exit code: 0, or 1 when ``git`` or ``gh`` failed.
    """
    result = Result.load(args.result)
    try:
        publish(result, args.base, args.run_url, args.dry_run)
    except subprocess.CalledProcessError as error:
        result.error = f"publishing failed: {error_line(error)}"
        print(f"{result.repository}: {result.error}", file=sys.stderr)
        result.save(args.result)
        return 1
    result.save(args.result)
    return 0


def _run_report(args: argparse.Namespace) -> int:
    """
    Run ``report``: append the summary to ``--summary``, and print the Slack message.

    :param args: The parsed arguments.
    :returns: The exit code: 0, or 1 when there is no result, or a repository's verdict is ``error``.
    """
    results = load_results(args.results)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(summary_markdown(results, args.version, args.run_url))
    print(slack_message(results, args.version, args.run_url))
    any_error = any(result.verdict == "error" for result in results)
    if not results or any_error:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Run one subcommand.

    :param argv: The arguments, without the program's name. None reads ``sys.argv``.
    :returns: The exit code: 0, or 1 when ``pin`` or ``publish`` failed, or when ``report`` found
        no result, or a repository whose verdict is ``error``.
    """
    args = parse_args(argv)
    if args.command == "pin":
        return _run_pin(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "publish":
        return _run_publish(args)
    return _run_report(args)


if __name__ == "__main__":
    sys.exit(main())

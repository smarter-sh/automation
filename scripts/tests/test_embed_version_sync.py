"""
Tests of scripts/embed_version_sync.py, which the Embed Version Sync workflow runs in each repository that loads.

Smarter Chat's embed script.

They never run git or gh: ``run`` is replaced by a fake that records each command and answers it.
"""

import io
import json
import os
import subprocess  # nosec B404: only to build a CalledProcessError
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import embed_version_sync as sync

RUN_URL = "https://github.com/smarter-sh/automation/actions/runs/1"

DRUPAL_FILES = {
    "smarter_chat.libraries.yml": (
        "embed:\n  js:\n    https://cdn.jsdelivr.net/npm/@smarter.sh/ui-chat@0.9.0/dist/embed.js:\n"
    ),
    "src/EmbedConfig.php": "<?php\nclass EmbedConfig {\n  public const EMBED_VERSION = '0.9.0';\n}\n",
}


def result(**overrides) -> "sync.Result":
    """A result of smarter-drupal, with the given fields."""
    values = {
        "repository": "smarter-sh/smarter-drupal",
        "version": "0.39.0",
        "release_tag": "v0.1.0",
        "released": ["0.9.0", "0.9.0"],
        "base": ["0.9.0", "0.9.0"],
        "updated": ["0.39.0", "0.39.0"],
        "changed": ["smarter_chat.libraries.yml", "src/EmbedConfig.php"],
    }
    values.update(overrides)
    return sync.Result(**values)


class FakeGitHub:
    """Stands in for run(): records every command, and answers gh's list and create commands."""

    def __init__(self, open_pr: dict | None = None, fail: tuple[str, ...] = ()):
        self.commands: list[tuple[str, ...]] = []
        self.open_pr = open_pr
        self.fail = fail

    def __call__(self, *args: str, check: bool = True, input_text: str | None = None, strip: bool = True) -> str:
        self.commands.append(args)
        if self.fail and args[: len(self.fail)] == self.fail:
            raise subprocess.CalledProcessError(1, list(args), stderr="HTTP 403: Forbidden")
        if args[:3] == ("gh", "pr", "list"):
            return json.dumps([self.open_pr] if self.open_pr else [])
        if args[:3] == ("gh", "pr", "create"):
            return "https://github.com/smarter-sh/smarter-drupal/pull/9"
        return ""

    def ran(self, *prefix: str) -> list[tuple[str, ...]]:
        """The commands that start with prefix."""
        return [command for command in self.commands if command[: len(prefix)] == prefix]


class TestPins(unittest.TestCase):
    """Every repository's pins read their version, and pin rewrites them."""

    def test_every_repository_has_pins(self):
        for repository, pins in sync.PINS.items():
            self.assertTrue(pins, repository)

    def test_read_each_pin(self):
        samples = {
            "smarter-sh/smarter-wordpress": ["define( 'SMARTER_CHAT_EMBED_VERSION', '0.36.10' );"],
            "smarter-sh/smarter-drupal": list(DRUPAL_FILES.values()),
            "smarter-sh/smarter-gtm-template": ["const url = 'x' + encodeUriComponent(data.version || '0.9.0') + 'y';"],
            "smarter-sh/smarter-shopify": ["https://cdn.jsdelivr.net/npm/@smarter.sh/ui-chat@0.9.0/dist/embed.js"],
            "smarter-sh/smarter-docs-site-generators": [
                '{\n  "package": "@smarter.sh/ui-chat",\n  "version": "0.9.0"\n}'
            ],
        }
        self.assertEqual(set(samples), set(sync.PINS))
        for repository, texts in samples.items():
            for pin, text in zip(sync.PINS[repository], texts):
                self.assertRegex(pin.read(text), r"^0\.(9\.0|36\.10)$", repository)
                rewritten = pin.write(text, "0.39.0")
                self.assertEqual(pin.read(rewritten), "0.39.0", repository)

    def test_read_missing(self):
        shopify_pin = sync.PINS["smarter-sh/smarter-shopify"][0]
        self.assertEqual(shopify_pin.read("nothing here"), "")

    def test_unknown_repository(self):
        with self.assertRaises(KeyError):
            sync.pins_of("smarter-sh/nope")

    def test_set_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in DRUPAL_FILES.items():
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text)
            written = sync.set_pins("smarter-sh/smarter-drupal", "0.39.0", root)
            libraries = (root / "smarter_chat.libraries.yml").read_text()
            embed_config = (root / "src/EmbedConfig.php").read_text()
            written_again = sync.set_pins("smarter-sh/smarter-drupal", "0.39.0", root)
        self.assertEqual(written, list(DRUPAL_FILES))
        self.assertIn("ui-chat@0.39.0/dist", libraries)
        self.assertIn("EMBED_VERSION = '0.39.0'", embed_config)
        self.assertEqual(written_again, [])


class TestVerdict(unittest.TestCase):
    """The verdict compares the release, the base and the update with the latest version."""

    def test_outdated(self):
        self.assertEqual(result().verdict, "outdated")

    def test_current(self):
        current = result(released=["0.39.0", "0.39.0"], base=["0.39.0", "0.39.0"])
        self.assertEqual(current.verdict, "current")

    def test_unreleased(self):
        self.assertEqual(result(base=["0.39.0", "0.39.0"]).verdict, "unreleased")

    def test_no_release_yet(self):
        self.assertEqual(result(release_tag="", released=[]).verdict, "outdated")

    def test_error(self):
        self.assertEqual(result(updated=["0.39.0", "0.9.0"]).verdict, "error")
        self.assertEqual(result(error="boom").verdict, "error")
        self.assertEqual(result(updated=[]).verdict, "error")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "r.json"
            result(pull_request_url="u").save(path)
            self.assertEqual(sync.Result.load(path), result(pull_request_url="u"))


class TestStatus(unittest.TestCase):
    """Status() reads the pins at the release tag, at HEAD, and in the working tree."""

    def test_status(self):
        def fake(*args, check=True, input_text=None, strip=True):
            if args[:2] == ("git", "describe"):
                return "v0.1.0"
            if args[:2] == ("git", "show"):
                _, path = args[2].split(":", 1)
                return DRUPAL_FILES[path]
            if args[:2] == ("git", "status"):
                return " M smarter_chat.libraries.yml\n M src/EmbedConfig.php\n"
            return ""

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(sync, "run", fake):
            cwd = os.getcwd()
            os.chdir(directory)
            try:
                for path, text in DRUPAL_FILES.items():
                    file = Path(path)
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(text)
                sync.set_pins("smarter-sh/smarter-drupal", "0.39.0")
                found = sync.status("smarter-sh/smarter-drupal", "0.39.0", "main")
                Path("src/EmbedConfig.php").write_text("nothing")
                missing = sync.status("smarter-sh/smarter-drupal", "0.39.0", "main")
            finally:
                os.chdir(cwd)
        self.assertEqual(found.verdict, "outdated")
        self.assertEqual(found.release_tag, "v0.1.0")
        self.assertEqual(found.changed, ["smarter_chat.libraries.yml", "src/EmbedConfig.php"])
        self.assertEqual(missing.verdict, "error")
        self.assertIn("src/EmbedConfig.php", missing.error)

    def test_left_behind(self):
        with (
            mock.patch.object(sync, "latest_release_tag", return_value=""),
            mock.patch.object(sync, "read_pins", side_effect=[["0.9.0"], ["0.9.0"]]),
            mock.patch.object(sync, "changed_files", return_value=[]),
        ):
            found = sync.status("smarter-sh/smarter-shopify", "0.39.0", "main")
        self.assertEqual(found.released, [])
        self.assertIn("left the pins at 0.9.0", found.error)


class TestPublish(unittest.TestCase):
    """Publish() pushes the branch and opens, updates or closes the pull request."""

    def publish(self, item, fake, token="t", dry_run=False):
        with (
            mock.patch.object(sync, "run", fake),
            mock.patch.dict(os.environ, {"GH_TOKEN": token}),
            redirect_stdout(io.StringIO()),
        ):
            return sync.publish(item, "alpha", RUN_URL, dry_run)

    def test_opens(self):
        fake = FakeGitHub()
        published = self.publish(result(), fake)
        self.assertEqual(published.pull_request_url, "https://github.com/smarter-sh/smarter-drupal/pull/9")
        self.assertTrue(fake.ran("git", "push", "--force", "--quiet", "origin", "HEAD:refs/heads/embed-version-sync"))
        create = fake.ran("gh", "pr", "create")[0]
        self.assertIn("fix(embed): load @smarter.sh/ui-chat 0.39.0", create)
        self.assertIn("alpha", create)

    def test_updates(self):
        fake = FakeGitHub(open_pr={"number": 4, "url": "https://x/4"})
        published = self.publish(result(), fake)
        self.assertEqual(published.pull_request_url, "https://x/4")
        self.assertTrue(fake.ran("gh", "pr", "edit", "4"))
        self.assertFalse(fake.ran("gh", "pr", "create"))

    def test_closes_when_current(self):
        fake = FakeGitHub(open_pr={"number": 4, "url": "https://x/4"})
        self.publish(result(base=["0.39.0", "0.39.0"]), fake)
        self.assertTrue(fake.ran("gh", "pr", "close", "4"))
        self.assertFalse(fake.ran("git", "push"))

    def test_writes_nothing(self):
        for kwargs in ({"dry_run": True}, {"token": ""}):
            fake = FakeGitHub()
            self.publish(result(), fake, **kwargs)
            self.assertEqual(fake.commands, [])
        fake = FakeGitHub()
        self.publish(result(error="x"), fake)
        self.assertEqual(fake.commands, [])

    def test_body(self):
        body = sync.pull_request_body(result(), RUN_URL)
        self.assertIn("v0.1.0 loads 0.9.0", body)
        self.assertIn(RUN_URL, body)
        self.assertNotIn("skip ci", body.lower())


class TestReport(unittest.TestCase):
    """The summary and Slack message cover every repository."""

    def test_clean(self):
        current = [result(released=["0.39.0", "0.39.0"], base=["0.39.0", "0.39.0"])]
        message = sync.slack_message(current, "0.39.0", RUN_URL)
        self.assertIn("clean bill of health", message)

    def test_attention(self):
        items = [
            result(pull_request_url="https://x/9"),
            result(repository="smarter-sh/smarter-shopify", released=["0.39.0"], base=["0.39.0"], updated=["0.39.0"]),
        ]
        message = sync.slack_message(items, "0.39.0", RUN_URL)
        summary = sync.summary_markdown(items, "0.39.0", RUN_URL)
        empty_message = sync.slack_message([], "0.39.0", RUN_URL)
        empty_summary = sync.summary_markdown([], "0.39.0", RUN_URL)
        self.assertIn("smarter-sh/smarter-drupal: out of date", message)
        self.assertIn("<https://x/9|pull request>", message)
        self.assertIn("1 more current: smarter-shopify", message)
        self.assertIn("| smarter-sh/smarter-drupal |", summary)
        self.assertIn("No repository", empty_message)
        self.assertIn("(no results)", empty_summary)


class TestMain(unittest.TestCase):
    """Main() runs each subcommand, and sets the exit code."""

    def test_report_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result().save(folder / "a.json")
            (folder / "junk.json").write_text("not json")
            summary = folder / "summary.md"
            with redirect_stdout(io.StringIO()):
                code = sync.main(["report", "--results", directory, "--version", "0.39.0", "--summary", str(summary)])
            self.assertEqual(code, 0)
            self.assertIn("smarter-drupal", summary.read_text())
            result(repository="smarter-sh/smarter-shopify", error="x").save(folder / "b.json")
            with redirect_stdout(io.StringIO()):
                code_with_error = sync.main(["report", "--results", directory, "--version", "0.39.0"])
            self.assertEqual(code_with_error, 1)
        with tempfile.TemporaryDirectory() as empty, redirect_stdout(io.StringIO()):
            code_without_results = sync.main(["report", "--results", empty, "--version", "0.39.0"])
        self.assertEqual(code_without_results, 1)

    def test_publish_failure_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "r.json"
            result().save(path)
            fake = FakeGitHub(fail=("git", "push"))
            with (
                mock.patch.object(sync, "run", fake),
                mock.patch.dict(os.environ, {"GH_TOKEN": "t"}),
                redirect_stdout(io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                code = sync.main(["publish", "--result", str(path)])
            saved = sync.Result.load(path)
        self.assertEqual(code, 1)
        self.assertEqual(saved.error, "publishing failed: HTTP 403: Forbidden")

    def test_status_unknown_repository(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            path = Path(directory) / "r.json"
            code = sync.main(["status", "--repository", "smarter-sh/nope", "--version", "1.0.0", "--output", str(path)])
            saved = sync.Result.load(path)
        self.assertEqual(code, 0)
        self.assertIn("no entry in PINS", saved.error)

    def test_pin(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            unknown_code = sync.main(["pin", "--repository", "smarter-sh/nope", "--version", "1.0.0"])
        with mock.patch.object(sync, "set_pins", return_value=["a"]), redirect_stdout(io.StringIO()) as out:
            code = sync.main(["pin", "--repository", "smarter-sh/smarter-drupal", "--version", "1.0.0"])
        self.assertEqual(unknown_code, 1)
        self.assertEqual(code, 0)
        self.assertIn("in a", out.getvalue())


if __name__ == "__main__":
    unittest.main()

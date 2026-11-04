"""Test scripts/pod_log_review.py, which runs the pod log review with Claude Code, and summarizes it."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pod_log_review
from pod_log_review import (
    SCRUBBED_ENV,
    Settings,
    claude_command,
    claude_environment,
    prompt,
    run,
    summarize,
    worth_retrying,
)
from pod_log_review_stream import ReviewResult
from tests.pod_log_fixtures import (
    result_event,
    write_call,
    write_log_dir,
    write_stream,
)

OK = result_event(num_turns=9, total_cost_usd=1.2)
OVERLOADED = result_event("API Error: Overloaded", is_error=True, api_error_status=529)
OVER_BUDGET = result_event("", is_error=True, subtype="error_max_budget_usd")


def option_value(command: list[str], option: str) -> str:
    """
    Return the argument that follows an option in a command line.

    :param command: The command line.
    :param option: The option, e.g. ``--settings``.
    :returns: The argument after it.
    """
    position = command.index(option)
    return command[position + 1]


class PodLogReviewTestCase(unittest.TestCase):
    """Give each test a runner folder, a log folder, and the settings that point at them."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "claude").mkdir()
        environ = {
            "ENVIRONMENT": "prod",
            "SMARTER_DIR": str(self.root / "smarter"),
            "PLATFORM_DOMAIN": "platform.example",
            "LOG_DIR": write_log_dir(self.root),
            "SINCE": "25h",
            "MAX_NEW_ISSUES": "5",
            "MAX_COMMENTS": "10",
            "MAX_CLOSED_ISSUES": "5",
            "MAX_PULL_REQUESTS": "2",
            "CLAUDE_MODEL": "claude-opus-5-5",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
        }
        with mock.patch.dict("os.environ", environ, clear=True):
            self.settings = Settings.from_env()


class TestSettings(PodLogReviewTestCase):
    """Test Settings.from_env()."""

    def test_from_env(self):
        self.assertEqual(self.settings.environment, "prod")
        self.assertEqual(self.settings.stream_path, str(self.root / "claude" / "stream.jsonl"))
        self.assertFalse(self.settings.dry_run)

    def test_defaults(self):
        with mock.patch.dict("os.environ", {"PR_BASE": ""}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.pr_base, "alpha")
        self.assertEqual(settings.smarter_dir, os.getcwd())
        self.assertEqual(settings.max_budget_usd, "5")


class TestClaudeEnvironment(unittest.TestCase):
    """Test claude_environment()."""

    def environment(self, **environ: str) -> dict[str, str]:
        """Return claude_environment() for this environment, without its output."""
        with mock.patch.dict("os.environ", environ, clear=True), redirect_stdout(io.StringIO()):
            return claude_environment()

    def test_credentials_are_removed(self):
        env = self.environment(ANTHROPIC_API_KEY="key", PATH="/bin", **dict.fromkeys(SCRUBBED_ENV, "secret"))
        self.assertEqual(sorted(env), ["ANTHROPIC_API_KEY", "PATH"])

    def test_the_subscription_token_wins(self):
        env = self.environment(CLAUDE_CODE_OAUTH_TOKEN="token", ANTHROPIC_API_KEY="key")
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "token"})

    def test_the_api_key_is_the_fallback(self):
        env = self.environment(CLAUDE_CODE_OAUTH_TOKEN="", ANTHROPIC_API_KEY="key")
        self.assertEqual(env, {"ANTHROPIC_API_KEY": "key"})

    def test_no_credential_fails(self):
        with self.assertRaises(SystemExit):
            self.environment()


class TestCommandLine(PodLogReviewTestCase):
    """Test prompt() and claude_command()."""

    def test_prompt(self):
        text = prompt(self.settings)
        self.assertIn("- Deployed release (image tags): v0.40.4", text)
        self.assertIn("- Each log covers: the last 25h", text)
        self.assertIn("- Mode: Act:", text)
        dry_run_settings = replace(self.settings, dry_run=True)
        self.assertIn("- Mode: DRY RUN", prompt(dry_run_settings))
        self.assertIn("- Redactions: none", text)
        self.assertIn(f"following {pod_log_review.INSTRUCTIONS_FILE}. Read that file first.", text)
        self.assertTrue(os.path.isfile(pod_log_review.INSTRUCTIONS_FILE))

    def test_prompt_names_the_redactions(self):
        report = {"total": 3, "kinds": {"email": 2, "token": 1}, "files": {"smarter/a.log": {}, "smarter/b.log": {}}}
        report_file = Path(self.settings.log_dir) / "redactions.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")
        text = prompt(self.settings)
        self.assertIn("- Redactions: 3 (email 2, token 1) in 2 file(s), listed in ", text)
        self.assertIn('Each one is a finding: see "Redactions are leaks".', text)

    def test_prompt_names_the_log_source(self):
        text = prompt(self.settings)
        self.assertIn("- Log source: kubectl: the running pods' logs, from kubectl logs.", text, "no log-source.txt")
        inventory = Path(self.settings.log_dir) / "inventory"
        inventory.mkdir(exist_ok=True)
        source = inventory / "log-source.txt"
        source.write_text("loki: every pod's log of the period.\nperiod: ...\n", encoding="utf-8")
        text = prompt(replace(self.settings, since=""))
        self.assertIn("- Log source: loki: every pod's log of the period. See inventory/log-source.txt.", text)
        self.assertIn("- Each log covers: the last 72 hours, all that Loki keeps", text)
        source.write_text("", encoding="utf-8")
        self.assertIn("- Log source: kubectl:", prompt(self.settings), "an empty file")
        source.write_text("kubectl: Loki could not be read: down\n", encoding="utf-8")
        text = prompt(replace(self.settings, since=""))
        self.assertIn("- Each log covers: the lifetime of the container", text)

    def test_claude_command(self):
        command = claude_command(self.settings, "review")
        self.assertEqual(command[:3], ["claude", "-p", "review"])
        disallowed = option_value(command, "--disallowedTools")
        self.assertEqual(disallowed, f"Read(/{self.root}/claude/**)")
        self.assertTrue(disallowed.startswith("Read(//"), "an absolute path in a permission rule starts with //")
        directories = [command[i + 1] for i, arg in enumerate(command) if arg == "--add-dir"]
        self.assertEqual(directories, [self.settings.log_dir, pod_log_review.PROMPTS_DIR])
        settings_file = option_value(command, "--settings")
        self.assertEqual(settings_file, pod_log_review.PERMISSIONS_FILE)
        self.assertTrue(os.path.isabs(settings_file))
        self.assertTrue(os.path.isfile(settings_file))


class TestRun(PodLogReviewTestCase):
    """Test run(), with Claude Code replaced by scripted streams."""

    def run_review(self, *attempts: list) -> tuple[int, int, str]:
        """
        Run the review, each attempt writing the next scripted stream.

        :param attempts: Each attempt's events, and its exit status last.
        :returns: run()'s exit status, the number of attempts, and what it printed.
        """
        scripted = list(attempts)

        def review_once(settings, command, env):  # pylint: disable=unused-argument
            *events, status = scripted.pop(0)
            write_stream(Path(settings.stream_path), events)
            return status

        output = io.StringIO()
        with (
            mock.patch.object(pod_log_review, "review_once", side_effect=review_once) as attempt,
            mock.patch.object(pod_log_review.time, "sleep"),
            mock.patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "token"}),
            redirect_stdout(output),
        ):
            status = run(self.settings)
        return status, attempt.call_count, output.getvalue()

    def test_success(self):
        status, attempts, _ = self.run_review([OK, 0])
        self.assertEqual((status, attempts), (0, 1))

    def test_a_transient_api_error_is_retried(self):
        status, attempts, output = self.run_review([OVERLOADED, 1], [OK, 0])
        self.assertEqual((status, attempts), (0, 2))
        self.assertIn("api_error_status 529", output)

    def test_a_run_that_died_is_retried(self):
        killed = [{"type": "system"}, '{"type": "assi', 137]
        status, attempts, _ = self.run_review(killed, [OK, 0])
        self.assertEqual((status, attempts), (0, 2))

    def test_only_one_retry(self):
        status, attempts, _ = self.run_review([OVERLOADED, 1], [OVERLOADED, 1])
        self.assertEqual((status, attempts), (1, 2))

    def test_a_run_that_wrote_is_not_retried(self):
        status, attempts, _ = self.run_review([write_call(), OVERLOADED, 1])
        self.assertEqual((status, attempts), (1, 1))

    def test_the_budget_is_not_retried(self):
        status, attempts, _ = self.run_review([OVER_BUDGET, 1])
        self.assertEqual((status, attempts), (1, 1))

    def test_worth_retrying(self):
        self.assertTrue(worth_retrying(None, 0))
        self.assertFalse(worth_retrying(None, 1))
        self.assertTrue(worth_retrying(ReviewResult(is_error=True, api_error_status=503), 0))
        self.assertFalse(worth_retrying(ReviewResult(is_error=True, subtype="error_max_turns"), 0))


class TestReviewOnce(PodLogReviewTestCase):
    """Test review_once()."""

    def test_claude_runs_in_the_smarter_checkout(self):
        with mock.patch.object(pod_log_review.subprocess, "run") as run_claude:
            run_claude.return_value.returncode = 0
            status = pod_log_review.review_once(self.settings, ["claude"], {"PATH": "/bin"})
        self.assertEqual(status, 0)
        self.assertEqual(run_claude.call_args.kwargs["cwd"], self.settings.smarter_dir)


class TestSummarize(PodLogReviewTestCase):
    """Test summarize()."""

    def summarize(self, events: list) -> tuple[int, str, str]:
        """Return summarize()'s exit status, the run's page, and what it printed."""
        write_stream(Path(self.settings.stream_path), events)
        output = io.StringIO()
        with redirect_stdout(output):
            status = summarize(self.settings)
        page = Path(self.settings.github_step_summary).read_text(encoding="utf-8")
        return status, page, output.getvalue()

    def test_success(self):
        status, page, output = self.summarize([OK])
        self.assertEqual(status, 0)
        self.assertIn("# Pod log review: prod", page)
        self.assertIn("**Verdict: healthy**", page)
        self.assertIn("---\nturns: 9, cost: $1.20\n", page)
        self.assertEqual(output, "")

    def test_an_api_error_fails_and_says_why(self):
        status, page, output = self.summarize([OVERLOADED])
        self.assertEqual(status, 1)
        self.assertIn("failed: subtype ?, terminal_reason ?, api_error_status 529", page)
        self.assertIn("Claude Code failed: turns: ?, cost: $?, failed:", output)
        self.assertIn("API Error: Overloaded", output)

    def test_a_run_that_did_not_finish(self):
        status, page, _ = self.summarize([{"type": "system"}])
        self.assertEqual(status, 0)
        self.assertIn("did not finish", page)


if __name__ == "__main__":
    unittest.main()

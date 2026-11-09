"""Tests of scripts/suite_counts.py, which counts the tests in a job's log from each test runner's summary lines."""

import unittest

import suite_counts
from suite_counts import Counts, count_log

STAMP = "2026-11-08T15:09:08.5577633Z "
"""The time stamp GitHub puts in front of every line of a job's log."""


def log(*lines: str) -> str:
    """Build a job's log, with a time stamp on every line."""
    stamped = [STAMP + line for line in lines]
    return "\n".join(stamped) + "\n"


class TestCountLog(unittest.TestCase):
    """Count_log() reads each runner's summary."""

    def test_pytest(self):
        text = log("collected 70 items", "=========== 70 passed in 0.58s ===========")
        self.assertEqual(count_log(text), Counts(run=70, passed=70))

    def test_pytest_failures(self):
        text = log("==== 2 failed, 66 passed, 3 skipped, 1 error, 4 warnings in 1.2s (0:00:01) ====")
        self.assertEqual(count_log(text), Counts(run=69, passed=66))

    def test_pytest_other_banners(self):
        text = log("============ FAILURES ============", "==== short test summary info ====")
        self.assertIsNone(count_log(text))

    def test_unittest(self):
        text = log("Ran 148 tests in 0.928s", "", "OK (skipped=10)")
        self.assertEqual(count_log(text), Counts(run=138, passed=138))

    def test_unittest_plain_ok(self):
        self.assertEqual(count_log(log("Ran 266 tests in 6.8s", "", "OK")), Counts(run=266, passed=266))

    def test_unittest_failed(self):
        text = log("Ran 20 tests in 1s", "FAILED (failures=2, errors=1, skipped=3, expected failures=1)")
        self.assertEqual(count_log(text), Counts(run=16, passed=13))

    def test_unittest_ok_without_ran(self):
        self.assertIsNone(count_log(log("OK")))

    def test_several_summaries_add_up(self):
        text = log("Ran 34 tests in 0.01s", "OK", "Ran 13 tests in 0.001s", "OK")
        self.assertEqual(count_log(text), Counts(run=47, passed=47))

    def test_vitest(self):
        text = log(
            "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m19 passed\x1b[39m\x1b[22m (20)",
            "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m134 passed\x1b[39m\x1b[22m | 10 skipped (144)",
        )
        self.assertEqual(count_log(text), Counts(run=134, passed=134))

    def test_vitest_failed(self):
        self.assertEqual(count_log(log("      Tests  2 failed | 40 passed (42)")), Counts(run=42, passed=40))

    def test_vitest_merged_report_is_skipped(self):
        text = log("npx vitest run --merge-reports --coverage", "      Tests  2356 passed (2356)")
        self.assertIsNone(count_log(text))

    def test_jest(self):
        text = log("Test Suites: 12 passed, 12 total", "Tests:       4 skipped, 146 passed, 150 total")
        self.assertEqual(count_log(text), Counts(run=146, passed=146))

    def test_node(self):
        self.assertEqual(count_log(log("# tests 96", "# pass 95", "# fail 1")), Counts(run=96, passed=95))
        self.assertEqual(count_log(log("ℹ tests 3", "ℹ pass 3", "ℹ fail 0")), Counts(run=3, passed=3))

    def test_mocha(self):
        self.assertEqual(count_log(log("  40 passing (2s)", "  2 failing")), Counts(run=42, passed=40))

    def test_phpunit(self):
        self.assertEqual(count_log(log("OK (26 tests, 68 assertions)")), Counts(run=26, passed=26))

    def test_phpunit_failures(self):
        text = log("FAILURES!", "Tests: 49, Assertions: 131, Failures: 2, Errors: 1, Skipped: 3.")
        self.assertEqual(count_log(text), Counts(run=46, passed=43))

    def test_no_count(self):
        text = log("ok  \tgithub.com/smarter-sh/smarter-golang\t2.343s\tcoverage: 97.0% of statements")
        self.assertIsNone(count_log(text))
        self.assertIsNone(count_log(""))

    def test_clean_line(self):
        cleaned = suite_counts.clean_line("﻿" + STAMP + "\x1b[32mOK\x1b[0m  \n")
        self.assertEqual(cleaned, "OK")


if __name__ == "__main__":
    unittest.main()

"""Test scripts/pod_log/redact.py, which redacts credentials and personal data from downloaded logs."""

import collections
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pod_log.redact import REPORT_NAME, is_expected, locate, main, read_report, redact


class TestPodLogRedact(unittest.TestCase):
    """Test redact() and main()."""

    def test_redact(self):
        counts: collections.Counter = collections.Counter()
        text = redact(
            "user someone@example.com password=hunter22 from 8.8.8.8 via 10.0.0.1 at 14:04:45 on 127.0.0.1",
            counts,
        )
        self.assertNotIn("someone@example.com", text)
        self.assertNotIn("hunter22", text)
        self.assertIn("password=<redacted:secret>", text)
        self.assertIn("<redacted:ip-address>", text)
        self.assertIn("10.0.0.1", text, "a private address is kept")
        self.assertIn("14:04:45", text, "a time is not an address")
        self.assertIn("127.0.0.1", text)
        self.assertEqual(counts, {"email": 1, "secret": 1, "ip-address": 1})

    def test_redact_alloy_markers(self):
        # a log read from Loki was redacted by Alloy: its markers become findings, except <ip>.
        counts: collections.Counter = collections.Counter()
        text = redact(
            "user <email> signed in, Authorization: Bearer <redacted>, password=<redacted>, from <ip>",
            counts,
        )
        self.assertEqual(
            text,
            "user <redacted:email> signed in, Authorization: Bearer <redacted:credential>, "
            "password=<redacted:credential>, from <ip>",
        )
        self.assertEqual(counts, {"email": 1, "credential": 2}, "<ip> is not counted: Alloy replaces every address")

    def test_redact_keeps_traceback_source_lines(self):
        # Python prints each frame's source line in a traceback: code, not a value.
        lines = (
            'File ".../knox/auth.py", line 50, in authenticate\n'
            "    user, auth_token = self.authenticate_credentials(auth[1])\n"
            "                       ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^\n"
            'File ".../smarter/lib/drf/token_authentication.py", line 118, in authenticate_credentials\n'
            "    user, auth_token = super().authenticate_credentials(token)\n"
            "    token = request.headers.get_authorization_header(request)\n"
            "    password = settings.get_database_password()\n"
        )
        counts: collections.Counter = collections.Counter()
        self.assertEqual(redact(lines, counts), lines)
        self.assertEqual(counts, {})

    def test_redact_keeps_secret_names(self):
        # Smarter's receivers describe a Secret resource by its name, never its value.
        line = (
            "INFO - smarter.apps.secret.receivers.secret_accessed() from Secret Secret: openai_api_key, id: 1, "
            "user_profile: pk=1"
        )
        counts: collections.Counter = collections.Counter()
        self.assertEqual(redact(line, counts), line)
        self.assertEqual(counts, {})

    def test_redact_keeps_masked_password_in_url(self):
        # Smarter's SqlConnection logs its connection string with the password already masked.
        line = (
            "smarter.apps.connection.receivers.sql_connection_success() "
            "django.db.backends.mysql://smarter:******@smarter-mariadb:3306/smarter_test_db is READY"
        )
        counts: collections.Counter = collections.Counter()
        self.assertEqual(redact(line, counts), line)
        self.assertEqual(counts, {})

    def test_redact_finds_password_in_url(self):
        counts: collections.Counter = collections.Counter()
        text = redact("mysql://smarter:p4ss@db:3306/smarter and mysql://smarter:p4**@db/", counts)
        self.assertEqual(
            text,
            "mysql://<redacted:password-in-url>@db:3306/smarter and mysql://<redacted:password-in-url>@db/",
        )
        self.assertEqual(counts, {"password-in-url": 2})

    def test_redact_still_finds_values(self):
        counts: collections.Counter = collections.Counter()
        text = redact(
            "secret: Zx81aBcD, then auth_token=9f8e7d6c5b4a, password: 'qwerty123', "
            "Authorization: Bearer 9f8e7d6c5b4a3f2e1d0c",
            counts,
        )
        self.assertEqual(
            text,
            "secret: <redacted:secret>, then auth_token=<redacted:secret>, password: '<redacted:secret>', "
            "Authorization: Bearer <redacted:token>",
        )
        self.assertEqual(counts, {"secret": 3, "token": 1})

    def test_redact_aws_account_id(self):
        # kubectl's inventory holds the account id in the IRSA role's ARN and in every ECR image name
        # (smarter-sh/smarter#981). Only the id is redacted.
        account_id = "123456789012"
        registry = account_id + ".dkr.ecr.us-east-2.amazonaws.com/smarter:v0.44.12"
        lines = (
            "      AWS_ROLE_ARN:  arn:aws:iam::" + account_id + ":role/smarter-irsa\n"
            "    Image:         " + registry + "\n"
            "2026-11-10T21:24:03Z 1 " + registry + "\n"
        )
        counts: collections.Counter = collections.Counter()
        text = redact(lines, counts)
        self.assertNotIn(account_id, text)
        self.assertIn("arn:aws:iam::<redacted:aws-account-id>:role/smarter-irsa", text)
        self.assertIn("Image:         <redacted:aws-account-id>.dkr.ecr.us-east-2.amazonaws.com/smarter:v0.44.12", text)
        self.assertEqual(counts, {"aws-account-id": 3})

    def test_redact_keeps_other_twelve_digit_numbers(self):
        # a 12-digit number that is not in an ARN or an ECR hostname, e.g. a timestamp in ms, stays.
        line = "took 123456789012 ms, and 1234567890123.dkr.ecr.us-east-2.amazonaws.com is not an account id"
        counts: collections.Counter = collections.Counter()
        self.assertEqual(redact(line, counts), line)
        self.assertEqual(counts, {})

    def test_main_rewrites_logs_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "smarter" / "pod.log"
            log.parent.mkdir()
            log.write_text("mail someone@example.com\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertEqual(log.read_text(encoding="utf-8"), "mail <redacted:email>\n")
            printed = output.getvalue().strip()
            first, warning = printed.splitlines()
            self.assertEqual(first, "redacted 1 file(s): email 1")
            self.assertTrue(warning.startswith("::warning::1 redaction(s) in 1 log(s)"))
            self.assertNotIn("someone", output.getvalue())
            report_text = (Path(folder) / REPORT_NAME).read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(
                report,
                {
                    "total": 1,
                    "kinds": {"email": 1},
                    "files": {"smarter/pod.log": {"email": {"count": 1, "lines": [1]}}},
                    "expected": {"total": 0, "kinds": {}, "files": {}},
                },
            )
            report_json = json.dumps(report)
            self.assertNotIn("someone", report_json, "the report never holds a value")
            # a second run reports the placeholders that are already there, the same way.
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertEqual(read_report(folder), report)

    def test_main_reports_nothing_on_clean_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "pod.log").write_text("all is well\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertEqual(output.getvalue().strip(), "redacted 1 file(s): nothing")
            empty = {"total": 0, "kinds": {}, "files": {}}
            self.assertEqual(read_report(folder), {**empty, "expected": empty})

    def test_main_keeps_inventory_account_ids_out_of_the_findings(self):
        # kubectl prints the account id in every ECR image name and the IRSA role's ARN
        # (smarter-sh/smarter#1002): redacted, but expected, so not counted in "total".
        account_id = "123456789012"
        registry = account_id + ".dkr.ecr.us-east-2.amazonaws.com/smarter:v0.45.2"
        with tempfile.TemporaryDirectory() as folder:
            inventory = Path(folder) / "inventory"
            inventory.mkdir()
            images = inventory / "images.txt"
            images.write_text("2026-11-13T22:04:55Z 3 " + registry + "\n", encoding="utf-8")
            describe = inventory / "describe.txt"
            describe.write_text(
                "  Image:  " + registry + "\n  AWS_ROLE_ARN:  arn:aws:iam::" + account_id + ":role/irsa\n",
                encoding="utf-8",
            )
            # the same kind in a pod's log, and another kind in the inventory, are still findings.
            log = Path(folder) / "smarter" / "pod.log"
            log.parent.mkdir()
            log.write_text("assumed arn:aws:iam::" + account_id + ":role/irsa\n", encoding="utf-8")
            (inventory / "events.txt").write_text("by someone@example.com\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertNotIn(account_id, images.read_text(encoding="utf-8"), "expected ids are still redacted")
            self.assertNotIn(account_id, describe.read_text(encoding="utf-8"))
            report = read_report(folder)
            self.assertEqual(report["total"], 2)
            self.assertEqual(report["kinds"], {"aws-account-id": 1, "email": 1})
            self.assertEqual(sorted(report["files"]), ["inventory/events.txt", "smarter/pod.log"])
            expected = report["expected"]
            self.assertEqual(expected["total"], 3)
            self.assertEqual(expected["kinds"], {"aws-account-id": 3})
            self.assertEqual(sorted(expected["files"]), ["inventory/describe.txt", "inventory/images.txt"])
            printed = output.getvalue().splitlines()
            self.assertEqual(printed[0], "redacted 4 file(s): aws-account-id 1, email 1")
            self.assertEqual(printed[1], "expected, not findings: aws-account-id 3")
            self.assertTrue(printed[2].startswith("::warning::2 redaction(s) in 2 log(s)"))

    def test_main_does_not_warn_on_expected_placeholders_only(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Path(folder) / "inventory"
            inventory.mkdir()
            text = "Image: 123456789012.dkr.ecr.us-east-2.amazonaws.com/smarter:v1\n"
            (inventory / "describe.txt").write_text(text, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertNotIn("::warning::", output.getvalue())
            report = read_report(folder)
            self.assertEqual(report["total"], 0, "a run with only expected placeholders can be healthy")
            self.assertEqual(report["expected"]["total"], 1)

    def test_main_expects_account_ids_in_kubelet_events(self):
        # a kubelet Pulled event of an HPA scale-up prints the ECR image name (smarter-sh/smarter#1025).
        account_id = "123456789012"
        registry = account_id + ".dkr.ecr.us-east-2.amazonaws.com/smarter:v0.46.9"
        event = (
            'Normal  Pulled  pod/smarter-worker-abc  Container image "' + registry + '" already present on machine\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            inventory = Path(folder) / "inventory"
            inventory.mkdir()
            events = inventory / "events.txt"
            events.write_text(event, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pod_log/redact.py", folder]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            redacted_event = events.read_text(encoding="utf-8")
            self.assertNotIn(account_id, redacted_event, "the id is still redacted")
            self.assertIn("<redacted:aws-account-id>.dkr.ecr.", redacted_event)
            report = read_report(folder)
            self.assertEqual(report["total"], 0)
            self.assertEqual(
                report["expected"]["files"], {"inventory/events.txt": {"aws-account-id": {"count": 1, "lines": [1]}}}
            )
            self.assertNotIn("::warning::", output.getvalue())

    def test_is_expected(self):
        self.assertTrue(is_expected("inventory/events.txt", "aws-account-id"))
        self.assertTrue(is_expected("inventory/describe.txt", "aws-account-id"))
        self.assertTrue(is_expected("inventory/images.txt", "aws-account-id"))
        self.assertFalse(is_expected("inventory/images.txt", "email"))
        self.assertFalse(is_expected("smarter/pod.log", "aws-account-id"))
        self.assertFalse(is_expected("images.txt", "aws-account-id"))

    def test_locate(self):
        text = "a\n<redacted:email> and <redacted:email>\nb <redacted:token>\n" + "<redacted:email>\n" * 30
        found = locate(text)
        self.assertEqual(found["token"], {"count": 1, "lines": [3]})
        self.assertEqual(found["email"]["count"], 32)
        self.assertEqual(found["email"]["lines"][:2], [2, 4], "a line is listed once")
        self.assertEqual(len(found["email"]["lines"]), 20)
        self.assertEqual(locate("nothing here"), {})

    def test_read_report_without_a_report(self):
        self.assertEqual(read_report(""), {})
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(read_report(folder), {})
            (Path(folder) / REPORT_NAME).write_text("not json", encoding="utf-8")
            self.assertEqual(read_report(folder), {})
            (Path(folder) / REPORT_NAME).write_text("[1]", encoding="utf-8")
            self.assertEqual(read_report(folder), {})

    def test_main_needs_a_folder(self):
        with (
            mock.patch.object(sys, "argv", ["pod_log/redact.py", "/no/such/folder"]),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()

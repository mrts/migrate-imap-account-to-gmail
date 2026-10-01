"""Offline safety tests: no network, credentials, or real mail."""
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("migration", Path(__file__).resolve().parents[1] / "migrate-imap-account-to-gmail.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
DATE = datetime(2001, 2, 3, 4, 5, 6)
BODY = b"Message-ID: <test@example.com>\r\nSubject: Test\r\n\r\nHello\r\n"
SOURCE = {"id": "one", "host": "source.test", "port": 993, "username": "one@example.com",
          "password_env": "SOURCE_PASSWORD", "label": "one@example.com"}
TARGET = {"host": "imap.gmail.com", "port": 993, "username": "target@gmail.com",
          "password_env": "TARGET_PASSWORD", "root": "Migration"}
DEST = "Migration/one@example.com/INBOX"


def message(body=BODY, flags=(b"\\Seen",)):
    return {b"BODY[]": body, b"RFC822.SIZE": len(body), b"FLAGS": flags, b"INTERNALDATE": DATE}


class FakeIMAP:
    def __init__(self, folders=None, listing=None):
        self.folders = folders or {}
        self.listing = listing
        self.validity = 10
        self.current = None
        self.calls = []
        self.fail = None
        self.response = True

    def list_folders(self):
        return self.listing if self.listing is not None else [((), b"/", name) for name in self.folders]

    def select_folder(self, name, readonly=False):
        assert readonly, "Every SELECT must be read-only"
        self.current = name
        self.calls.append(("select", name, readonly))
        return {b"UIDVALIDITY": self.validity, b"EXISTS": len(self.folders[name]),
                b"UIDNEXT": max(self.folders[name], default=0) + 1}

    def search(self, criteria):
        return [uid for uid, data in self.folders[self.current].items()
                if criteria == ["ALL"] or b"\\Deleted" not in data[b"FLAGS"]]

    def fetch(self, uids, fields):
        assert "RFC822" not in fields and "BODY[]" not in fields, "Fetch must use PEEK"
        self.calls.append(("fetch", tuple(uids), tuple(fields)))
        if fields == ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]:
            return {uid: {b"BODY[HEADER.FIELDS (MESSAGE-ID)]":
                          self.folders[self.current][uid][b"BODY[]"].split(b"\r\n\r\n")[0] + b"\r\n\r\n"}
                    for uid in uids if uid in self.folders[self.current]}
        return {uid: {field: value for field, value in self.folders[self.current][uid].items()
                      if field.decode() in fields or (field == b"BODY[]" and "BODY.PEEK[]" in fields)}
                for uid in uids if uid in self.folders[self.current]}

    def folder_exists(self, name):
        return name in self.folders

    def create_folder(self, name):
        self.calls.append(("create", name))
        self.folders[name] = {}

    def append(self, folder, body, flags, date):
        self.calls.append(("append", folder))
        if self.fail == "before":
            raise OSError("secret-password must never be logged")
        uid = max(self.folders[folder], default=0) + 1
        self.folders[folder][uid] = message(body, tuple(flags))
        self.folders[folder][uid][b"INTERNALDATE"] = date
        if self.fail == "after":
            raise OSError("lost acknowledgement")
        if not self.response:
            return b"OK"
        return f"[APPENDUID {self.validity} {uid}] Success".encode()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        events = patch.object(m, "event")
        events.start()
        self.addCleanup(events.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.db = m.Database(self.path / "state.sqlite")
        self.addCleanup(self.db.close)
        self.db.bind(SOURCE, TARGET)
        self.src = FakeIMAP({"INBOX": {1: message(), 2: message(BODY + b"second")}})
        self.dst = FakeIMAP()
        self.args = m.parser().parse_args(["copy", "--delay", "0"])
        self.sleep = patch.object(m.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def report(self):
        return {"folders": [], "uploaded": 0, "already_uploaded": 0, "bytes": 0,
                "verified": 0, "reconciled": 0, "problems": 0, "limited": False}

    def copy(self, report=None):
        report = report if report is not None else self.report()
        m.copy_account(self.src, self.dst, SOURCE, TARGET, self.db, self.args, report)
        return report

    def verify(self, deep=False, uploaded_only=False):
        self.args.deep = deep
        self.args.uploaded_only = uploaded_only
        report = self.report()
        m.verify_account(self.src, self.dst, SOURCE, TARGET, self.db, self.args, report)
        return report

    def test_copy_preserves_bytes_date_flags_and_resume_skips(self):
        self.assertEqual(self.copy()["uploaded"], 2)
        self.assertEqual(self.src.folders["INBOX"], self.dst.folders[DEST])
        report = self.copy()
        self.assertEqual((report["uploaded"], report["already_uploaded"]), (0, 2))
        self.assertEqual(self.verify(deep=True)["problems"], 0)

    def test_spam_trash_localized_special_use_and_descendants_excluded(self):
        src = FakeIMAP(listing=[
            ((), b"/", "INBOX"), ((), b"/", "Spam"), ((), b"/", "Trash/child"),
            ((b"\\Trash",), b"/", "Prügikast"), ((), b"/", "Prügikast/child"),
            ((b"\\Junk",), b"/", "Rämps"), ((b"\\Noselect",), b"/", "Container"),
            ((), b"/", "Sent")])
        included = [name for name, _, reason in m.folders(src, SOURCE, TARGET) if not reason]
        self.assertEqual(included, ["INBOX", "Sent"])

    def test_folder_mapping_keeps_unicode_and_escapes_literal_slash(self):
        src = FakeIMAP(listing=[((), b".", "Töö.Foo/Bar"), ((), b".", "Töö.Foo%2FBar")])
        mapped = [dest for _, dest, _ in m.folders(src, SOURCE, TARGET)]
        self.assertEqual(len(set(mapped)), 2)
        self.assertTrue(mapped[0].startswith("Migration/one@example.com/Töö/"))

    def test_inventory_does_not_fetch_bodies(self):
        report = self.report()
        m.inventory(self.src, SOURCE, TARGET, report)
        self.assertEqual(report["folders"][0]["messages"], 2)
        self.assertEqual(report["folders"][0]["bytes"], len(BODY) * 2 + 6)
        self.assertFalse(any("BODY.PEEK[]" in call[2] for call in self.src.calls if call[0] == "fetch"))

    def test_deleted_messages_excluded_and_unsupported_flags_filtered(self):
        self.src.folders["INBOX"][1][b"FLAGS"] = (b"\\Deleted",)
        self.src.folders["INBOX"][2][b"FLAGS"] = (b"\\Flagged", b"\\Recent", b"custom")
        self.assertEqual(self.copy()["uploaded"], 1)
        self.assertEqual(self.dst.folders[DEST][1][b"FLAGS"], (b"\\Flagged",))

    def test_pilot_limit_is_new_uploads_and_can_verify_only_pilot(self):
        self.args.max_messages = 1
        report = self.copy()
        self.assertEqual(report["uploaded"], 1)
        self.assertTrue(report["limited"])
        self.assertEqual(self.verify(uploaded_only=True)["problems"], 0)
        self.assertGreater(self.verify()["problems"], 0)
        self.assertEqual(self.copy()["uploaded"], 1)

    def test_byte_limit_creates_no_labels(self):
        self.args.max_bytes = len(BODY) - 1
        report = self.copy()
        self.assertTrue(report["limited"])
        self.assertEqual(self.dst.folders, {})
        self.assertEqual(self.db.rows(), [])

    def test_uidvalidity_change_stops_copy(self):
        self.copy()
        self.src.validity = 11
        with self.assertRaisesRegex(m.StopMigration, "UIDVALIDITY"):
            self.copy()
        self.assertEqual(len(self.dst.folders[DEST]), 2)

    def test_account_configuration_cannot_change_silently(self):
        with self.assertRaisesRegex(m.StopMigration, "configuration differs"):
            self.db.bind(SOURCE, {**TARGET, "username": "another@gmail.com"})

    def test_existing_untracked_destination_stops_before_upload(self):
        self.dst.folders[DEST] = {1: message()}
        with self.assertRaisesRegex(m.StopMigration, "untracked"):
            self.copy()
        self.assertEqual(self.db.rows(), [])

    def test_lost_append_response_is_durable_and_reconciles(self):
        self.dst.fail = "after"
        with self.assertRaises(OSError):
            self.copy()
        self.assertEqual(self.db.rows()[0]["status"], "pending")
        second_db = m.Database(self.path / "state.sqlite")
        self.assertEqual(second_db.rows()[0]["status"], "pending")
        second_db.close()
        report = self.report()
        m.reconcile(self.dst, self.db, {"one"}, self.args, report)
        self.assertEqual(report["reconciled"], 1)
        self.dst.fail = None
        self.assertEqual(self.copy()["uploaded"], 1)
        self.assertEqual(len(self.dst.folders[DEST]), 2)

    def test_missing_appenduid_requires_reconciliation(self):
        self.dst.response = False
        with self.assertRaisesRegex(m.StopMigration, "APPENDUID"):
            self.copy()
        self.assertEqual(self.db.rows()[0]["status"], "pending")

    def test_missing_upload_remains_pending_without_explicit_retry(self):
        self.dst.fail = "before"
        with self.assertRaises(OSError):
            self.copy()
        report = self.report()
        m.reconcile(self.dst, self.db, {"one"}, self.args, report)
        self.assertEqual(report["problems"], 1)
        self.assertEqual(self.db.rows()[0]["status"], "pending")
        self.args.retry_missing = True
        m.reconcile(self.dst, self.db, {"one"}, self.args, self.report())
        self.assertEqual(self.db.rows(), [])

    def test_ambiguous_hash_matches_are_not_accepted(self):
        self.dst.fail = "after"
        with self.assertRaises(OSError):
            self.copy()
        self.dst.folders[DEST][2] = message()
        report = self.report()
        m.reconcile(self.dst, self.db, {"one"}, self.args, report)
        self.assertEqual(report["problems"], 1)
        self.assertEqual(self.db.rows()[0]["status"], "pending")

    def test_verifier_detects_missing_extra_flags_and_hash_changes(self):
        self.copy()
        self.dst.folders[DEST][1][b"BODY[]"] = BODY.replace(b"Hello", b"HELLO")
        self.assertEqual(self.verify()["problems"], 0)
        self.assertGreater(self.verify(deep=True)["problems"], 0)
        self.dst.folders[DEST][1][b"FLAGS"] = ()
        self.assertGreater(self.verify()["problems"], 0)
        del self.dst.folders[DEST][1]
        self.dst.folders[DEST][3] = message()
        report = self.verify()
        self.assertGreater(report["problems"], 1)
        self.assertIn(3, report["folders"][0]["unexpected_destination_uids"])

    def test_verifier_detects_destination_validity_and_source_deletions(self):
        self.copy()
        self.dst.validity = 99
        self.assertGreater(self.verify()["problems"], 0)
        self.dst.validity = 10
        del self.src.folders["INBOX"][1]
        self.assertIn("source_records_no_longer_in_scope", self.verify())

    def test_verification_failures_include_ids_searches_and_both_sizes(self):
        self.copy()
        self.dst.folders[DEST][1][b"RFC822.SIZE"] += 10
        self.dst.folders[DEST][1][b"BODY[]"] = BODY.replace(b"test@example.com", b"gmail@example.com")
        report = self.verify()
        failure = report["folders"][0]["failures"][0]
        self.assertEqual(failure["message_id"], "test@example.com")
        self.assertEqual(failure["gmail_message_id"], "gmail@example.com")
        self.assertEqual(failure["gmail_search"],
                         f'label:"{DEST}" rfc822msgid:gmail@example.com')
        self.assertEqual((failure["expected_size"], failure["actual_size"]), (len(BODY), len(BODY) + 10))
        self.assertEqual(failure["target_uid"], 1)
        for client in [self.src, self.dst]:
            headers = [call for call in client.calls if call[0] == "fetch" and
                       call[2] == ("BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]",)]
            self.assertEqual(len(headers), 1)
            self.assertEqual(headers[0][1], (1,))

    def test_successful_verification_does_not_fetch_headers(self):
        self.copy()
        self.assertEqual(self.verify()["problems"], 0)
        for client in [self.src, self.dst]:
            self.assertFalse(any(call[0] == "fetch" and
                                 "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]" in call[2]
                                 for call in client.calls))

    def test_missing_destination_uses_source_message_id_for_search(self):
        self.copy()
        del self.dst.folders[DEST][1]
        failure = self.verify()["folders"][0]["failures"][0]
        self.assertEqual(failure["message_id"], "test@example.com")
        self.assertIsNone(failure["gmail_message_id"])
        self.assertIsNone(failure["actual_size"])
        self.assertIn("rfc822msgid:test@example.com", failure["gmail_search"])

    def test_message_without_id_has_no_invented_search(self):
        self.src.folders["INBOX"][1][b"BODY[]"] = b"Subject: No ID\r\n\r\nHello"
        self.copy()
        self.dst.folders[DEST][1][b"FLAGS"] = ()
        failure = self.verify()["folders"][0]["failures"][0]
        self.assertIsNone(failure["message_id"])
        self.assertIsNone(failure["gmail_message_id"])
        self.assertIsNone(failure["gmail_search"])

    def write_config(self, sources=None):
        path = self.path / "accounts.json"
        path.write_text(json.dumps({"target": TARGET, "sources": sources or [SOURCE]}))
        return path

    def test_single_email_field_supplies_login_id_and_label(self):
        path = self.path / "simple.json"
        path.write_text(json.dumps({
            "target": {"email": "target@gmail.com", "password": ""},
            "sources": [{"host": "source.test", "email": "first@example.com", "password": "direct-secret"}]
        }))
        config = m.load_config(path)
        source = config["sources"][0]
        self.assertEqual([source[key] for key in ("username", "id", "label")], ["first@example.com"] * 3)
        self.assertEqual(config["target"]["username"], "target@gmail.com")
        self.assertEqual(config["target"]["host"], "imap.gmail.com")

    def test_direct_password_used_without_environment_and_takes_precedence(self):
        constructor = Mock()
        client = constructor.return_value
        account = {**SOURCE, "password": "direct-secret"}
        with patch.dict(m.sys.modules, {"imapclient": Mock(IMAPClient=constructor)}), \
                patch.dict(m.os.environ, {"SOURCE_PASSWORD": "environment-secret"}):
            with m.connect(account) as connected:
                self.assertIs(connected.client, client)
        client.login.assert_called_once_with(SOURCE["username"], "direct-secret")
        client.logout.assert_called_once()

    def test_blank_direct_password_fails_before_connecting(self):
        constructor = Mock()
        with patch.dict(m.sys.modules, {"imapclient": Mock(IMAPClient=constructor)}):
            with self.assertRaisesRegex(m.StopMigration, "Set the password in JSON"):
                with m.connect({**SOURCE, "password": ""}):
                    self.fail("Blank password should not connect")
        constructor.assert_not_called()

    def test_password_must_be_a_string(self):
        path = self.path / "invalid.json"
        path.write_text(json.dumps({
            "target": {"email": "target@gmail.com", "password": ""},
            "sources": [{"host": "source.test", "email": "first@example.com", "password": 123}]
        }))
        with self.assertRaisesRegex(m.StopMigration, "password must be a string"):
            m.load_config(path)

    def test_dry_run_never_connects_to_target_or_creates_state(self):
        args = m.parser().parse_args(["copy", "--dry-run", "--config", str(self.write_config()),
                                     "--state", str(self.path / "new.sqlite")])
        connected = []
        @contextmanager
        def connect(account):
            connected.append(account["host"])
            yield self.src
        with patch.object(m, "connect", connect):
            m.run(args, self.report())
        self.assertEqual(connected, [SOURCE["host"]])
        self.assertFalse((self.path / "new.sqlite").exists())

    def test_legacy_pending_blocks_account_without_blind_retry(self):
        self.db.pending(("one", "INBOX", 10, 1), DEST, BODY, (), DATE)
        args = m.parser().parse_args(["copy", "--config", str(self.write_config()),
                                     "--state", str(self.path / "state.sqlite")])
        @contextmanager
        def connect(account):
            yield self.dst if account["host"] == TARGET["host"] else self.src
        report = self.report()
        with patch.object(m, "connect", connect):
            m.run(args, report)
        self.assertEqual(report["problems"], 1)
        self.assertEqual(self.db.rows()[0]["status"], "pending")
        self.assertEqual(self.dst.folders, {})

    def test_multisource_limit_is_global_and_accounts_are_separate(self):
        second = {**SOURCE, "id": "two", "label": "two@example.com", "username": "two@example.com"}
        args = m.parser().parse_args(["copy", "--config", str(self.write_config([SOURCE, second])),
                                     "--state", str(self.path / "multi.sqlite"), "--max-messages", "3"])
        @contextmanager
        def connect(account):
            yield self.dst if account["host"] == TARGET["host"] else self.src
        report = self.report()
        with patch.object(m, "connect", connect):
            m.run(args, report)
        self.assertEqual(report["uploaded"], 3)
        self.assertEqual(len(self.dst.folders[DEST]), 2)
        self.assertEqual(len(self.dst.folders["Migration/two@example.com/INBOX"]), 1)

    def test_main_writes_error_report_without_server_secret(self):
        with patch.object(m, "run", side_effect=OSError("secret-password")):
            result = m.main(["inventory", "--log-dir", str(self.path / "logs")])
        self.assertEqual(result, 1)
        report = next((self.path / "logs").glob("*-report.json")).read_text()
        self.assertNotIn("secret-password", report)
        self.assertIn("incomplete", report)

    def test_imap_abort_logs_server_reason_and_command_context(self):
        import imaplib
        client = Mock()
        client.append.side_effect = imaplib.IMAP4.abort("command: APPEND => socket error: EOF")
        logged = m.LoggedIMAP(client, TARGET)
        with self.assertRaises(imaplib.IMAP4.abort) as caught:
            logged.append(DEST, BODY, (), DATE)
        details = caught.exception._migration_diagnostics
        self.assertEqual(details["operation"], "append")
        self.assertEqual(details["host"], "imap.gmail.com")
        self.assertEqual(details["bytes"], len(BODY))
        self.assertIn("socket error: EOF", details["error"]["message"])
        self.assertIn("session_age_s", details)
        self.assertIn("idle_s", details)
        self.assertTrue(details["error"]["frames"])
        self.assertNotIn("Subject: Test", json.dumps(details))

    def test_connection_failure_preserves_errno_and_diagnostics(self):
        constructor = Mock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
        with patch.dict(m.sys.modules, {"imapclient": Mock(IMAPClient=constructor)}):
            with self.assertRaises(ConnectionResetError) as caught:
                with m.connect({**SOURCE, "password": "private-password"}):
                    self.fail("Connection should fail")
        details = caught.exception._migration_diagnostics
        self.assertEqual(details["operation"], "connect")
        self.assertEqual(details["error"]["errno"], 104)
        self.assertIn("reset by peer", details["error"]["message"])

    def test_login_error_redacts_password_and_cleanup_preserves_original_error(self):
        client = Mock()
        client.login.side_effect = OSError("Rejected password private-password")
        client.logout.side_effect = OSError("logout also failed")
        constructor = Mock(return_value=client)
        with patch.dict(m.sys.modules, {"imapclient": Mock(IMAPClient=constructor)}):
            with self.assertRaises(OSError) as caught:
                with m.connect({**SOURCE, "password": "private-password"}):
                    self.fail("Login should fail")
        details = caught.exception._migration_diagnostics
        self.assertEqual(details["operation"], "login")
        self.assertIn("[redacted]", details["error"]["message"])
        self.assertNotIn("private-password", json.dumps(details))
        self.assertNotIn("logout also failed", json.dumps(details))
        client.shutdown.assert_called_once()

    def test_unexpected_protocol_response_omits_message_payload(self):
        import imaplib
        details = m.error_details(imaplib.IMAP4.abort("unexpected response: b'private mail body'"), True)
        self.assertNotIn("private mail body", json.dumps(details))

    def test_session_counters_and_timing_logged_without_append_body(self):
        client = Mock()
        client.append.return_value = b"[APPENDUID 10 1] OK"
        logged = m.LoggedIMAP(client, TARGET)
        self.assertEqual(logged.append(DEST, BODY, (), DATE), b"[APPENDUID 10 1] OK")
        logged.append(DEST, BODY, (), DATE)
        self.assertEqual(logged.uploads, 2)
        self.assertEqual(logged.uploaded_bytes, 2 * len(BODY))
        records = [call.kwargs for call in m.event.call_args_list]
        self.assertTrue(any(record.get("session_uploads") == 1 for record in records))
        self.assertTrue(any("elapsed_s" in record for record in records))
        self.assertNotIn("Subject: Test", json.dumps(records))

    def test_main_preserves_operation_diagnostics_in_final_report(self):
        error = OSError("socket error: EOF")
        error._migration_diagnostics = {"operation": "append", "host": "imap.gmail.com",
                                         "error": {"type": "OSError", "message": "socket error: EOF"}}
        with patch.object(m, "run", side_effect=error):
            result = m.main(["copy", "--log-dir", str(self.path / "diagnostic-logs")])
        self.assertEqual(result, 1)
        report = json.loads(next((self.path / "diagnostic-logs").glob("*-report.json")).read_text())
        self.assertEqual(report["diagnostics"]["operation"], "append")
        self.assertEqual(report["diagnostics"]["error"]["message"], "socket error: EOF")

    def recover(self, report=None):
        @contextmanager
        def connect(account):
            yield self.dst if account["host"] == TARGET["host"] else self.src
        report = report if report is not None else self.report()
        with patch.object(m, "connect", connect):
            m.copy_with_recovery(SOURCE, TARGET, self.db, self.args, report)
        return report

    def test_reconnect_after_rejected_connection_retries_missing_without_duplicates(self):
        import imaplib
        original = self.dst.append
        attempts = []
        def append(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise imaplib.IMAP4.abort("socket error: EOF")
            return original(*args)
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.dst.folders[DEST]), 2)
        self.assertTrue(all(row["status"] == "uploaded" for row in self.db.rows()))
        self.assertTrue(any(call.args[0] == "retry_safe" for call in m.event.call_args_list))

    def test_lost_acknowledgement_reconciles_and_preserves_pilot_limit(self):
        import imaplib
        self.args.max_messages = 1
        original = self.dst.append
        attempts = []
        def append(*args):
            result = original(*args)
            attempts.append(1)
            if len(attempts) == 1:
                raise imaplib.IMAP4.abort("socket error: EOF")
            return result
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["uploaded"], 1)
        self.assertEqual(report["bytes"], len(BODY))
        self.assertEqual(report["reconciled"], 1)
        self.assertTrue(report["limited"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.dst.folders[DEST]), 1)

    def test_recovery_refuses_changed_uidvalidity_or_unmatched_new_message(self):
        key = ("one", "INBOX", 10, 1)
        self.db.pending(key, DEST, BODY, (), DATE, {b"UIDVALIDITY": 10, b"UIDNEXT": 1})
        self.dst.folders[DEST] = {1: message(BODY.replace(b"Hello", b"HELLO"))}
        recovery = {"reconciled": 0, "problems": 0}
        m.reconcile(self.dst, self.db, {"one"}, self.args, recovery, automatic=True)
        self.assertEqual(recovery["problems"], 1)
        self.assertEqual(self.db.get(key)["status"], "pending")
        self.dst.validity = 99
        self.args.retry_missing = True
        recovery = {"reconciled": 0, "problems": 0}
        m.reconcile(self.dst, self.db, {"one"}, self.args, recovery, automatic=True)
        self.assertEqual(recovery["problems"], 1)
        self.assertEqual(self.db.get(key)["status"], "pending")

    def test_preexisting_identical_message_cannot_claim_uncertain_upload(self):
        key = ("one", "INBOX", 10, 1)
        self.dst.folders[DEST] = {1: message()}
        self.db.pending(key, DEST, BODY, (), DATE, {b"UIDVALIDITY": 10, b"UIDNEXT": 2})
        report = {"reconciled": 0, "problems": 0}
        m.reconcile(self.dst, self.db, {"one"}, self.args, report, automatic=True)
        self.assertIsNone(self.db.get(key))
        self.assertEqual(report["reconciled"], 0)
        self.assertEqual(len(self.dst.folders[DEST]), 1)

    def test_retry_exhaustion_is_bounded_and_backoff_is_capped(self):
        self.args.max_retries = 3
        self.args.retry_delay = 10
        self.args.retry_max_delay = 15
        @contextmanager
        def connect(account):
            if account["host"] == SOURCE["host"]:
                raise TimeoutError("source timed out")
            yield self.dst
        report = self.report()
        with patch.object(m, "connect", connect):
            m.copy_with_recovery(SOURCE, TARGET, self.db, self.args, report)
        self.assertEqual([call.args[0] for call in m.time.sleep.call_args_list], [10, 15, 15])
        self.assertEqual(report["problems"], 1)
        self.assertEqual(report["failed_accounts"][0]["retries"], 3)

    def test_account_failure_does_not_block_other_sources(self):
        second = {**SOURCE, "id": "two", "label": "two@example.com", "username": "two@example.com"}
        args = m.parser().parse_args(["copy", "--config", str(self.write_config([SOURCE, second])),
                                     "--state", str(self.path / "multi.sqlite"), "--max-retries", "0"])
        @contextmanager
        def connect(account):
            if account.get("id") == "one":
                raise TimeoutError("source unavailable")
            yield self.dst if account["host"] == TARGET["host"] else self.src
        report = self.report()
        with patch.object(m, "connect", connect):
            m.run(args, report)
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["failed_accounts"][0]["account"], "one")
        self.assertEqual(len(self.dst.folders["Migration/two@example.com/INBOX"]), 2)

    def test_pending_on_unselected_source_does_not_block_selected_source(self):
        self.db.pending(("other", "INBOX", 10, 1), "Migration/other/INBOX", BODY, (), DATE)
        report = self.recover()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(self.db.get(("other", "INBOX", 10, 1))["status"], "pending")

    def test_target_authentication_failure_stops_without_retry(self):
        import imaplib
        @contextmanager
        def connect(account):
            if account["host"] == TARGET["host"]:
                raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
            yield self.src
        report = self.report()
        with patch.object(m, "connect", connect):
            with self.assertRaises(m.StopMigration):
                m.copy_with_recovery(SOURCE, TARGET, self.db, self.args, report)
        m.time.sleep.assert_not_called()
        self.assertEqual(report["failed_accounts"][0]["retries"], 0)

    def test_transient_failure_classifier_excludes_local_storage_and_tls_certificate_errors(self):
        import imaplib
        import ssl
        self.assertTrue(m.transient_failure(imaplib.IMAP4.abort("EOF")))
        self.assertTrue(m.transient_failure(imaplib.IMAP4.error("[UNAVAILABLE] Try again")))
        self.assertFalse(m.transient_failure(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Bad login")))
        self.assertFalse(m.transient_failure(OSError(28, "No space left on device")))
        self.assertFalse(m.transient_failure(ssl.SSLCertVerificationError("Invalid certificate")))

    def test_retry_wait_remains_interruptible(self):
        @contextmanager
        def connect(account):
            raise TimeoutError("offline")
            yield
        with patch.object(m, "connect", connect), patch.object(m.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.copy_with_recovery(SOURCE, TARGET, self.db, self.args, self.report())

    def test_pending_schema_upgrade_keeps_existing_message_records(self):
        import sqlite3
        path = self.path / "old.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TABLE messages (
                account TEXT, folder TEXT, validity INTEGER, uid INTEGER, destination TEXT,
                sha256 TEXT, size INTEGER, flags TEXT, internaldate TEXT, status TEXT,
                target_validity INTEGER, target_uid INTEGER,
                PRIMARY KEY (account, folder, validity, uid))""")
            connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               ("one", "INBOX", 10, 1, DEST, m.digest(BODY), len(BODY), "[]", DATE.isoformat(), "uploaded", 10, 1))
        upgraded = m.Database(path)
        try:
            row = upgraded.get(("one", "INBOX", 10, 1))
            self.assertEqual(row["status"], "uploaded")
            self.assertIsNone(row["before_uidnext"])
        finally:
            upgraded.close()

    def test_retry_budget_resets_after_successful_upload_progress(self):
        import imaplib
        self.args.max_retries = 1
        original = self.dst.append
        attempts = []
        def append(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise imaplib.IMAP4.abort("EOF before first upload")
            result = original(*args)
            if len(attempts) == 3:
                raise imaplib.IMAP4.abort("EOF after accepted second upload")
            return result
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["problems"], 0)
        self.assertEqual(len(self.dst.folders[DEST]), 2)
        waits = [call for call in m.event.call_args_list if call.args[0] == "retry_wait"]
        self.assertEqual([call.kwargs["attempt"] for call in waits], [1, 1])

    def test_source_fetch_disconnect_reconnects_without_pending_upload(self):
        original = self.src.fetch
        failed = []
        def fetch(uids, fields):
            if "BODY.PEEK[]" in fields and not failed:
                failed.append(1)
                raise ConnectionResetError(104, "Connection reset by peer")
            return original(uids, fields)
        with patch.object(self.src, "fetch", side_effect=fetch):
            report = self.recover()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["problems"], 0)
        self.assertEqual(report["reconciled"], 0)

    def test_target_retry_exhaustion_stops_whole_run(self):
        self.args.max_retries = 1
        @contextmanager
        def connect(account):
            raise TimeoutError("Gmail unavailable")
            yield
        report = self.report()
        with patch.object(m, "connect", connect):
            with self.assertRaises(m.StopMigration):
                m.copy_with_recovery(SOURCE, TARGET, self.db, self.args, report)
        self.assertEqual(report["failed_accounts"][0]["retries"], 1)
        self.assertEqual(report["problems"], 1)

    def test_connection_uses_separate_configurable_connect_and_upload_timeouts(self):
        from types import SimpleNamespace
        constructor = Mock()
        module = Mock(IMAPClient=constructor, SocketTimeout=lambda **values: SimpleNamespace(**values))
        with patch.dict(m.sys.modules, {"imapclient": module}):
            with m.connect({**SOURCE, "password": "secret"}):
                pass
            timeout = constructor.call_args.kwargs["timeout"]
            self.assertEqual((timeout.connect, timeout.read), (60, 600))
            with m.connect({**SOURCE, "password": "secret", "timeout": 1200, "connect_timeout": 30}):
                pass
            timeout = constructor.call_args.kwargs["timeout"]
            self.assertEqual((timeout.connect, timeout.read), (30, 1200))

    def test_cli_timeout_applies_to_all_accounts(self):
        args = m.parser().parse_args(["inventory", "--config", str(self.write_config()),
                                     "--timeout", "1200", "--connect-timeout", "30"])
        seen = []
        @contextmanager
        def connect(account):
            seen.append((account["timeout"], account["connect_timeout"]))
            yield self.src
        with patch.object(m, "connect", connect):
            m.run(args, self.report())
        self.assertEqual(seen, [(1200, 30)])

    def test_large_message_timeout_does_not_strand_later_accounts(self):
        import imaplib
        second = {**SOURCE, "id": "two", "label": "two@example.com", "username": "two@example.com"}
        args = m.parser().parse_args(["copy", "--config", str(self.write_config([SOURCE, second])),
                                     "--state", str(self.path / "multi.sqlite"), "--max-retries", "0"])
        original = self.dst.append
        def append(folder, *values):
            if folder == DEST:
                error = imaplib.IMAP4.abort("socket error: The write operation timed out")
                error._migration_diagnostics = {"host": TARGET["host"], "account": TARGET["username"],
                    "operation": "append", "error": {"message": "socket error: The write operation timed out"}}
                raise error
            return original(folder, *values)
        @contextmanager
        def connect(account):
            yield self.dst if account["host"] == TARGET["host"] else self.src
        report = self.report()
        with patch.object(m, "connect", connect), patch.object(self.dst, "append", side_effect=append):
            m.run(args, report)
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["problems"], 1)
        self.assertEqual(report["failed_accounts"][0]["account"], "one")
        self.assertEqual(len(self.dst.folders["Migration/two@example.com/INBOX"]), 2)
        state = m.Database(self.path / "multi.sqlite")
        try:
            self.assertEqual(state.get(("one", "INBOX", 10, 1))["status"], "pending")
        finally:
            state.close()

    def test_destination_quota_failure_still_stops_whole_run(self):
        import imaplib
        self.args.max_retries = 0
        error = imaplib.IMAP4.error("[OVERQUOTA] Account storage quota exceeded")
        error._migration_diagnostics = {"host": TARGET["host"], "account": TARGET["username"],
            "operation": "append", "error": {"message": "[OVERQUOTA] Account storage quota exceeded"}}
        with patch.object(self.dst, "append", side_effect=error):
            with self.assertRaises(m.StopMigration):
                self.recover()

    def test_selects_destination_once_and_advances_each_upload_baseline(self):
        self.copy()
        selections = [call for call in self.dst.calls if call[0] == "select"]
        self.assertEqual(selections, [("select", DEST, True)])
        rows = sorted(self.db.rows(), key=lambda row: row["uid"])
        self.assertEqual([(row["before_validity"], row["before_uidnext"]) for row in rows], [(10, 1), (10, 2)])
        self.dst.calls.clear()
        self.copy()
        self.assertFalse(any(call[0] == "select" for call in self.dst.calls))

    def test_each_destination_folder_gets_its_own_fresh_baseline(self):
        self.src.folders["Sent"] = {1: message(BODY + b"sent"), 2: message(BODY + b"sent 2")}
        self.copy()
        selections = [call[1] for call in self.dst.calls if call[0] == "select"]
        self.assertEqual(selections, [DEST, "Migration/one@example.com/Sent"])
        sent_rows = sorted((row for row in self.db.rows() if row["folder"] == "Sent"), key=lambda row: row["uid"])
        self.assertEqual([row["before_uidnext"] for row in sent_rows], [1, 2])

    def test_new_copy_attempt_refreshes_baseline_for_existing_folder(self):
        self.copy()
        self.src.folders["INBOX"][3] = message(BODY + b"third")
        self.dst.calls.clear()
        self.copy()
        self.assertEqual([call for call in self.dst.calls if call[0] == "select"], [("select", DEST, True)])
        self.assertEqual(self.db.get(("one", "INBOX", 10, 3))["before_uidnext"], 3)

    def test_lost_second_ack_uses_updated_baseline_and_recovers_without_duplicate(self):
        import imaplib
        original = self.dst.append
        attempts = []
        def append(*args):
            response = original(*args)
            attempts.append(1)
            if len(attempts) == 2:
                raise imaplib.IMAP4.abort("socket error: EOF")
            return response
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(report["reconciled"], 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.dst.folders[DEST]), 2)
        self.assertEqual(self.db.get(("one", "INBOX", 10, 2))["before_uidnext"], 2)
        self.assertEqual(len([call for call in self.dst.calls if call[0] == "select"]), 2)

    def test_reconnect_reads_fresh_state_before_next_unseen_message(self):
        import imaplib
        self.src.folders["INBOX"][3] = message(BODY + b"third")
        original = self.dst.append
        attempts = []
        def append(*args):
            response = original(*args)
            attempts.append(1)
            if len(attempts) == 2:
                raise imaplib.IMAP4.abort("socket error: EOF")
            return response
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["uploaded"], 3)
        self.assertEqual(len(self.dst.folders[DEST]), 3)
        self.assertEqual(self.db.get(("one", "INBOX", 10, 3))["before_uidnext"], 3)
        # Initial selection, fresh reconciliation, fresh post-reconnect copy.
        self.assertEqual(len([call for call in self.dst.calls if call[0] == "select"]), 3)

    def test_inconsistent_appenduid_preserves_pending_record(self):
        self.dst.append = Mock(return_value=b"[APPENDUID 99 1] OK")
        with self.assertRaisesRegex(m.StopMigration, "cached destination state"):
            self.copy()
        self.assertEqual(self.db.get(("one", "INBOX", 10, 1))["status"], "pending")

    def test_unexpected_new_uids_do_not_allow_blind_retry_from_cached_baseline(self):
        import imaplib
        original = self.dst.append
        attempts = []
        def append(*args):
            attempts.append(1)
            if len(attempts) == 2:
                # Simulate another writer allocating a UID after our cache was
                # advanced; an unmatched result must remain uncertain.
                self.dst.folders[DEST][99] = message(BODY + b"unrelated")
                raise imaplib.IMAP4.abort("socket error: EOF")
            return original(*args)
        with patch.object(self.dst, "append", side_effect=append):
            report = self.recover()
        self.assertEqual(report["problems"], 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.db.get(("one", "INBOX", 10, 2))["status"], "pending")


class FakeGmail(FakeIMAP):
    def __init__(self):
        super().__init__({"All Mail": {}, "Spam": {}, "Trash": {}})
        self.next_gmid = 1000

    def capabilities(self):
        return (b"X-GM-EXT-1",)

    def list_folders(self):
        return [(({"All Mail": b"\\All", "Spam": b"\\Junk", "Trash": b"\\Trash"}[name],)
                 if name in {"All Mail", "Spam", "Trash"} else (), b"/", name)
                for name in self.folders]

    def select_folder(self, name, readonly=False):
        self.current = name
        self.calls.append(("select", name, readonly))
        return {b"UIDVALIDITY": self.validity, b"EXISTS": len(self.folders[name]),
                b"UIDNEXT": max(self.folders[name], default=0) + 1}

    def append(self, folder, body, flags, date):
        response = super().append(folder, body, flags, date)
        self.next_gmid += 1
        uid = max(self.folders[folder])
        self.folders[folder][uid][b"X-GM-MSGID"] = self.next_gmid
        return response

    def gmail_search(self, query):
        self.calls.append(("gmail_search", self.current))
        wanted = json.loads(query.split("rfc822msgid:", 1)[1])
        return [uid for uid, data in self.folders[self.current].items()
                if str(m.BytesHeaderParser().parsebytes(data[b"BODY[]"]).get("Message-ID", "")).strip("<>") == wanted]

    def search(self, criteria):
        if criteria[0] == "X-GM-MSGID":
            return [uid for uid, data in self.folders[self.current].items()
                    if data.get(b"X-GM-MSGID") == criteria[1]]
        return super().search(criteria)

    def add_gmail_labels(self, uids, labels, silent=False):
        self.calls.append(("label", tuple(uids), tuple(labels)))
        for uid in uids:
            for label in labels:
                messages = self.folders[label]
                if not any(data[b"X-GM-MSGID"] == self.folders[self.current][uid][b"X-GM-MSGID"]
                           for data in messages.values()):
                    messages[max(messages, default=0) + 1] = dict(self.folders[self.current][uid])


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.state = self.path / "state.sqlite"
        self.db = m.Database(self.state)
        self.addCleanup(self.db.close)
        self.db.bind(SOURCE, TARGET)
        self.src = FakeIMAP({"INBOX": {1: message(), 2: message(
            BODY.replace(b"test@example.com", b"second@example.com") + b"second")}})
        self.gmail = FakeGmail()
        self.config = self.path / "accounts.json"
        self.config.write_text(json.dumps({"target": TARGET, "sources": [SOURCE]}))
        self.audit = self.path / "verify.json"
        self.audit_data = {"command": "verify", "result": "incomplete", "error": None,
                           "destination": {k: TARGET[k] for k in ("host", "username", "root")},
                           "folders": [{"account": SOURCE["id"], "folder": "INBOX", "failures": [
                               {"uid": 1, "problems": ["destination message missing"]}]}]}
        self.audit.write_text(json.dumps(self.audit_data))
        self.args = m.parser().parse_args(["repair", "--config", str(self.config),
                                          "--state", str(self.state), "--verify-report", str(self.audit),
                                          "--delay", "0", "--kib-per-second", "1000000"])
        self.events = patch.object(m, "event")
        self.events.start()
        self.addCleanup(self.events.stop)
        with patch.object(m.time, "sleep"):
            m.copy_account(self.src, self.gmail, SOURCE, TARGET, self.db, self.args, self.report())
        self.original = self.gmail.folders[DEST].pop(1)
        self.gmail.calls.clear()

    def report(self):
        return {"uploaded": 0, "bytes": 0, "already_uploaded": 0, "reconciled": 0,
                "problems": 0, "limited": False}

    def run_repair(self):
        @contextmanager
        def connect(account):
            yield self.gmail if account["host"] == TARGET["host"] else self.src
        report = self.report()
        with patch.object(m, "connect", connect), patch.object(m.time, "sleep"):
            m.run(self.args, report)
        return report

    def test_preview_finds_message_elsewhere_without_mutating_mail_or_ledger(self):
        self.gmail.folders["All Mail"][10] = self.original
        self.gmail.folders["Spam"][11] = self.original  # Same Gmail message in two mailboxes.
        before = self.state.read_bytes()
        report = self.run_repair()
        self.assertEqual(report["repairs"][0]["status"], "found_elsewhere")
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(any(c[0] in {"label", "append", "create"} for c in self.gmail.calls))
        self.assertTrue(all(c[2] for c in self.gmail.calls if c[0] == "select"))
        self.assertEqual({c[1] for c in self.gmail.calls if c[0] == "gmail_search"},
                         {DEST, "All Mail", "Spam", "Trash"})

    def test_apply_restores_label_and_uid_without_duplicate_upload(self):
        self.gmail.folders["All Mail"][10] = self.original
        self.args.apply = True
        report = self.run_repair()
        self.assertEqual(report["repairs"][0]["status"], "restored")
        self.assertEqual(self.db.get(("one", "INBOX", 10, 1))["target_uid"], 3)
        self.assertFalse(any(c[0] == "append" for c in self.gmail.calls))
        backup = m.Database(report["state_backup"], readonly=True)
        try:
            self.assertEqual(backup.get(("one", "INBOX", 10, 1))["target_uid"], 1)
        finally:
            backup.close()
        self.assertEqual(self.run_repair()["repairs"][0]["status"], "already_present")

    def test_absent_preview_then_upload_preserves_date_flags_and_hash(self):
        self.assertEqual(self.run_repair()["repairs"][0]["status"], "confirmed_absent")
        self.args.apply = True
        report = self.run_repair()
        self.assertEqual(report["repairs"][0]["status"], "uploaded")
        self.assertEqual(report["uploaded"], 1)
        data = self.gmail.folders[DEST][3]
        self.assertEqual((data[b"BODY[]"], data[b"FLAGS"], data[b"INTERNALDATE"]),
                         (BODY, (b"\\Seen",), DATE))

    def test_same_message_id_with_changed_body_is_ambiguous_not_uploaded(self):
        self.gmail.folders["All Mail"][10] = {**self.original, b"BODY[]": BODY + b"changed"}
        self.args.apply = True
        report = self.run_repair()
        self.assertEqual(report["repairs"][0]["status"], "ambiguous")
        self.assertEqual(report["problems"], 1)
        self.assertFalse(any(c[0] in {"append", "label"} for c in self.gmail.calls))

    def test_multiple_exact_copies_are_ambiguous(self):
        self.gmail.folders["All Mail"] = {10: self.original,
                                           11: {**self.original, b"X-GM-MSGID": 9000}}
        self.assertEqual(self.run_repair()["repairs"][0]["status"], "ambiguous")

    def test_already_claimed_occurrence_is_not_reassigned_or_relabelled(self):
        self.gmail.folders[DEST][2] = self.original
        self.args.apply = True
        report = self.run_repair()
        self.assertEqual(report["repairs"][0]["status"], "ambiguous")
        self.assertFalse(any(c[0] in {"append", "label"} for c in self.gmail.calls))

    def test_changed_source_and_uidvalidity_block_repairs(self):
        self.src.folders["INBOX"][1][b"BODY[]"] += b"changed"
        self.assertEqual(self.run_repair()["repairs"][0]["status"], "ambiguous")
        self.src.folders["INBOX"][1][b"BODY[]"] = BODY
        self.src.validity = 99
        self.assertEqual(self.run_repair()["repairs"][0]["status"], "ambiguous")

    def test_missing_id_and_incomplete_search_results_never_trigger_upload(self):
        with patch.object(m, "fetch_message_ids", return_value={}):
            self.assertEqual(self.run_repair()["repairs"][0]["status"], "ambiguous")
        with patch.object(self.gmail, "gmail_search", return_value=[999]):
            self.assertEqual(self.run_repair()["repairs"][0]["status"], "ambiguous")

    def test_append_lost_ack_keeps_pending_for_existing_reconciliation(self):
        self.args.apply = True
        original = self.gmail.append
        def append(*args):
            original(*args)
            raise m.imaplib.IMAP4.abort("socket error: EOF")
        with patch.object(self.gmail, "append", side_effect=append):
            with self.assertRaises(m.imaplib.IMAP4.abort):
                self.run_repair()
        row = self.db.get(("one", "INBOX", 10, 1))
        self.assertEqual((row["status"], row["before_uidnext"]), ("pending", 3))
        result = {"reconciled": 0, "problems": 0}
        m.reconcile(self.gmail, self.db, {"one"}, self.args, result, automatic=True)
        self.assertEqual(result, {"reconciled": 1, "problems": 0})
        self.assertEqual(len(self.gmail.folders[DEST]), 2)

    def test_incomplete_report_wrong_destination_and_hidden_mailboxes_are_rejected(self):
        self.audit_data["error"] = "Interrupted"
        self.audit.write_text(json.dumps(self.audit_data))
        with self.assertRaisesRegex(m.StopMigration, "completed full"):
            self.run_repair()
        self.audit_data["error"] = None
        self.audit_data["destination"]["username"] = "different@gmail.com"
        self.audit.write_text(json.dumps(self.audit_data))
        with self.assertRaisesRegex(m.StopMigration, "destination differs"):
            self.run_repair()
        self.audit_data["destination"]["username"] = TARGET["username"]
        self.audit.write_text(json.dumps(self.audit_data))
        del self.gmail.folders["Trash"]
        with self.assertRaisesRegex(m.StopMigration, "All Mail, Spam, and Trash"):
            self.run_repair()


class ParallelMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.sources = [{**SOURCE, "id": str(i), "username": f"{i}@example.com",
                         "label": f"{i}@example.com"} for i in range(3)]
        self.config = self.path / "accounts.json"
        self.config.write_text(json.dumps({"target": TARGET, "sources": self.sources}))
        self.state = self.path / "state.sqlite"
        self.args = m.parser().parse_args([
            "copy", "--config", str(self.config), "--state", str(self.state),
            "--delay", "0", "--kib-per-second", "1000000", "--max-retries", "1",
            "--retry-delay", "0.001"])
        self.report = {"uploaded": 0, "bytes": 0, "already_uploaded": 0,
                       "reconciled": 0, "problems": 0, "limited": False}
        self.mail = {}
        self.clients = []
        self.barrier = threading.Barrier(3, timeout=5)
        self.append_hook = None
        events = patch.object(m, "event")
        events.start()
        self.addCleanup(events.stop)

    @contextmanager
    def connect(self, account):
        if account["host"] != TARGET["host"]:
            yield FakeIMAP({"INBOX": {1: message(), 2: message(BODY + b"second")}})
            return
        client = FakeIMAP()
        client.folders = self.mail
        self.clients.append(client)
        original = client.append
        def append(folder, *values):
            if self.append_hook is not None:
                return self.append_hook(client, original, folder, *values)
            if not client.calls or not any(c[0] == "append" for c in client.calls):
                self.barrier.wait()  # Require three simultaneous account workers.
            return original(folder, *values)
        client.append = append
        yield client

    def run_copy(self):
        with patch.object(m, "connect", self.connect):
            m.run(self.args, self.report)

    def rows(self):
        db = m.Database(self.state)
        try:
            return db.rows()
        finally:
            db.close()

    def test_three_workers_copy_distinct_accounts_and_resume_without_duplicates(self):
        self.run_copy()
        self.assertEqual(self.report["workers"], 3)
        self.assertEqual(self.report["uploaded"], 6)
        self.assertEqual(len(self.rows()), 6)
        self.assertTrue(all(row["status"] == "uploaded" for row in self.rows()))
        self.assertEqual(len({id(c) for c in self.clients}), 3)
        self.assertEqual(sum(c.calls.count(("create", "Migration")) for c in self.clients), 1)
        for source in self.sources:
            folder = f"Migration/{source['label']}/INBOX"
            self.assertEqual(len(self.mail[folder]), 2)
        self.report["uploaded"] = 0
        self.run_copy()
        self.assertEqual(self.report["uploaded"], 0)
        self.assertEqual(self.report["already_uploaded"], 6)
        self.assertEqual(sum(len(v) for v in self.mail.values()), 6)

    def test_lost_ack_recovers_while_other_accounts_upload(self):
        lost = []
        def append(client, original, folder, *values):
            if not any(c[0] == "append" for c in client.calls) and not lost:
                self.barrier.wait()
            response = original(folder, *values)
            if folder == "Migration/0@example.com/INBOX" and not lost:
                lost.append(True)
                raise m.imaplib.IMAP4.abort("socket error: EOF")
            return response
        self.append_hook = append
        self.run_copy()
        self.assertEqual(self.report["uploaded"], 6)
        self.assertEqual(self.report["reconciled"], 1)
        self.assertEqual(self.report["problems"], 0)
        self.assertEqual(sum(len(v) for v in self.mail.values()), 6)
        self.assertTrue(all(row["status"] == "uploaded" for row in self.rows()))

    def test_interrupt_stops_workers_preserves_pending_and_merges_completed_uploads(self):
        ready = threading.Event()
        second = threading.Barrier(3, action=ready.set, timeout=5)
        def append(client, original, folder, *values):
            if not any(c[0] == "append" for c in client.calls):
                self.barrier.wait()
                return original(folder, *values)
            second.wait()
            self.args.copy_control.stopped.wait(5)
            raise m.imaplib.IMAP4.abort("socket error: EOF")
        self.append_hook = append
        def interrupt(futures):
            self.assertTrue(ready.wait(5))
            raise KeyboardInterrupt()
        with patch.object(m, "as_completed", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_copy()
        rows = self.rows()
        self.assertEqual(sum(r["status"] == "pending" for r in rows), 3)
        self.assertEqual(sum(r["status"] == "uploaded" for r in rows), 3)
        self.assertEqual(self.report["uploaded"], 3)
        self.assertEqual(self.report["problems"], 0)
        self.assertFalse(hasattr(self.args, "copy_control"))

    def test_quota_failure_cancels_other_workers_and_records_failure(self):
        def append(client, original, folder, *values):
            self.barrier.wait()
            if folder == "Migration/0@example.com/INBOX":
                error = m.imaplib.IMAP4.error("[OVERQUOTA] storage quota exceeded")
                error._migration_diagnostics = {"host": TARGET["host"],
                    "account": TARGET["username"], "operation": "append",
                    "error": {"message": "[OVERQUOTA] storage quota exceeded"}}
                raise error
            self.args.copy_control.stopped.wait(5)
            raise m.imaplib.IMAP4.abort("socket error: EOF")
        self.append_hook = append
        with self.assertRaises(m.StopMigration):
            self.run_copy()
        self.assertEqual(self.report["problems"], 1)
        self.assertEqual(self.report["failed_accounts"][0]["account"], "0")
        self.assertEqual(self.report["uploaded"], 0)
        self.assertEqual(len(self.rows()), 3)

    def test_worker_count_validation_and_selected_account_cap(self):
        for invalid in [0, -1, 4]:
            self.args.workers = invalid
            with patch.object(m, "connect") as connect:
                with self.assertRaisesRegex(m.StopMigration, "--workers"):
                    m.run(self.args, self.report)
                connect.assert_not_called()
        self.args.workers = 3
        self.args.account = ["0"]
        self.append_hook = lambda client, original, folder, *values: original(folder, *values)
        self.run_copy()
        self.assertEqual(self.report["workers"], 1)
        self.assertEqual(self.report["uploaded"], 2)

    def test_pilot_limits_use_one_worker_and_remain_global(self):
        self.args.max_messages = 3
        self.append_hook = lambda client, original, folder, *values: original(folder, *values)
        self.run_copy()
        self.assertEqual(self.report["workers"], 1)
        self.assertEqual(self.report["uploaded"], 3)
        self.assertEqual(len(self.rows()), 3)

    def test_two_workers_take_next_accounts_from_queue(self):
        self.sources.append({**SOURCE, "id": "3", "username": "3@example.com",
                             "label": "3@example.com"})
        self.config.write_text(json.dumps({"target": TARGET, "sources": self.sources}))
        self.args.workers = 2
        self.barrier = threading.Barrier(2, timeout=5)
        self.run_copy()
        self.assertEqual(self.report["workers"], 2)
        self.assertEqual(self.report["uploaded"], 8)
        self.assertEqual({r["account"] for r in self.rows()}, {"0", "1", "2", "3"})

    def test_stop_wakes_blocked_socket_and_retry_wait(self):
        control = m.CopyControl()
        sock, peer = m.socket.socketpair()
        self.addCleanup(sock.close)
        self.addCleanup(peer.close)
        client = Mock()
        client._imap.sock = sock
        control.register(client)
        received = []
        thread = threading.Thread(target=lambda: received.append(sock.recv(1)), daemon=True)
        thread.start()
        control.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received, [b""])
        self.args.copy_control = control
        with self.assertRaises(m.CopyCancelled):
            m.copy_wait(self.args, 300)


if __name__ == "__main__":
    unittest.main()

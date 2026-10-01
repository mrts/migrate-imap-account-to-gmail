#!/usr/bin/env python3
"""Copy IMAP archives to Gmail. See README.md for the staged workflow."""

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.parser import BytesHeaderParser
import fcntl
import errno
import hashlib
import imaplib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid

LOG = logging.getLogger("migration")
SECRETS = set()
FLAGS = {b"\\Seen", b"\\Answered", b"\\Flagged", b"\\Draft"}
JUNK_NAMES = {"trash", "spam", "junk", "junk e-mail", "junk email", "deleted items",
              "deleted messages", "bin"}


class StopMigration(Exception):
    """An actionable, safe-to-log failure."""


class CopyCancelled(Exception):
    """Another worker failed or the user interrupted the run."""


class CopyControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.folder_lock = threading.Lock()
        self.lock = threading.Lock()
        self.clients = set()

    def check(self):
        if self.stopped.is_set():
            raise CopyCancelled()

    def register(self, client):
        with self.lock:
            self.clients.add(client)
            stopped = self.stopped.is_set()
        if stopped:
            self.disconnect(client)
            self.check()

    def unregister(self, client):
        with self.lock:
            self.clients.discard(client)

    @staticmethod
    def disconnect(client):
        # Wake a blocked read/write without touching IMAP parser state from
        # another thread. The owning worker closes the connection afterward.
        try:
            client._imap.sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass

    def stop(self):
        with self.lock:
            self.stopped.set()
            clients = list(self.clients)
        for client in clients:
            self.disconnect(client)


def copy_check(args):
    control = getattr(args, "copy_control", None)
    if control is not None:
        control.check()


def copy_wait(args, seconds):
    control = getattr(args, "copy_control", None)
    if control is None:
        time.sleep(seconds)
    else:
        control.stopped.wait(seconds)
        control.check()


def string(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def event(name, **fields):
    LOG.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                         "event": name, **fields}, ensure_ascii=False))


def digest(body):
    return hashlib.sha256(body).hexdigest()


def error_details(exc, include_message=False):
    """Keep useful server/network diagnostics without traceback locals or wire data."""
    message = str(exc) if include_message else "Error text omitted outside an IMAP operation."
    for secret in sorted(SECRETS, key=len, reverse=True):
        if secret:
            message = message.replace(secret, "[redacted]")
            message = message.replace(repr(secret)[1:-1], "[redacted]")
    if re.search(r"unexpected response|literal|parse|subject:|message-id:|content-type:|\r|\n",
                 message, re.IGNORECASE):
        message = "Protocol payload omitted to protect message content."
    details = {"type": type(exc).__name__, "message": message[:2000],
               "frames": [{"file": Path(frame.filename).name, "line": frame.lineno,
                           "function": frame.name}
                          for frame in traceback.extract_tb(exc.__traceback__)]}
    for attribute in ("errno", "winerror", "reason", "verify_code", "verify_message"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, str)):
            details[attribute] = value if isinstance(value, int) else redact(value)
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and cause is not exc:
        details["cause"] = error_details(cause, include_message=include_message)
    return details


def redact(value):
    for secret in sorted(SECRETS, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    return value[:2000]


class LoggedIMAP:
    """Log command metadata and timing, never command arguments containing mail."""
    def __init__(self, client, account):
        self.client = client
        self.context = {"host": account["host"], "port": account["port"],
                        "account": account["username"], "session": uuid.uuid4().hex[:12],
                        "timeout_s": account.get("timeout", 600)}
        self.started = time.monotonic()
        self.last_finished = self.started
        self.sequence = 0
        self.folder = None
        self.uploaded_bytes = 0
        self.uploads = 0

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if not callable(method):
            return method

        def command(*args, **kwargs):
            self.sequence += 1
            start = time.monotonic()
            fields = {**self.context, "operation": name, "sequence": self.sequence,
                      "session_age_s": round(start - self.started, 3),
                      "idle_s": round(start - self.last_finished, 3),
                      "session_uploaded_bytes": self.uploaded_bytes, "session_uploads": self.uploads}
            if name in ("append", "select_folder", "create_folder", "folder_exists") and args:
                fields["folder"] = args[0]
            elif self.folder is not None:
                fields["folder"] = self.folder
            if name == "append":
                fields["bytes"] = len(args[1])
            elif name == "fetch":
                fields["uid_count"] = len(args[0])
                fields["uids"] = list(args[0])[:10]
            elif name == "select_folder":
                fields["readonly"] = kwargs.get("readonly", False)
            event("imap_start", **fields)
            try:
                result = method(*args, **kwargs)
            except Exception as exc:
                details = error_details(exc, include_message=True)
                exc._migration_diagnostics = {**fields, "elapsed_s": round(time.monotonic() - start, 3),
                                               "error": details}
                event("imap_error", **exc._migration_diagnostics)
                raise
            else:
                if name == "select_folder":
                    self.folder = args[0]
                elif name == "append":
                    self.uploads += 1
                    self.uploaded_bytes += len(args[1])
                event("imap_complete", **fields, elapsed_s=round(time.monotonic() - start, 3))
                return result
            finally:
                self.last_finished = time.monotonic()
        return command


def load_config(path):
    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    target = config["target"]
    target.setdefault("host", "imap.gmail.com")
    target.setdefault("root", "Migration")
    sources = config["sources"]
    if not sources:
        raise StopMigration("Configure at least one source account.")
    ids, labels = set(), set()
    for account in [target, *sources]:
        if "email" in account:
            if not isinstance(account["email"], str) or not account["email"]:
                raise StopMigration("Each email must be a nonempty string.")
            account["username"] = account["email"]
        for field in ("host", "username"):
            if not isinstance(account.get(field), str) or not account[field]:
                raise StopMigration(f"Each account needs a nonempty {field}.")
        if "password" in account:
            if not isinstance(account["password"], str):
                raise StopMigration("Each password must be a string.")
        elif not isinstance(account.get("password_env"), str) or not account["password_env"]:
            raise StopMigration("Each account needs password or password_env.")
        account.setdefault("port", 993)
        secret = account.get("password") if "password" in account else os.environ.get(account["password_env"])
        if secret:
            SECRETS.add(secret)
    for source in sources:
        if "email" in source:
            source["id"] = source["email"]
            source["label"] = source["email"]
        source.setdefault("id", source["username"])
        account_id = source.get("id")
        source.setdefault("label", source["username"])
        if not isinstance(account_id, str) or not account_id or account_id in ids:
            raise StopMigration("Source IDs must be nonempty and unique.")
        label = source["label"]
        if not isinstance(label, str) or not label or "/" in label or label in labels:
            raise StopMigration("Source labels must be unique, nonempty, and contain no slash.")
        if (source["host"].lower(), source["username"].lower()) == (
                target["host"].lower(), target["username"].lower()):
            raise StopMigration("Source and destination must be different accounts.")
        ids.add(account_id)
        labels.add(label)
    if not isinstance(target["root"], str) or not target["root"].strip("/"):
        raise StopMigration("The destination root must be nonempty.")
    target["root"] = target["root"].strip("/")
    return config


@contextmanager
def connect(account):
    # Import lazily so --help and offline tests need no installed dependency.
    from imapclient import IMAPClient, SocketTimeout
    control = account.get("_copy_control")
    if control is not None:
        control.check()
    password = account.get("password") if "password" in account else os.environ.get(account["password_env"])
    if not password:
        if "password" in account:
            raise StopMigration(f"Set the password in JSON for {account['username']}.")
        raise StopMigration(f"Set credential environment variable {account['password_env']}.")
    SECRETS.add(password)
    timeout = account.get("timeout", 600)
    connect_timeout = account.get("connect_timeout", 60)
    context = {"host": account["host"], "port": account["port"], "account": account["username"],
               "operation": "connect", "timeout_s": timeout, "connect_timeout_s": connect_timeout}
    started = time.monotonic()
    event("imap_start", **context)
    try:
        raw_client = IMAPClient(account["host"], port=account["port"], ssl=True,
                               use_uid=True, timeout=SocketTimeout(connect=connect_timeout, read=timeout))
    except Exception as exc:
        exc._migration_diagnostics = {**context, "elapsed_s": round(time.monotonic() - started, 3),
                                       "error": error_details(exc, include_message=True)}
        event("imap_error", **exc._migration_diagnostics)
        raise
    event("imap_complete", **context, elapsed_s=round(time.monotonic() - started, 3))
    client = LoggedIMAP(raw_client, account)
    try:
        if control is not None:
            control.register(raw_client)
        client.login(account["username"], password)
        yield client
    finally:
        try:
            if control is not None and control.stopped.is_set():
                raw_client.shutdown()
            else:
                client.logout()
        except Exception:
            # The command wrapper logged this; preserve the original failure.
            try:
                client.shutdown()
            except Exception:
                pass
        finally:
            if control is not None:
                control.unregister(raw_client)


def folders(client, source, target):
    listing = sorted(client.list_folders(), key=lambda item: item[2])
    excluded = []
    for flags, delimiter, name in listing:
        delimiter = string(delimiter) if delimiter else None
        components = name.split(delimiter) if delimiter else [name]
        special = {string(flag).lower() for flag in flags}
        if special & {"\\trash", "\\junk"} or any(
                part.casefold() in JUNK_NAMES for part in components):
            excluded.append((name, delimiter))
        if name in source.get("ignore_folders", []):
            excluded.append((name, delimiter))
    used = set()
    for flags, delimiter, name in listing:
        delimiter = string(delimiter) if delimiter else None
        special = {string(flag).lower() for flag in flags}
        reason = None
        if any(name == parent or (sep and name.startswith(parent + sep))
               for parent, sep in excluded):
            reason = "spam/trash or configured exclusion"
        elif "\\noselect" in special:
            reason = "not selectable"
        components = name.split(delimiter) if delimiter else [name]
        # Escape literal slash and percent to keep mappings distinct.
        encoded = [part.replace("%", "%25").replace("/", "%2F") for part in components]
        mapped = "/".join([target["root"], source["label"], *encoded])
        if not reason and mapped in used:
            raise StopMigration(f"Folder mapping collision for account {source['id']}.")
        used.add(mapped)
        yield name, mapped, reason


def batches(values, size=500):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def snapshot(client, folder):
    info = client.select_folder(folder, readonly=True)
    validity = int(info[b"UIDVALIDITY"])
    uids = sorted(client.search(["NOT", "DELETED"]))
    metadata = {}
    for batch in batches(uids):
        metadata.update(client.fetch(batch, ["RFC822.SIZE", "FLAGS", "INTERNALDATE"]))
    if set(metadata) != set(uids):
        raise StopMigration("Source changed during inventory; rerun after mailbox activity settles.")
    return validity, metadata


class Database:
    def __init__(self, path, readonly=False):
        self.connection = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True,
                                         timeout=30) if readonly else sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        if readonly:
            return
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                account TEXT NOT NULL, folder TEXT NOT NULL, validity INTEGER NOT NULL,
                uid INTEGER NOT NULL, destination TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, flags TEXT NOT NULL, internaldate TEXT NOT NULL,
                status TEXT NOT NULL, target_validity INTEGER, target_uid INTEGER,
                PRIMARY KEY (account, folder, validity, uid));
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(messages)")}
        with self.connection:
            for column in ("before_validity", "before_uidnext"):
                if column not in columns:
                    self.connection.execute(f"ALTER TABLE messages ADD COLUMN {column} INTEGER")

    def bind(self, source, target, readonly=False):
        identity = json.dumps({"source": [source[k] for k in ("host", "port", "username")],
                               "target": [target[k] for k in ("host", "port", "username", "root")],
                               "label": source["label"]}, sort_keys=True)
        row = self.connection.execute("SELECT identity FROM accounts WHERE id=?", (source["id"],)).fetchone()
        if row and row[0] != identity:
            raise StopMigration(f"Account {source['id']} configuration differs from its saved state.")
        if readonly:
            if row is None:
                raise StopMigration(f"Account {source['id']} has no saved migration identity.")
            return
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?)", (source["id"], identity))

    def check_validity(self, account, folder, validity):
        if self.connection.execute("SELECT 1 FROM messages WHERE account=? AND folder=? AND validity!=?",
                                   (account, folder, validity)).fetchone():
            raise StopMigration(f"UIDVALIDITY changed for {account}/{folder}; manual reconciliation required.")

    def rows(self, account=None):
        if account is None:
            return self.connection.execute("SELECT * FROM messages").fetchall()
        return self.connection.execute("SELECT * FROM messages WHERE account=?", (account,)).fetchall()

    def get(self, key):
        return self.connection.execute("SELECT * FROM messages WHERE account=? AND folder=? AND validity=? AND uid=?",
                                       key).fetchone()

    def pending(self, key, destination, body, flags, date, before=None):
        before = before or {}
        with self.connection:
            self.connection.execute("""INSERT INTO messages
                (account, folder, validity, uid, destination, sha256, size, flags, internaldate,
                 status, before_validity, before_uidnext)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (*key, destination, digest(body), len(body),
                 json.dumps([string(flag) for flag in flags]), date.isoformat(),
                 before.get(b"UIDVALIDITY"), before.get(b"UIDNEXT")))

    def uploaded(self, key, validity, uid):
        with self.connection:
            self.connection.execute("""UPDATE messages SET status='uploaded', target_validity=?, target_uid=?
                WHERE account=? AND folder=? AND validity=? AND uid=?""", (validity, uid, *key))

    def delete_pending(self, key):
        with self.connection:
            self.connection.execute("""DELETE FROM messages WHERE account=? AND folder=? AND validity=? AND uid=?
                AND status='pending'""", key)

    def repair_pending(self, key, before):
        with self.connection:
            changed = self.connection.execute("""UPDATE messages SET status='pending',
                target_validity=NULL, target_uid=NULL, before_validity=?, before_uidnext=?
                WHERE account=? AND folder=? AND validity=? AND uid=? AND status='uploaded'""",
                (before[b"UIDVALIDITY"], before[b"UIDNEXT"], *key)).rowcount
            if changed != 1:
                raise StopMigration("Repair ledger record changed; stop and review before retrying.")

    def close(self):
        self.connection.close()


def row_key(row):
    return tuple(row[k] for k in ("account", "folder", "validity", "uid"))


def append_identity(response):
    match = re.search(rb"\[APPENDUID (\d+) (\d+)\]", response if isinstance(response, bytes)
                      else str(response).encode())
    return (int(match[1]), int(match[2])) if match else None


def ensure_folder(client, folder):
    parts = folder.split("/")
    for count in range(1, len(parts) + 1):
        parent = "/".join(parts[:count])
        if not client.folder_exists(parent):
            client.create_folder(parent)


def inventory(client, source, target, report):
    for folder, destination, reason in folders(client, source, target):
        entry = {"account": source["id"], "folder": folder, "destination": destination}
        if reason:
            entry.update(excluded=True, reason=reason)
        else:
            validity, metadata = snapshot(client, folder)
            sizes = [item[b"RFC822.SIZE"] for item in metadata.values()]
            dates = [item[b"INTERNALDATE"].isoformat() for item in metadata.values()]
            entry.update(uidvalidity=validity, messages=len(metadata), bytes=sum(sizes),
                         largest_bytes=max(sizes, default=0), uids=list(metadata),
                         earliest=min(dates, default=None), latest=max(dates, default=None))
        report["folders"].append(entry)
        event("inventory_folder", **{key: value for key, value in entry.items() if key != "uids"})


def copy_account(client, destination_client, source, target, db, args, report):
    for folder, destination, reason in folders(client, source, target):
        copy_check(args)
        if reason:
            event("excluded_folder", account=source["id"], folder=folder, reason=reason)
            continue
        validity, metadata = snapshot(client, folder)
        db.check_validity(source["id"], folder, validity)
        before = None  # Local to this folder and connection attempt.
        for uid in metadata:
            copy_check(args)
            key = (source["id"], folder, validity, uid)
            existing_row = db.get(key)
            if existing_row:
                if existing_row["status"] != "uploaded":
                    raise StopMigration("An upload remains uncertain; reconciliation is required.")
                report["already_uploaded"] += 1
                continue
            if args.max_messages is not None and report["uploaded"] >= args.max_messages:
                report["limited"] = True
                return
            if args.max_bytes is not None and report["bytes"] + metadata[uid][b"RFC822.SIZE"] > args.max_bytes:
                report["limited"] = True
                return
            data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]).get(uid)
            if not data or b"BODY[]" not in data:
                raise StopMigration("Source message disappeared during copy; rerun after mailbox activity settles.")
            body = data[b"BODY[]"]
            if args.max_bytes is not None and report["bytes"] + len(body) > args.max_bytes:
                report["limited"] = True
                return
            raw_flags = set(data[b"FLAGS"])
            if b"\\Deleted" in raw_flags:
                event("excluded_deleted", account=source["id"], folder=folder, uid=uid)
                continue
            flags = sorted(raw_flags & FLAGS)
            ignored = raw_flags - FLAGS - {b"\\Recent"}
            if ignored:
                event("unsupported_flags", account=source["id"], folder=folder, uid=uid,
                      count=len(ignored))
            if before is None:
                known = any(row["destination"] == destination for row in db.rows(source["id"]))
                if not known and destination_client.folder_exists(destination):
                    before = destination_client.select_folder(destination, readonly=True)
                    if before[b"EXISTS"]:
                        raise StopMigration("Destination folder already contains untracked mail; use the original state database or a new root.")
                control = getattr(args, "copy_control", None)
                if control is None:
                    ensure_folder(destination_client, destination)
                else:
                    # Account labels differ, but their root is shared.
                    with control.folder_lock:
                        copy_check(args)
                        ensure_folder(destination_client, destination)
                if before is None:
                    before = destination_client.select_folder(destination, readonly=True)
            date = data[b"INTERNALDATE"]
            copy_check(args)
            db.pending(key, destination, body, flags, date, before)
            event("upload_pending", account=source["id"], folder=folder, uid=uid,
                  destination=destination, bytes=len(body))
            # Never retry APPEND automatically, including on a lost response.
            response = destination_client.append(destination, body, flags, date)
            identity = append_identity(response)
            if identity is None:
                raise StopMigration("Upload returned no APPENDUID; run reconcile before copying again.")
            if identity[0] != int(before[b"UIDVALIDITY"]) or (
                    before.get(b"UIDNEXT") is not None and identity[1] < int(before[b"UIDNEXT"])):
                raise StopMigration("APPENDUID conflicts with cached destination state; manual review required.")
            db.uploaded(key, *identity)
            # With one writer, APPENDUID supplies the next upload's baseline.
            # This cache is discarded on folder changes and all reconnections.
            before = {b"UIDVALIDITY": identity[0], b"UIDNEXT": identity[1] + 1}
            report["uploaded"] += 1
            report["bytes"] += len(body)
            event("uploaded", account=source["id"], folder=folder, uid=uid,
                  destination=destination, target_uid=identity[1], bytes=len(body))
            copy_wait(args, max(args.delay, len(body) / (args.kib_per_second * 1024)))
        event("account_folder_complete", account=source["id"], folder=folder)


def fetch_message_ids(client, uids):
    """Read only Message-ID headers, without changing message flags."""
    result = {}
    field = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"
    key = b"BODY[HEADER.FIELDS (MESSAGE-ID)]"
    for batch in batches(uids):
        for uid, data in client.fetch(batch, [field]).items():
            header = BytesHeaderParser().parsebytes(data.get(key, b""))
            value = str(header.get("Message-ID", "")).strip()
            match = re.fullmatch(r"<?([^<>\s]+)>?", value)
            result[uid] = match[1] if match else None
    return result


def verify_account(client, destination_client, source, target, db, args, report):
    current_keys = set()
    for folder, destination, reason in folders(client, source, target):
        if reason:
            continue
        validity, metadata = snapshot(client, folder)
        db.check_validity(source["id"], folder, validity)
        rows = {row["uid"]: row for row in db.rows(source["id"])
                if row["folder"] == folder and row["validity"] == validity}
        target_info = None
        target_data = {}
        if destination_client.folder_exists(destination):
            target_info = destination_client.select_folder(destination, readonly=True)
            target_uids = sorted(destination_client.search(["ALL"]))
            fields = ["RFC822.SIZE", "FLAGS", "INTERNALDATE"]
            for batch in batches(target_uids):
                target_data.update(destination_client.fetch(batch, fields))
        else:
            target_uids = []
        failures = []
        known_targets = set()
        for uid in metadata:
            key = (source["id"], folder, validity, uid)
            current_keys.add(key)
            row = rows.get(uid)
            if args.uploaded_only and row is None:
                continue
            problems = []
            actual = None
            if not row or row["status"] != "uploaded":
                problems.append("not confirmed uploaded")
            elif not target_info or row["target_validity"] != int(target_info[b"UIDVALIDITY"]):
                problems.append("destination missing or UIDVALIDITY changed")
            else:
                if row["target_uid"] in known_targets:
                    problems.append("multiple source records share a destination UID")
                known_targets.add(row["target_uid"])
                actual = target_data.get(row["target_uid"])
                if not actual:
                    problems.append("destination message missing")
                else:
                    if actual[b"RFC822.SIZE"] != row["size"]:
                        problems.append("size differs")
                    expected_flags = set(json.loads(row["flags"]))
                    actual_flags = {string(flag) for flag in actual[b"FLAGS"] if flag in FLAGS}
                    if expected_flags != actual_flags or b"\\Deleted" in actual[b"FLAGS"]:
                        problems.append("flags differ")
                    if actual[b"INTERNALDATE"] != datetime.fromisoformat(row["internaldate"]):
                        problems.append("internal date differs")
                    if args.deep:
                        fetched = destination_client.fetch([row["target_uid"]], ["BODY.PEEK[]"])
                        body = fetched.get(row["target_uid"], {}).get(b"BODY[]")
                        if body is None or digest(body) != row["sha256"]:
                            problems.append("raw content hash differs")
            if problems:
                failures.append({"uid": uid, "problems": problems,
                                 "target_uid": row["target_uid"] if row else None,
                                 "expected_size": row["size"] if row else metadata[uid][b"RFC822.SIZE"],
                                 "actual_size": actual[b"RFC822.SIZE"] if actual else None})
            else:
                report["verified"] += 1
        if failures:
            source_ids = fetch_message_ids(client, [item["uid"] for item in failures])
            target_ids = fetch_message_ids(destination_client, [
                item["target_uid"] for item in failures if item["actual_size"] is not None])
            for item in failures:
                item["message_id"] = source_ids.get(item["uid"])
                item["gmail_message_id"] = target_ids.get(item["target_uid"])
                message_id = item["gmail_message_id"] or item["message_id"]
                item["gmail_search"] = (
                    f'label:{json.dumps(destination, ensure_ascii=False)} rfc822msgid:{message_id}'
                    if message_id else None)
        unexpected = sorted(set(target_uids) - known_targets)
        entry = {"account": source["id"], "folder": folder, "source_messages": len(metadata),
                 "expected": sum(uid in rows for uid in metadata) if args.uploaded_only else len(metadata),
                 "destination_messages": len(target_uids), "failures": failures,
                 "unexpected_destination_uids": unexpected}
        report["folders"].append(entry)
        report["problems"] += len(failures) + len(unexpected)
        event("verification_folder", **entry)
    stale = [row_key(row) for row in db.rows(source["id"]) if row_key(row) not in current_keys]
    if stale:
        report["problems"] += len(stale)
        report.setdefault("source_records_no_longer_in_scope", []).extend(stale)
        event("source_records_no_longer_in_scope", account=source["id"], count=len(stale))


def reconcile(destination_client, db, accounts, args, report, automatic=False):
    for row in db.rows():
        if row["account"] not in accounts or row["status"] != "pending":
            continue
        matches = []
        info = None
        if destination_client.folder_exists(row["destination"]):
            info = destination_client.select_folder(row["destination"], readonly=True)
            if row["before_validity"] is not None and int(info[b"UIDVALIDITY"]) != row["before_validity"]:
                report["problems"] += 1
                event("unresolved_upload", account=row["account"], folder=row["folder"], uid=row["uid"],
                      reason="destination UIDVALIDITY changed")
                continue
            uids = sorted(destination_client.search(["ALL"]))
            if row["before_uidnext"] is not None:
                uids = [uid for uid in uids if uid >= row["before_uidnext"]]
            for batch in batches(uids):
                for uid, metadata in destination_client.fetch(batch, ["RFC822.SIZE"]).items():
                    if metadata[b"RFC822.SIZE"] == row["size"]:
                        fetched = destination_client.fetch([uid], ["BODY.PEEK[]"])
                        body = fetched.get(uid, {}).get(b"BODY[]")
                        if body is not None and digest(body) == row["sha256"]:
                            matches.append(uid)
        key = row_key(row)
        # Do not assign a target occurrence already recorded for another source UID.
        claimed = {other["target_uid"] for other in db.rows(row["account"])
                   if other["destination"] == row["destination"] and other["status"] == "uploaded"
                   and info and other["target_validity"] == int(info[b"UIDVALIDITY"])}
        if len(matches) == 1 and matches[0] not in claimed:
            db.uploaded(key, int(info[b"UIDVALIDITY"]), matches[0])
            report["reconciled"] += 1
            event("reconciled", account=row["account"], folder=row["folder"], uid=row["uid"])
        elif not matches and automatic and info and row["before_validity"] is not None and row["before_uidnext"] is not None and (
                int(info.get(b"UIDNEXT", -1)) == row["before_uidnext"]):
            # UIDNEXT never goes backwards: the server assigned no UID to this
            # attempt. With the old connection closed, it is safe to reattempt.
            db.delete_pending(key)
            event("retry_safe", account=row["account"], folder=row["folder"], uid=row["uid"],
                  reason="unchanged destination UIDVALIDITY and UIDNEXT")
        elif not matches and args.retry_missing and not automatic:
            db.delete_pending(key)
            event("retry_authorized", account=row["account"], folder=row["folder"], uid=row["uid"])
        else:
            report["problems"] += 1
            event("unresolved_upload", account=row["account"], folder=row["folder"], uid=row["uid"],
                  matches=len(matches))


def transient_failure(exc):
    """Retry disconnections and explicit temporary server errors, not bad credentials."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, (imaplib.IMAP4.abort, TimeoutError, ConnectionError, socket.gaierror)):
        return True
    if isinstance(exc, OSError) and exc.errno in {
            errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
            errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN}:
        return True
    if isinstance(exc, imaplib.IMAP4.error):
        return bool(re.search(r"\[UNAVAILABLE\]|\[SERVERBUG\]|\[LIMIT\]|rate.?limit|too many|"
                              r"temporar|try again|bandwidth", str(exc), re.IGNORECASE))
    return False


def copy_with_recovery(source, target, db, args, report):
    failures = 0
    initial_keys = {row_key(row) for row in db.rows(source["id"])}
    while True:
        copy_check(args)
        uploaded_before = report["uploaded"]
        skips_before = report["already_uploaded"]
        phase = "target"
        try:
            # Fresh connections on each retry also discard stale folder selections.
            with connect(target) as destination_client:
                phase = "reconcile"
                pending = [row for row in db.rows(source["id"]) if row["status"] == "pending"]
                if pending:
                    recovery = {"reconciled": 0, "problems": 0}
                    reconcile(destination_client, db, {source["id"]}, args, recovery, automatic=True)
                    report["reconciled"] += recovery["reconciled"]
                    for row in pending:
                        key = row_key(row)
                        recovered = db.get(key)
                        if recovered and recovered["status"] == "uploaded" and key not in initial_keys:
                            # Count an accepted-but-unacknowledged upload toward
                            # this run's pilot limits exactly once.
                            report["uploaded"] += 1
                            report["bytes"] += row["size"]
                            initial_keys.add(key)
                    if recovery["problems"]:
                        raise StopMigration("An upload could not be reconciled safely; manual review required.")
                phase = "source"
                with connect(source) as client:
                    copy_account(client, destination_client, source, target, db, args, report)
            return
        except Exception as exc:
            report["already_uploaded"] = skips_before
            copy_check(args)
            if isinstance(exc, sqlite3.Error):
                raise  # A broken or full state store is unsafe for every account.
            details = getattr(exc, "_migration_diagnostics", {"error": error_details(exc)})
            if transient_failure(exc):
                if report["uploaded"] > uploaded_before:
                    failures = 0
                if failures < args.max_retries:
                    delay = min(args.retry_max_delay, args.retry_delay * (2 ** min(failures, 20)))
                    failures += 1
                    event("retry_wait", account=source["id"], attempt=failures,
                          max_retries=args.max_retries, delay_s=delay, diagnostics=details)
                    copy_wait(args, delay)
                    continue
            failure = {"account": source["id"], "retries": failures,
                       "reason": str(exc) if isinstance(exc, StopMigration) else type(exc).__name__,
                       "diagnostics": details}
            report.setdefault("failed_accounts", []).append(failure)
            report["problems"] += 1
            event("account_failed", **failure)
            is_target = phase == "target" or (details.get("host"), details.get("account")) == (
                target["host"], target["username"])
            # A failed APPEND can be specific to one large/rejected message.
            # Keep that account incomplete, but do not strand every later source.
            server_message = details.get("error", {}).get("message", "")
            global_target_failure = is_target and (
                phase == "target" or details.get("operation") != "append" or
                bool(re.search(r"quota|bandwidth|rate.?limit|too many|\[LIMIT\]",
                               server_message, re.IGNORECASE)))
            if global_target_failure or args.stop_on_error:
                raise StopMigration("Copy stopped after account failure; see failed_accounts in the report.") from exc
            return


def copy_accounts_parallel(sources, target, state_path, args, report, workers):
    control = CopyControl()
    args.copy_control = control
    target = {**target, "_copy_control": control}
    local_reports = []

    def worker(source, local):
        db = None
        try:
            control.check()
            db = Database(state_path)  # SQLite connections belong to their worker.
            event("account_start", account=source["id"])
            copy_with_recovery({**source, "_copy_control": control}, target, db, args, local)
            if not local.get("failed_accounts"):
                event("account_complete", account=source["id"], uploaded=local["uploaded"],
                      limited=local["limited"])
        except CopyCancelled:
            event("account_cancelled", account=source["id"])
        except BaseException:
            control.stop()
            raise
        finally:
            if db is not None:
                db.close()

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="migration")
    futures = []
    try:
        for source in sources:
            local = {"uploaded": 0, "bytes": 0, "already_uploaded": 0, "reconciled": 0,
                     "problems": 0, "limited": False}
            local_reports.append(local)
            futures.append(executor.submit(worker, source, local))
        for future in as_completed(futures):
            future.result()
    finally:
        control.stop()
        executor.shutdown(wait=True, cancel_futures=True)
        # Workers never mutate the combined report. Merge even on interruption
        # so successful uploads and failures appear in the final report.
        for local in local_reports:
            for key in ("uploaded", "bytes", "already_uploaded", "reconciled", "problems"):
                report[key] += local[key]
            if local.get("failed_accounts"):
                report.setdefault("failed_accounts", []).extend(local["failed_accounts"])
        del args.copy_control


def repair_candidates(args, target, selected):
    audit = json.loads(Path(args.verify_report).read_text(encoding="utf-8"))
    if audit.get("command") != "verify" or audit.get("error") or audit.get("uploaded_only"):
        raise StopMigration("Repair requires a completed full verification report, without an execution error.")
    if audit.get("destination") != {key: target[key] for key in ("host", "username", "root")}:
        raise StopMigration("Verification report destination differs from the configured destination.")
    candidates = {}
    for folder in audit.get("folders", []):
        if folder["account"] not in selected:
            continue
        for failure in folder.get("failures", []):
            if "destination message missing" in failure["problems"]:
                candidates[(folder["account"], folder["folder"], int(failure["uid"]))] = failure
    return candidates


def gmail_repair_folders(client):
    special = {}
    for flags, delimiter, name in client.list_folders():
        flags = {string(flag).lower() for flag in flags}
        if "\\noselect" in flags:
            continue
        for flag in ("\\all", "\\junk", "\\trash"):
            if flag in flags:
                special[flag] = name
    if len(special) != 3:
        raise StopMigration("Repair needs IMAP-visible All Mail, Spam, and Trash to search safely.")
    return list(special.values())


def repair_account(client, gmail, source, target, db, args, report, candidates, search_folders):
    mapped = {folder: destination for folder, destination, reason in folders(client, source, target)
              if not reason}
    for account, folder, uid in candidates:
        item = {"account": account, "folder": folder, "uid": uid}
        report["repairs"].append(item)

        def finish(status, reason=None):
            item["status"] = status
            report["repair_summary"][status] = report["repair_summary"].get(status, 0) + 1
            if reason:
                item["reason"] = reason
            if status == "ambiguous":
                report["problems"] += 1
            event("repair_message", **item)

        rows = [row for row in db.rows(account) if row["folder"] == folder and row["uid"] == uid]
        if len(rows) != 1 or rows[0]["status"] != "uploaded":
            finish("ambiguous", "No unique confirmed ledger record; reconcile pending uploads first.")
            continue
        row = rows[0]
        key = row_key(row)
        destination = row["destination"]
        if mapped.get(folder) != destination:
            finish("ambiguous", "Source folder is excluded, missing, or mapped to a different label.")
            continue
        info = client.select_folder(folder, readonly=True)
        if int(info[b"UIDVALIDITY"]) != row["validity"]:
            finish("ambiguous", "Source UIDVALIDITY changed.")
            continue
        data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]).get(uid, {})
        body = data.get(b"BODY[]")
        if body is None or b"\\Deleted" in data.get(b"FLAGS", ()) or digest(body) != row["sha256"]:
            finish("ambiguous", "Source message is missing, deleted, or differs from the uploaded hash.")
            continue
        if gmail.folder_exists(destination):
            info = gmail.select_folder(destination, readonly=True)
            if int(info[b"UIDVALIDITY"]) != row["target_validity"]:
                finish("ambiguous", "Destination UIDVALIDITY changed.")
                continue
            if gmail.fetch([row["target_uid"]], ["RFC822.SIZE"]).get(row["target_uid"]):
                finish("already_present")
                continue
        message_id = fetch_message_ids(client, [uid]).get(uid)
        item["message_id"] = message_id
        if not message_id:
            finish("ambiguous", "Message has no usable Message-ID for a complete Gmail search.")
            continue
        matches = {}
        candidate_count = 0
        # Search each special mailbox explicitly: IMAP UIDs are mailbox-local,
        # even when X-GM-RAW accepts Gmail's global search syntax.
        for mailbox in dict.fromkeys([destination, *search_folders]):
            if mailbox == destination and not gmail.folder_exists(mailbox):
                continue
            info = gmail.select_folder(mailbox, readonly=True)
            uids = gmail.gmail_search(f"in:anywhere rfc822msgid:{json.dumps(message_id)}")
            for batch in batches(uids):
                fetched = gmail.fetch(batch, ["X-GM-MSGID", "BODY.PEEK[]"])
                if set(fetched) != set(batch):
                    finish("ambiguous", "Gmail search results changed before candidate bodies were fetched.")
                    break
                for target_uid, candidate in fetched.items():
                    candidate_count += 1
                    if candidate.get(b"BODY[]") is None or b"X-GM-MSGID" not in candidate:
                        finish("ambiguous", "Gmail candidate could not be read completely.")
                        break
                    if digest(candidate[b"BODY[]"]) == row["sha256"]:
                        matches[int(candidate[b"X-GM-MSGID"])] = {
                            "mailbox": mailbox, "uid": target_uid,
                            "validity": int(info[b"UIDVALIDITY"])}
                if item.get("status") == "ambiguous":
                    break
            if item.get("status") == "ambiguous":
                break
        if item.get("status") == "ambiguous":
            continue
        if len(matches) > 1 or (candidate_count and not matches):
            finish("ambiguous", "Multiple exact matches, or Message-ID candidates differ in raw content.")
            continue
        if matches:
            gm_id, match = next(iter(matches.items()))
            item["gmail_id"] = gm_id
            item["found_in"] = match["mailbox"]
            if gmail.folder_exists(destination):
                info = gmail.select_folder(destination, readonly=True)
                present = gmail.search(["X-GM-MSGID", gm_id])
                claimed = {other["target_uid"] for other in db.rows(account)
                           if row_key(other) != key and other["destination"] == destination and
                           other["status"] == "uploaded" and other["target_validity"] == int(info[b"UIDVALIDITY"])}
                if len(present) > 1 or any(target_uid in claimed for target_uid in present):
                    finish("ambiguous", "Matching destination occurrence is already claimed by another source UID.")
                    continue
            if not args.apply:
                finish("found_elsewhere")
                continue
            ensure_folder(gmail, destination)
            info = gmail.select_folder(match["mailbox"], readonly=False)
            fresh = gmail.fetch([match["uid"]], ["X-GM-MSGID", "BODY.PEEK[]"]).get(match["uid"], {})
            if (int(info[b"UIDVALIDITY"]) != match["validity"] or
                    fresh.get(b"X-GM-MSGID") != gm_id or digest(fresh.get(b"BODY[]", b"")) != row["sha256"]):
                finish("ambiguous", "Matched Gmail message changed before label restoration.")
                continue
            gmail.add_gmail_labels([match["uid"]], [destination], silent=True)
            info = gmail.select_folder(destination, readonly=True)
            uids = gmail.search(["X-GM-MSGID", gm_id])
            claimed = {other["target_uid"] for other in db.rows(account)
                       if row_key(other) != key and other["destination"] == destination and
                       other["status"] == "uploaded" and other["target_validity"] == int(info[b"UIDVALIDITY"])}
            if len(uids) != 1 or uids[0] in claimed:
                finish("ambiguous", "Restored label has no unique, unclaimed Gmail UID; label may already be added.")
                continue
            db.uploaded(key, int(info[b"UIDVALIDITY"]), uids[0])
            item["target_uid"] = uids[0]
            finish("restored")
        elif not args.apply:
            finish("confirmed_absent")
        else:
            ensure_folder(gmail, destination)
            before = gmail.select_folder(destination, readonly=True)
            if int(before[b"UIDVALIDITY"]) != row["target_validity"]:
                finish("ambiguous", "Destination UIDVALIDITY changed before re-upload.")
                continue
            db.repair_pending(key, before)  # Durable before APPEND; never blindly retry.
            item["status"] = "upload_pending"
            event("repair_upload_pending", **item, bytes=len(body))
            flags = [flag.encode() for flag in json.loads(row["flags"])]
            response = gmail.append(destination, body, flags, datetime.fromisoformat(row["internaldate"]))
            identity = append_identity(response)
            if not identity or identity[0] != int(before[b"UIDVALIDITY"]) or identity[1] < int(before[b"UIDNEXT"]):
                raise StopMigration("Repair APPEND returned no consistent UID; reconcile the pending record before retrying.")
            db.uploaded(key, *identity)
            report["uploaded"] += 1
            report["bytes"] += len(body)
            item["target_uid"] = identity[1]
            finish("uploaded")
            copy_wait(args, max(args.delay, len(body) / (args.kib_per_second * 1024)))


def repair(sources, target, db, args, report):
    candidates = repair_candidates(args, target, {source["id"] for source in sources})
    report.update(repairs=[], repair_summary={}, repair_apply=args.apply, repair_source_report=args.verify_report)
    event("repair_start", candidates=len(candidates), apply=args.apply)
    if not candidates:
        return
    if args.apply:
        backup = Path(args.state).with_name(Path(args.state).name + ".repair-" + uuid.uuid4().hex + ".bak")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        copy = sqlite3.connect(backup)
        try:
            db.connection.backup(copy)
        finally:
            copy.close()
        report["state_backup"] = str(backup)
        event("repair_backup", path=str(backup))
    with connect(target) as gmail:
        if b"X-GM-EXT-1" not in gmail.capabilities():
            raise StopMigration("Repair requires Gmail IMAP extensions.")
        search_folders = gmail_repair_folders(gmail)
        for source in sources:
            selected = [key for key in candidates if key[0] == source["id"]]
            if selected:
                with connect(source) as client:
                    repair_account(client, gmail, source, target, db, args, report, selected, search_folders)
    event("repair_complete", summary=report["repair_summary"], apply=args.apply)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["inventory", "copy", "verify", "reconcile", "repair"])
    cli.add_argument("--config", default="accounts.json")
    cli.add_argument("--state", default="migration.sqlite")
    cli.add_argument("--log-dir", default="logs")
    cli.add_argument("--account", action="append", help="Source ID; repeat to select several (default: all)")
    cli.add_argument("--workers", type=int,
                     help="Copy: concurrent source accounts (default: 3, capped at selected accounts)")
    cli.add_argument("--dry-run", action="store_true", help="Copy: inventory only; repair: preview (default)")
    cli.add_argument("--verify-report", help="Repair: completed verification JSON report containing missing messages")
    cli.add_argument("--apply", action="store_true", help="Repair: restore labels/ledger or upload confirmed absent mail")
    cli.add_argument("--max-messages", type=int, help="Maximum new uploads across this entire run")
    cli.add_argument("--max-bytes", type=int, help="Maximum new message bytes across this entire run")
    cli.add_argument("--delay", type=float, default=1, help="Minimum seconds between uploads (default: 1)")
    cli.add_argument("--kib-per-second", type=float, default=128, help="Upload pacing (default: 128 KiB/s)")
    cli.add_argument("--deep", action="store_true", help="Verify raw content hashes, downloading every target message")
    cli.add_argument("--uploaded-only", action="store_true", help="Verify only recorded pilot messages; not a final audit")
    cli.add_argument("--retry-missing", action="store_true",
                     help="Reconcile: allow retry of unmatched uploads ONLY after manually confirming absence")
    cli.add_argument("--max-retries", type=int, default=10, help="Copy: retries without upload progress (default: 10)")
    cli.add_argument("--retry-delay", type=float, default=10, help="Initial retry wait in seconds (default: 10)")
    cli.add_argument("--retry-max-delay", type=float, default=300, help="Maximum retry wait in seconds (default: 300)")
    cli.add_argument("--stop-on-error", action="store_true", help="Copy: stop instead of continuing to another source")
    cli.add_argument("--timeout", type=float, default=600,
                     help="IMAP read/write timeout in seconds, including uploads (default: 600)")
    cli.add_argument("--connect-timeout", type=float, default=60,
                     help="IMAP connection timeout in seconds (default: 60)")
    return cli


def run(args, report):
    config = load_config(args.config)
    selected = set(args.account or [source["id"] for source in config["sources"]])
    sources = [source for source in config["sources"] if source["id"] in selected]
    if selected != {source["id"] for source in sources}:
        raise StopMigration("Unknown --account ID.")
    target = config["target"]
    if args.workers is not None and not 1 <= args.workers <= len(config["sources"]):
        raise StopMigration("--workers must be between 1 and the number of configured source accounts.")
    for account in [target, *sources]:
        account["timeout"] = args.timeout
        account["connect_timeout"] = args.connect_timeout
    report["accounts"] = [source["id"] for source in sources]
    report["destination"] = {key: target[key] for key in ("host", "username", "root")}
    if (args.dry_run and args.command != "repair") or args.command == "inventory":
        for source in sources:
            with connect(source) as client:
                inventory(client, source, target, report)
        return
    state_path = Path(args.state)
    if args.command in ("verify", "reconcile", "repair") and not state_path.exists():
        raise StopMigration("No migration database exists; copy a pilot first.")
    # One process per state file, including verification and reconciliation.
    with open(str(state_path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopMigration("Another process is using this migration database.") from None
        readonly = args.command == "repair" and not args.apply
        db = Database(state_path, readonly=readonly)
        try:
            for source in sources:
                db.bind(source, target, readonly=readonly)
            if args.command == "repair":
                repair(sources, target, db, args, report)
                return
            if args.command == "copy":
                workers = min(args.workers or 3, len(sources))
                if args.max_messages is not None or args.max_bytes is not None:
                    # Pilot limits are global; keep their exact existing semantics.
                    workers = 1
                report["workers"] = workers
                event("copy_workers", workers=workers, accounts=len(sources),
                      pilot_limits=args.max_messages is not None or args.max_bytes is not None)
                if workers > 1:
                    copy_accounts_parallel(sources, target, state_path, args, report, workers)
                    return
                for source in sources:
                    before = report["uploaded"]
                    before_failures = len(report.get("failed_accounts", []))
                    event("account_start", account=source["id"])
                    copy_with_recovery(source, target, db, args, report)
                    if len(report.get("failed_accounts", [])) == before_failures:
                        event("account_complete", account=source["id"], uploaded=report["uploaded"] - before,
                              limited=report["limited"])
                    if report["limited"]:
                        break
                return
            with connect(target) as destination_client:
                if args.command == "reconcile":
                    reconcile(destination_client, db, selected, args, report)
                    return
                for source in sources:
                    before = report["uploaded"]
                    event("account_start", account=source["id"])
                    with connect(source) as client:
                        verify_account(client, destination_client, source, target, db, args, report)
                    event("account_complete", account=source["id"], uploaded=report["uploaded"] - before,
                          limited=report["limited"])
                    if report["limited"]:
                        break
        finally:
            db.close()


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "repair" and not args.verify_report:
        cli.error("repair requires --verify-report")
    if (args.apply or args.verify_report) and args.command != "repair":
        cli.error("--apply and --verify-report are only valid with repair")
    if args.apply and args.dry_run:
        cli.error("--apply cannot be combined with --dry-run")
    if args.command == "repair" and (args.max_messages is not None or args.max_bytes is not None):
        cli.error("repair uses the verification candidate list; pilot limits are only supported by copy")
    if args.delay < 0 or args.kib_per_second <= 0 or any(
            limit is not None and limit < 0 for limit in (args.max_messages, args.max_bytes)):
        cli.error("Limits and delay must be nonnegative; KiB/s must be positive.")
    if args.retry_missing and args.command != "reconcile":
        cli.error("--retry-missing is only valid with reconcile")
    if (args.deep or args.uploaded_only) and args.command != "verify":
        cli.error("--deep and --uploaded-only are only valid with verify")
    if args.max_retries < 0 or any(not math.isfinite(value) or value <= 0 for value in (
            args.retry_delay, args.retry_max_delay, args.timeout, args.connect_timeout)):
        cli.error("max-retries must be nonnegative; retry delays and timeouts must be finite and positive")
    os.umask(0o077)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(log_dir / f"{run_id}.jsonl", encoding="utf-8"),
                logging.StreamHandler()]
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(handler)
    report = {"run_id": run_id, "command": "inventory" if args.dry_run and args.command != "repair" else args.command,
              "pacing": {"delay": args.delay, "kib_per_second": args.kib_per_second},
              "retry_policy": {"max_retries": args.max_retries, "retry_delay": args.retry_delay,
                               "retry_max_delay": args.retry_max_delay},
              "timeouts": {"read_write_s": args.timeout, "connect_s": args.connect_timeout},
              "uploaded_only": args.uploaded_only, "deep": args.deep,
              "folders": [], "uploaded": 0, "already_uploaded": 0, "bytes": 0,
              "verified": 0, "reconciled": 0, "problems": 0, "limited": False}
    exit_code = 0
    event("run_start", command=report["command"], pacing=report["pacing"],
          timeouts=report["timeouts"], python=sys.version.split()[0])
    try:
        run(args, report)
        if report["problems"]:
            exit_code = 1
    except KeyboardInterrupt:
        report["error"] = "Interrupted; any pending upload must be reconciled before resuming."
        exit_code = 130
    except Exception as exc:
        report["diagnostics"] = getattr(exc, "_migration_diagnostics", {"error": error_details(exc)})
        report["error"] = str(exc) if isinstance(exc, StopMigration) else (
            f"{type(exc).__name__}: stopped; check connection, credentials, storage and server limits. "
            "Any pending upload must be reconciled before resuming.")
        exit_code = 1
    report["result"] = "incomplete" if exit_code else ("limited" if report["limited"] else "ok")
    report_path = log_dir / f"{run_id}-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    event("run_complete", report=str(report_path), result=report["result"],
          uploaded=report["uploaded"], verified=report["verified"], problems=report["problems"],
          error=report.get("error"))
    for handler in handlers:
        LOG.removeHandler(handler)
        handler.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

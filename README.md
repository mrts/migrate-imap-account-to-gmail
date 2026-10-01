# IMAP accounts to Gmail

Copy several IMAP accounts into one personal Gmail account, preserving folders as
nested labels:

```text
Migration/first@example.com/INBOX
Migration/first@example.com/Sent
Migration/second@example.com/INBOX
```

The script copies mail only. It opens source folders read-only and fetches bodies
with `BODY.PEEK[]`, so reading mail does not mark it read. It never deletes or
expunges mail. Spam, Trash, their subfolders, and messages flagged deleted are
excluded. Dates, raw message bytes, and supported read/answered/starred/draft flags
are passed to Gmail. Custom flags are omitted and their omission is logged.
Parent labels organize the hierarchy; they are not separately applied to every
message. Empty source folders do not create labels. Imported Sent and Inbox folders are archive labels, not Gmail's native
Sent and Inbox.

Requires Python 3.10+ on Linux/macOS. Uses one pinned dependency, IMAPClient.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp accounts.example.json accounts.json
```

Edit `accounts.json`: enter your Gmail address and all 19 source accounts.
Each account uses one `email` field. For sources, this is also the account ID,
IMAP login username, and Gmail label. Enter passwords directly in `password`:

```json
{
  "host": "imap.example.com",
  "email": "first@example.com",
  "password": "your-source-password"
}
```

Use a Gmail **app password** for the target's `password`, with 2-Step Verification
if your account supports it; your ordinary Google password will not work. See
[Google's app-password instructions](https://support.google.com/accounts/answer/185833).
Fill in the blank passwords before connecting. Inventory needs only source
passwords; the target password can remain blank until copying or verifying.
`accounts.json`, logs, and state files are ignored by Git. Keep the configuration
private since it contains passwords; for example, `chmod 600 accounts.json`.
Passwords are never written to logs or reports.

OAuth is not implemented. All connections use TLS with certificate verification,
port 993 by default. A source can override `port` for another implicit-TLS port.
IMAP reads/writes, including large uploads, have a 600-second timeout by default;
connection setup has a separate 60-second timeout. Change these for all accounts
with `--timeout 1200 --connect-timeout 60`, for example. The selected timeouts are
recorded in operation logs and reports. This allows large messages to finish
without extending the wait for an unreachable server.
The old `username`/`id`/`label` configuration still works. As an optional alternative
to a direct password, use `password_env` to name an environment variable. If both
are supplied, the direct `password` takes precedence.

Spam/Trash detection uses IMAP `\\Junk`/`\\Trash` special-use flags plus common names
(Spam, Trash, Junk, Junk E-mail, Deleted Items, Deleted Messages, Bin), ignoring
case. Check the inventory for any localized or unusual folder names the server
does not identify. Add these exact names to that source's `ignore_folders`; their
subfolders are also excluded. Non-selectable container folders are skipped.
Source hierarchy delimiters are converted to `/`; literal slashes and percent
characters within a component are escaped to avoid collisions.

## 1. Inventory / dry run

```bash
python migrate-imap-account-to-gmail.py inventory
```

This connects only to the sources. It creates no Gmail labels, uploads no mail,
and does not modify the resume database. `copy --dry-run` does the same thing.
The JSON report contains per-folder counts, total and largest message sizes,
date ranges, UID validity, source UIDs, and exclusions. Compare total source bytes
with Gmail's available storage, allowing headroom, before copying. Storage quota
is checked manually in Gmail; the script does not query it.

Use `--account first@example.com` to inspect one source. Repeating `--account` selects
several; omitting it selects all configured sources in configuration order.

## 2. Small pilot

```bash
python migrate-imap-account-to-gmail.py copy --account first@example.com --max-messages 20 --max-bytes 52428800
python migrate-imap-account-to-gmail.py verify --account first@example.com --uploaded-only --deep
```

The pilot copies the first eligible messages in folder/UID order, up to 20 new
messages or 50 MiB. The byte limit stops before a message that would exceed it;
it does not skip ahead. Inspect the messages in Gmail: attachments, old dates,
read/starred status, and folder labels. If these first messages do not include
representative attachments or older mail, extend the pilot before the full run.
A pilot on each distinct source provider is useful.

`--uploaded-only` checks the recorded pilot rather than requiring the whole
account to have been copied. `--deep` additionally downloads the uploaded bodies
and compares their SHA-256 hashes. Keep the same state database and labels for
the full migration. **Repeating the limited copy copies the next 20 unseen
messages**, skipping confirmed uploads; the limit applies to new uploads in each
run, not a fixed selection.

## 3. Full copy and catch-up

```bash
python migrate-imap-account-to-gmail.py copy --account first@example.com
python migrate-imap-account-to-gmail.py verify --account first@example.com
python migrate-imap-account-to-gmail.py copy --workers 3 --delay 0 --kib-per-second 1000000
```

Copying defaults to three workers, capped at the number of selected accounts.
Set `--workers` from 1 to the number of configured accounts; `--workers 1` copies
sequentially. Each worker handles a different account, with its own source/Gmail
connections and SQLite connection to the shared ledger. As an account finishes,
the worker takes the next account. Confirmed uploads are skipped on restart.
The existing process lock still prevents a second script from using this ledger.
Inventory, verification, and manual reconciliation remain sequential.

Runs with `--max-messages` or `--max-bytes` use one worker so pilot limits remain
exact across the entire run. The effective worker count appears in the
`copy_workers` log event and the final report's `workers` field. Upload and
account events identify each source account even when log lines are interleaved.

By default each worker paces uploads to at most roughly 128 KiB/s with a minimum
one-second pause per message. To use the faster settings
from the pilot, pass `--delay 0 --kib-per-second 1000000`. These are pacing settings,
not a claim about personal Gmail's quotas. All 19 accounts share the destination's
storage and service limits.

During copying, Gmail's destination folder is selected once per source folder
and connection attempt. Successful APPENDUID responses update the cached UID
state for the next upload, avoiding a SELECT round trip for every message.
Reconnection and pending-upload reconciliation always read fresh Gmail state.
Each destination folder has only one worker writing to it. Keep other programs
from adding, moving, or deleting mail in these destination labels while copying.

Copying automatically recovers from socket resets, EOF/disconnections, timeouts,
DNS failures, and explicit temporary/rate-limit server errors. It closes the old
connections, waits with exponential backoff, reconnects, and resumes from the
ledger. Defaults are 10 retries without upload progress, with waits of 10, 20, 40,
80, 160, then 300 seconds. Successful upload progress resets the retry budget.
Change these with `--max-retries`, `--retry-delay`, and `--retry-max-delay`.
With multiple workers, Ctrl-C stops scheduling new work, interrupts retry/pacing
waits, and shuts down active sockets to wake blocked uploads or reads. Pending
uploads remain journaled for reconciliation on restart; completed uploads are
included in the final report. A connection still being established can take up
to its connection timeout to exit. A fatal destination or database error also
stops all workers; source-account failures retain the existing continue policy.
Ctrl-C interrupts retry waits immediately. Inventory/verify/manual reconcile do
not automatically retry their reads.

Each upload is journaled before APPEND, including destination UIDVALIDITY and
UIDNEXT. If a connection drops during APPEND, the next attempt first reconciles
that pending record. An exact, unclaimed match among newly assigned destination
UIDs is recorded as accepted. If UIDVALIDITY and UIDNEXT are unchanged on the
fresh connection, no UID was assigned, so the pending attempt can be retried.
Changed UID validity, unmatched newly assigned UIDs, missing labels, or ambiguous
matches require manual review. Existing pending records from before this upgrade
have no UID baseline: an exact match can recover them, but an unmatched one still
needs manual confirmation. The database schema upgrades automatically while
preserving saved messages. Do not move/delete imported messages during recovery.

An unresolved upload or exhausted source retries marks that account failed and
continues to the next source; its pending record does not block unrelated accounts.
The final report lists `failed_accounts` and exits nonzero if any failed. Use
`--stop-on-error` to stop at the first source failure. Destination authentication
failures, exhausted destination connection or non-upload retries, destination
quota/rate-limit failures after retries, and state-store errors stop the entire
run. A single APPEND that exhausts its retries (such as a large message timing out)
marks its source account incomplete and allows later accounts to proceed; its
pending record is preserved for safe recovery. No failed account is reported as
complete, and no ambiguous upload is blindly
retried. The attempt budget prevents permanent failures from looping indefinitely.

### Gmail throttling

During the personal-Gmail migration, Gmail returned
`append failed: System Error (Failure) [THROTTLED]`. This is an explicit server
rejection, distinct from a socket timeout. Command latency also increased during
the run, but slow responses alone do not prove throttling or reveal its cause.
The error does not identify the exact allowance that was reached.

All workers upload into the same Gmail account. Separate source accounts and
labels do not provide separate destination accounts or independent allowances.
Additional workers can overlap response waits, but may increase pressure on
Gmail's limits. In this migration, parallel uploads initially improved throughput,
then individual accounts encountered throttling. Changing networks or restarting
the script should not be assumed to reset Gmail's limits.

Google's [Workspace bandwidth documentation](https://knowledge.workspace.google.com/admin/gmail/gmail-bandwidth-limits)
currently lists **500 MB/day for IMAP uploads** and **2,500 MB/day for IMAP
downloads**, across all Workspace editions, with limits subject to change.
It says large transfers can temporarily stop IMAP uploads; a documented bandwidth
suspension typically lasts an hour and can last up to 24 hours. These figures
are for Workspace, not a confirmed allowance or cooldown for ordinary Gmail.
An individual `[THROTTLED]` response also does not establish that the account
has been suspended.

**Current limitation:** the retry classifier does not recognize `[THROTTLED]`
explicitly. That APPEND error marks the source account failed and processing
continues with other accounts. The failed account is not queued again in that run;
look for `imap_error`, `account_failed`, and the final report's `failed_accounts`.
A lost or rejected APPEND may leave a pending ledger record for reconciliation.

If throttling repeats, pause uploads to let the account recover, then resume with
fewer workers (for example `--workers 1`) and retain the default pacing, or use a
larger `--delay`. These settings may help but do not guarantee that Gmail accepts
further uploads. Keep the same ledger; do not delete it or force a pending upload
to be retried just to bypass an error. Check the report after resuming and run
verification when copying finishes. The script has no shared throttling cooldown
across workers yet.

For overnight use, run all configured accounts detached from the terminal:

```bash
nohup .venv/bin/python migrate-imap-account-to-gmail.py copy \
  --delay 0 --kib-per-second 1000000 > overnight.log 2>&1 &
```

Watch with `tail -f overnight.log`. Keep the computer awake and connected; `nohup`
protects against closing the terminal, not sleep or shutdown. In the morning,
check the final report and run `verify`. If there are failed accounts, resolve the
reported problem and rerun `copy`; confirmed uploads will be skipped.

Use the same `migration.sqlite` throughout (or always pass the same `--state`).
Do not delete it to retry. Account identity, destination root/label, and source
UID validity are checked against saved state. Changes require investigation rather
than silently starting over. One process can hold the state-file lock at a time;
do not bypass this by starting concurrent copies with different state files.
The old script's `.py.sqlite` database is not compatible. This version refuses to
start copying into a nonempty destination folder with no matching recorded state.

Keep source folders stable during the migration: avoid moves, deletions, and
renames. New arrivals can be picked up by running `copy` again. This is an archive
copy, not a two-way sync: later flag changes, deletions, and moves are not mirrored.
There is no cross-account deduplication. Gmail sources with overlapping labels
can expose the same content in several folders; review those folder memberships
in the pilot. Verification reports ambiguous or shared destination identities.

## Recovery after an interrupted upload

```bash
python migrate-imap-account-to-gmail.py reconcile
```

This reads Gmail and looks for an exact content hash match in the intended label
(and, when journaled, among UIDs assigned after the upload started).
One unclaimed match is recorded as uploaded. Zero or ambiguous matches remain
pending and return a nonzero exit code. During automatic copying, an unchanged
UID baseline may permit safe retry; otherwise that source requires manual review.
Reconciliation may download
same-size candidate messages. It never writes to Gmail.

If there is no match, first check Gmail manually and allow any delayed operation
to settle. Only after confirming that the uncertain message is absent:

```bash
python migrate-imap-account-to-gmail.py reconcile --account first@example.com --retry-missing
python migrate-imap-account-to-gmail.py copy --account first@example.com
```

`--retry-missing` clears unmatched pending records for the selected accounts so
they can be attempted again. It does not itself upload mail. Do not use this option
if Gmail changed the representation of an existing message: a different hash does
not prove absence. Multiple matches or a changed UID validity require manual
investigation; the script will not automatically delete or merge anything.

## 4. Final verification

After a final catch-up copy, run:

```bash
python migrate-imap-account-to-gmail.py verify
```

Verification reads source folders afresh and checks every in-scope source UID
against both the ledger and Gmail. It checks destination UID validity, existence,
size, internal date, and supported flags captured at upload time. It reports
missing uploads/messages, unexpected destination UIDs, shared destination UIDs,
and source records that disappeared or moved out of scope. Counts are messages,
not Gmail conversation threads. New arrivals after the snapshot need another
catch-up pass.

Each verification failure includes `target_uid`, `expected_size` (bytes recorded
at upload), and `actual_size` (Gmail's current size, or null if unavailable).
Verification fetches only Message-ID headers for failed messages, in batches,
using BODY.PEEK so read flags are preserved. Failures include `message_id` from
the source, `gmail_message_id` from Gmail, and a `gmail_search` query to paste into
Gmail's search box. The query combines the destination label and `rfc822msgid`,
preferring Gmail's current Message-ID. Missing IDs are null; messages without an
ID on either side have no generated query. Multiple messages with the same
Message-ID may appear in search results.

The default check does **not** prove body/attachment equality. Manually inspect
samples from every account. For a full raw-content check, add `--deep`, which
redownloads all recorded destination messages and compares their hashes with the
source bytes captured during copying. Gmail representation changes may trigger
size/hash differences; investigate them rather than treating them as success.
Verification does not change message flags or repair/delete messages.

Completion means a full `verify` run without `--uploaded-only`, no discrepancies,
and acceptance of the manual samples. Retain the source accounts or independent
backups for at least 30 days after acceptance. Future delivery/forwarding and
send-as configuration are separate from this archive migration.

## Repair missing recorded messages

Use a completed full verification JSON report (a result of `incomplete` is normal
when discrepancies were found; a report with an execution error is rejected).
Repair considers only `destination message missing` failures. Size, date, and
flag differences are left for review.

Preview first; this is the default and does not change Gmail or the ledger:

```bash
python migrate-imap-account-to-gmail.py repair --verify-report logs/<verify-run-id>-report.json
```

Repair rechecks the recorded UID, reads the source with BODY.PEEK, and checks its
UIDVALIDITY and saved SHA-256 hash. It searches the migration label plus Gmail's
All Mail, Spam, and Trash for the current source Message-ID. All three special
mailboxes must be visible through IMAP; their names are discovered from special-use
flags, so localized names work. Identical occurrences across labels are grouped
by Gmail's X-GM-MSGID. An exact raw hash is required to reuse a message; matching
Message-IDs alone are not enough. Changed source messages, absent Message-IDs,
unreadable candidates, multiple exact Gmail copies, and candidates with different
raw content are reported as `ambiguous` and skipped. Added Gmail headers can also
make a candidate ambiguous; investigate rather than uploading it blindly.

The JSON report and `repair_message` events show `already_present`,
`found_elsewhere`, `confirmed_absent`, or `ambiguous`, with a `repair_summary`.
Review the preview, then apply clear cases:

```bash
python migrate-imap-account-to-gmail.py repair --verify-report logs/<verify-run-id>-report.json --apply
python migrate-imap-account-to-gmail.py verify
```

`--account` can restrict repair to selected sources. Repair runs sequentially.
Applying first backs up the SQLite ledger to a private
`<state-file>.repair-<id>.bak` file, recorded as `state_backup` in the report.
For an existing exact match, it adds the original migration label and updates
the ledger to the label's current UID (`restored`), retaining other labels.
It does not remove Spam/Trash labels or merge source records onto an already
claimed UID. Confirmed absent messages are uploaded from the source with their
original recorded flags/date (`uploaded`). Every new APPEND is durably journaled
as pending before sending. If acknowledgement is lost, stop and use ordinary
copy/reconcile recovery before repeating repair; never clear the pending record
blindly. Re-running repair rechecks live state and skips repaired UIDs that are
already present. Normal verification is still needed afterward.

## Logs and reports

Every command writes timestamped `logs/<run-id>.jsonl` events and
`logs/<run-id>-report.json`. The final console event identifies the report.
Logs include account/folder/UID identifiers, upload outcomes and byte counts,
exclusions, and verification problems. Each IMAP operation writes `imap_start`
and `imap_complete` events with the server/account, session ID, command sequence,
elapsed time, session age, idle time, and prior session upload counts/bytes.
Connection setup and login are logged separately. APPEND records message size and
destination, and FETCH records UID identifiers. Verification failure events and
reports include Message-IDs and Gmail search queries, but no subjects or bodies.

Failures write `imap_error` with the sanitized server/network error text, exception
type, OS error number or TLS details when available, chained errors, and traceback
file/function/line locations. The final report's `diagnostics` field preserves the
failing operation and error. A failed logout is logged separately and never replaces
the original failure. `run_start` also records pacing settings and Python version.
These records can distinguish an EOF/reset, timeout, server rejection or quota
message, and show whether failure correlates with session age or upload volume.
If the server closes without providing a reason, logs cannot prove its internal
cause.

Configured passwords are redacted; protocol errors containing unexpected wire
responses or apparent message payloads are omitted. No IMAP wire/debug transcript,
login arguments, message subjects/bodies, traceback source lines or locals are
logged. Errors outside instrumented IMAP operations retain their exception type
and traceback locations, with arbitrary error text omitted.

Exit status is 0 for a completed command, 1 for errors or verification problems,
and 130 for Ctrl-C. An intentionally capped pilot exits 0 with `result: limited`;
it is not a completed full migration. Reports record `uploaded_only` and `deep`
so the verification scope is explicit. Abrupt process/machine termination may
prevent the final report, but committed SQLite checkpoints remain available.
New files are private to the current user. Back up the database while no command
is running, along with the configuration and reports. Logs contain addresses and
folder names, so keep them private too.

## Offline tests

```bash
python -m unittest discover -s tests -v
```

These use simulated IMAP servers and no real credentials. They cover safe reads,
exclusions, limits, multi-account resume, uncertain uploads, reconciliation, and
verification failures. A real Gmail pilot is still required before the full copy.

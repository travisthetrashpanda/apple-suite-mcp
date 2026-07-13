#!/usr/bin/env python3
"""Apple Suite MCP server.

Exposes Apple Mail, Contacts, Calendar, Notes, and Reminders as MCP tools
on macOS.

- Mail / Contacts / Notes are driven through the apps via AppleScript / JXA,
  so every account configured in the apps (iCloud, Gmail, Google Workspace,
  etc.) is visible with no extra logins.
- Calendar / Reminders use EventKit (Apple's native database API), which is
  hundreds of times faster than scripting the Calendar app.
- Mail listing/search additionally has a fast path that reads Mail's local
  SQLite index directly (requires Full Disk Access); it falls back to
  AppleScript automatically when unavailable.

Run `python server.py check` (or the check_access tool) to diagnose
permissions.
"""

import glob
import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("apple-suite")

# Large mailboxes (tens of thousands of messages) can take ~30s per
# scripting call; AppleScript blocks carry their own 600s timeout.
OSA_TIMEOUT = 630

# Control-character field/record separators used to move structured data
# out of AppleScript without colliding with message content. The clean()
# AppleScript handler strips them from content fields before joining.
FS = "\x1f"
RS = "\x1e"

TRIAGE_BATCH_LIMIT = 50


# ---------------------------------------------------------------------------
# osascript helpers
# ---------------------------------------------------------------------------

def run_osascript(script: str, lang: str = "AppleScript", timeout: int = OSA_TIMEOUT) -> str:
    cmd = ["osascript"]
    if lang == "JavaScript":
        cmd += ["-l", "JavaScript"]
    cmd += ["-e", script]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            f"The app didn't respond within {timeout}s — it may be busy "
            "syncing (common right after Mail launches) or waiting on a "
            "permission dialog. Try again in a minute."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"osascript error: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def run_jxa_json(script: str, timeout: int = OSA_TIMEOUT):
    out = run_osascript(script, lang="JavaScript", timeout=timeout)
    return json.loads(out) if out else None


def as_quote(s: str) -> str:
    """Escape a string for an AppleScript double-quoted literal.

    Raw newlines are legal inside AppleScript string literals, so only
    backslashes and quotes need escaping.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def js(v) -> str:
    """Encode a Python value as a JavaScript literal."""
    return json.dumps(v)


def parse_dt(s: str, what: str = "date") -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        raise ValueError(
            f'Invalid {what} "{s}" — use ISO format like "2026-07-15" or '
            '"2026-07-15T09:00"'
        )


def parse_rows(raw: str, fields: list[str]) -> list[dict]:
    rows = []
    for rec in raw.split(RS):
        if not rec:
            continue
        vals = rec.split(FS)
        if len(vals) != len(fields):
            # Can only happen if a delimiter slipped past clean(); skip
            # the malformed row rather than misalign or crash.
            continue
        rows.append(dict(zip(fields, vals)))
    return rows


# Shared AppleScript handlers: ISO dates, null-safe text, and delimiter
# stripping (clean) so message content can't break row parsing.
AS_HANDLERS = """
on pad2(n)
    set t to n as string
    if (length of t) < 2 then set t to "0" & t
    return t
end pad2

on isoDate(d)
    if d is missing value then return ""
    return (year of d as string) & "-" & my pad2(month of d as integer) & "-" & my pad2(day of d) & "T" & my pad2(hours of d) & ":" & my pad2(minutes of d) & ":" & my pad2(seconds of d)
end isoDate

on txt(v)
    if v is missing value then return ""
    return v as string
end txt

on clean(v)
    set s to my txt(v)
    set AppleScript's text item delimiters to character id 31
    set s to (text items of s)
    set AppleScript's text item delimiters to " "
    set s to s as string
    set AppleScript's text item delimiters to character id 30
    set s to (text items of s)
    set AppleScript's text item delimiters to " "
    set s to s as string
    set AppleScript's text item delimiters to ""
    return s
end clean
"""

MSG_FIELDS = ["id", "subject", "sender", "date", "read"]


def _mail_batch_script(account: str, mailbox: str, limit: int,
                       whose: str = None) -> str:
    """AppleScript that batch-fetches message headers in a single Apple event
    and returns FS/RS-delimited rows.

    Without `whose`, fetches the newest `limit` messages (index 1 = newest).
    With `whose`, fetches everything matching the filter, then caps at
    `limit`. The mailbox is resolved and counted OUTSIDE any try block so a
    mistyped account/mailbox name errors loudly instead of returning [].
    """
    if whose:
        fetch = f"""
        try
            set batch to (get {{id, subject, sender, date received, read status}} of (every message of mb whose {whose}))
        on error
            return ""
        end try"""
    else:
        fetch = f"""
        set n to {int(limit)}
        if n > total then set n to total
        if n < 1 then return ""
        set batch to (get {{id, subject, sender, date received, read status}} of messages 1 thru n of mb)"""
    return f"""
{AS_HANDLERS}
set FS to character id 31
set RS to character id 30
set outText to ""
with timeout of 600 seconds
    tell application "Mail"
        set mb to mailbox "{as_quote(mailbox)}" of account "{as_quote(account)}"
        set total to count of messages of mb
{fetch}
        set ids to item 1 of batch
        set subs to item 2 of batch
        set sndrs to item 3 of batch
        set dts to item 4 of batch
        set rds to item 5 of batch
        set n to count of ids
        if n > {int(limit)} then set n to {int(limit)}
        repeat with i from 1 to n
            set outText to outText & my txt(item i of ids) & FS & my clean(item i of subs) & FS & my clean(item i of sndrs) & FS & my isoDate(item i of dts) & FS & my txt(item i of rds) & RS
        end repeat
    end tell
end timeout
return outText
"""


def _rows_from_script(script: str) -> list[dict]:
    rows = parse_rows(run_osascript(script), MSG_FIELDS)
    for r in rows:
        r["read"] = r["read"] == "true"
        r["id"] = int(r["id"])
    return rows


# ---------------------------------------------------------------------------
# Mail fast path: read Mail's local SQLite index (Envelope Index) directly.
# Requires Full Disk Access; every entry point degrades to AppleScript when
# the database is unreadable or the schema surprises us.
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()
_db_state = {"conn": None, "cols": None, "epoch": 0, "mbx": None, "probed": False}


def find_envelope_index() -> str | None:
    hits = sorted(glob.glob(os.path.expanduser(
        "~/Library/Mail/V*/MailData/Envelope Index")))
    return hits[-1] if hits else None


def mail_db():
    """Return a read-only connection to the Envelope Index, or None if
    unavailable (no Full Disk Access, no Mail data, unexpected schema)."""
    with _db_lock:
        if _db_state["conn"] is not None:
            return _db_state["conn"]
        path = find_envelope_index()
        if not path:
            return None
        try:
            uri = "file:" + urllib.parse.quote(path) + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            if not {"subject", "sender", "mailbox", "date_received"} <= cols:
                conn.close()
                return None
            mx = conn.execute("SELECT MAX(date_received) FROM messages").fetchone()[0] or 0
            # date_received is unix epoch on modern macOS; older versions
            # used Apple's 2001 epoch. A recent timestamp disambiguates.
            _db_state["epoch"] = 0 if mx > 1.4e9 else 978307200
            _db_state["cols"] = cols
            _db_state["conn"] = conn
            return conn
        except Exception:
            return None


def _account_identities() -> list[dict]:
    """Account names with their emails/usernames, fetched once via JXA."""
    script = """
const Mail = Application("Mail");
JSON.stringify(Mail.accounts().map(a => {
  let user = null;
  try { user = a.userName(); } catch (e) {}
  return {name: a.name(), emails: a.emailAddresses() || [], user: user};
}));
"""
    return run_jxa_json(script)


def _mailbox_map(conn) -> list[dict]:
    """Map Envelope Index mailboxes to Mail account names by matching the
    username embedded in each mailbox URL against account emails."""
    if _db_state["mbx"] is not None:
        return _db_state["mbx"]
    idents = []
    for a in _account_identities():
        keys = {e.lower() for e in a["emails"]}
        if a.get("user"):
            keys.add(a["user"].lower())
        idents.append((a["name"], keys))
    out = []
    for row in conn.execute("SELECT ROWID, url FROM mailboxes"):
        url = row["url"] or ""
        parsed = urllib.parse.urlparse(url)
        user = urllib.parse.unquote(parsed.username or "").lower()
        account = None
        for name, keys in idents:
            if user and user in keys:
                account = name
                break
        segs = [urllib.parse.unquote(s) for s in parsed.path.split("/") if s]
        out.append({
            "rowid": row["ROWID"],
            "account": account,
            "path": "/".join(segs),
            "name": segs[-1] if segs else "",
        })
    _db_state["mbx"] = out
    return out


def _mailbox_rowids(conn, account: str | None, mailbox: str | None) -> list[int]:
    ids = []
    for m in _mailbox_map(conn):
        if account and (m["account"] or "").lower() != account.lower():
            continue
        if mailbox and mailbox != "*":
            if m["name"].lower() != mailbox.lower() and m["path"].lower() != mailbox.lower():
                continue
        if m["account"] is None and account is None:
            continue  # skip local/system stores in cross-account queries
        ids.append(m["rowid"])
    return ids


def _fast_query(conn, where: str, params: list, limit: int) -> list[dict]:
    cols = _db_state["cols"]
    read_expr = "m.read" if "read" in cols else "(m.flags & 1)"
    deleted = "AND m.deleted = 0" if "deleted" in cols else ""
    q = f"""
        SELECT m.ROWID AS id, s.subject AS subject, a.address AS address,
               a.comment AS comment, m.date_received AS ts,
               {read_expr} AS read_flag, m.mailbox AS mbx
        FROM messages m
        LEFT JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        WHERE {where} {deleted}
        ORDER BY m.date_received DESC LIMIT ?
    """
    by_rowid = {m["rowid"]: m for m in _mailbox_map(conn)}
    rows = []
    for r in conn.execute(q, params + [int(limit)]):
        sender = (f'{r["comment"]} <{r["address"]}>'
                  if r["comment"] else (r["address"] or ""))
        mb = by_rowid.get(r["mbx"], {})
        rows.append({
            "id": r["id"],
            "subject": r["subject"] or "",
            "sender": sender,
            "date": datetime.fromtimestamp(
                (r["ts"] or 0) + _db_state["epoch"]).isoformat(timespec="seconds"),
            "read": bool(r["read_flag"]),
            "account": mb.get("account"),
            "mailbox": mb.get("name"),
        })
    return rows


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def fast_mail_messages(account, mailbox, limit, unread_only):
    """Fast-path message listing; returns None to signal AppleScript fallback."""
    try:
        conn = mail_db()
        if conn is None:
            return None
        with _db_lock:
            rowids = _mailbox_rowids(conn, account, mailbox)
            if not rowids:
                return None
            where = f"m.mailbox IN ({','.join('?' * len(rowids))})"
            params = list(rowids)
            if unread_only:
                cols = _db_state["cols"]
                where += " AND " + ("m.read = 0" if "read" in cols else "(m.flags & 1) = 0")
            return _fast_query(conn, where, params, limit)
    except Exception:
        return None


def fast_mail_search(account, query, field, mailbox, limit):
    """Fast-path search; returns None to signal AppleScript fallback."""
    try:
        conn = mail_db()
        if conn is None:
            return None
        with _db_lock:
            rowids = _mailbox_rowids(conn, account, mailbox)
            if not rowids:
                return None
            where = f"m.mailbox IN ({','.join('?' * len(rowids))})"
            params: list = list(rowids)
            if field == "subject":
                where += r" AND s.subject LIKE ? ESCAPE '\'"
                params.append(_like(query))
            else:
                where += r" AND (a.address LIKE ? ESCAPE '\' OR a.comment LIKE ? ESCAPE '\')"
                params += [_like(query), _like(query)]
            return _fast_query(conn, where, params, limit)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Mail tools
# ---------------------------------------------------------------------------

@mcp.tool()
def mail_accounts() -> list[dict]:
    """List every account configured in Apple Mail (iCloud, Gmail, Google
    Workspace, etc.) with its email addresses and mailbox (folder) names."""
    script = """
const Mail = Application("Mail");
JSON.stringify(Mail.accounts().map(a => {
  let mailboxes = [];
  try { mailboxes = a.mailboxes.name(); } catch (e) {}
  return {
    name: a.name(),
    emails: a.emailAddresses(),
    enabled: a.enabled(),
    mailboxes: mailboxes,
  };
}));
"""
    return run_jxa_json(script)


@mcp.tool()
def mail_unread_counts() -> list[dict]:
    """Get the unread message count for each account's inbox, plus the
    combined total. Fast — use this before pulling message lists."""
    script = """
const Mail = Application("Mail");
const out = [];
for (const a of Mail.accounts()) {
  let unread = null;
  try { unread = a.mailboxes.byName("INBOX").unreadCount(); } catch (e) {}
  out.push({account: a.name(), unread: unread});
}
out.push({account: "ALL_INBOXES", unread: Mail.inbox.unreadCount()});
JSON.stringify(out);
"""
    return run_jxa_json(script)


@mcp.tool()
def mail_messages(account: str, mailbox: str = "INBOX", limit: int = 20,
                  unread_only: bool = False) -> list[dict]:
    """List recent messages in a mailbox, newest first. Returns id, subject,
    sender, date, and read status. Uses the fast local-index path when Full
    Disk Access is granted; otherwise AppleScript (large mailboxes ~30s)."""
    limit = max(1, min(int(limit), 100))
    fast = fast_mail_messages(account, mailbox, limit, unread_only)
    if fast is not None:
        return fast
    whose = "read status is false" if unread_only else None
    return _rows_from_script(_mail_batch_script(account, mailbox, limit, whose))


@mcp.tool()
def mail_search(account: str = None, query: str = "", field: str = "subject",
                mailbox: str = "INBOX", limit: int = 20) -> list[dict]:
    """Search messages whose subject or sender contains the query
    (case-insensitive). field is "subject" or "sender". account=None
    searches across ALL accounts (requires the fast path / Full Disk
    Access). mailbox="*" searches every folder of the account."""
    if field not in ("subject", "sender"):
        raise ValueError('field must be "subject" or "sender"')
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 100))
    fast = fast_mail_search(account, query, field, mailbox, limit)
    if fast is not None:
        return fast
    if account is None:
        raise RuntimeError(
            "Searching across all accounts needs the fast mail path, which "
            "requires Full Disk Access for the host app (System Settings > "
            "Privacy & Security > Full Disk Access). Or pass a specific "
            "account name."
        )
    if mailbox == "*":
        raise RuntimeError(
            'mailbox="*" needs the fast mail path (Full Disk Access). '
            "Or pass a specific mailbox name."
        )
    whose = f'{field} contains "{as_quote(query)}"'
    return _rows_from_script(_mail_batch_script(account, mailbox, limit, whose))


@mcp.tool()
def mail_read_message(account: str, message_id: int, mailbox: str = "INBOX",
                      max_chars: int = 20000) -> dict:
    """Read the full content of one message by the id returned from
    mail_messages / mail_search."""
    script = f"""
{AS_HANDLERS}
set FS to character id 31
with timeout of 600 seconds
    tell application "Mail"
        set mb to mailbox "{as_quote(mailbox)}" of account "{as_quote(account)}"
        set matches to (every message of mb whose id is {int(message_id)})
        if (count of matches) is 0 then error "No message with id {int(message_id)} in that mailbox (it may have been moved by another mail app)"
        set msg to item 1 of matches
        set toAddrs to ""
        repeat with r in to recipients of msg
            set toAddrs to toAddrs & (address of r) & ", "
        end repeat
        set ccAddrs to ""
        repeat with r in cc recipients of msg
            set ccAddrs to ccAddrs & (address of r) & ", "
        end repeat
        set c to my txt(content of msg)
        if (length of c) > {int(max_chars)} then set c to text 1 thru {int(max_chars)} of c
        return my clean(subject of msg) & FS & my clean(sender of msg) & FS & my isoDate(date received of msg) & FS & toAddrs & FS & ccAddrs & FS & c
    end tell
end timeout
"""
    fields = ["subject", "sender", "date", "to", "cc", "content"]
    vals = run_osascript(script).split(FS, len(fields) - 1)
    out = dict(zip(fields, vals))
    out["to"] = out.get("to", "").rstrip(", ")
    out["cc"] = out.get("cc", "").rstrip(", ")
    return out


@mcp.tool()
def mail_send(to: list[str], subject: str, body: str, cc: list[str] = None,
              from_account_email: str = None) -> str:
    """Send an email through Apple Mail. from_account_email selects which
    account sends it (must match an address from mail_accounts); omit for
    the default. Always confirm with the user before sending."""
    if not to:
        raise ValueError("'to' must contain at least one recipient address")
    sender_line = ""
    if from_account_email:
        known = [e for a in _account_identities() for e in a["emails"]]
        if from_account_email.lower() not in {e.lower() for e in known}:
            raise ValueError(
                f"'{from_account_email}' doesn't match any Mail account "
                f"address. Known addresses: {', '.join(sorted(known))}"
            )
        sender_line = f'    set sender of msg to "{as_quote(from_account_email)}"'
    recip_lines = "\n".join(
        f'        make new to recipient at end of to recipients with properties {{address:"{as_quote(a)}"}}'
        for a in to
    )
    cc_lines = "\n".join(
        f'        make new cc recipient at end of cc recipients with properties {{address:"{as_quote(a)}"}}'
        for a in (cc or [])
    )
    script = f"""
with timeout of 120 seconds
tell application "Mail"
    set msg to make new outgoing message with properties {{subject:"{as_quote(subject)}", content:"{as_quote(body)}", visible:false}}
    tell msg
{recip_lines}
{cc_lines}
    end tell
{sender_line}
    send msg
end tell
end timeout
return "sent"
"""
    return run_osascript(script)


def _triage_script(account: str, mailbox: str, message_ids: list[int],
                   action: str) -> str:
    """One Apple event resolves all ids via an OR-chained whose filter, then
    acts on each match. Returns the ids actually touched (FS-joined)."""
    chain = " or ".join(f"(id is {int(i)})" for i in message_ids)
    return f"""
set FS to character id 31
set outText to ""
with timeout of 600 seconds
    tell application "Mail"
        set mb to mailbox "{as_quote(mailbox)}" of account "{as_quote(account)}"
        set msgs to (every message of mb whose {chain})
        repeat with m in msgs
            set outText to outText & ((id of m) as string) & FS
            {action}
        end repeat
    end tell
end timeout
return outText
"""


def _run_triage(account, mailbox, message_ids, action) -> dict:
    if not message_ids:
        raise ValueError("message_ids must not be empty")
    if len(message_ids) > TRIAGE_BATCH_LIMIT:
        raise ValueError(f"Max {TRIAGE_BATCH_LIMIT} ids per call — split into batches")
    out = run_osascript(_triage_script(account, mailbox, message_ids, action))
    touched = sorted(int(i) for i in out.split(FS) if i)
    missing = sorted(set(int(i) for i in message_ids) - set(touched))
    return {"updated": touched, "not_found": missing}


@mcp.tool()
def mail_mark_read(account: str, message_ids: list[int],
                   mailbox: str = "INBOX", read: bool = True) -> dict:
    """Mark messages read (or unread with read=false) by id, in one batch
    (max 50). Ids not found are reported back, not errors — messages may
    have been moved by another mail client."""
    action = f"set read status of m to {'true' if read else 'false'}"
    return _run_triage(account, mailbox, message_ids, action)


@mcp.tool()
def mail_move(account: str, message_ids: list[int], to_mailbox: str,
              mailbox: str = "INBOX") -> dict:
    """Move messages by id from one mailbox to another within the same
    account (e.g. to "Archive"), in one batch (max 50)."""
    action = (f'move m to mailbox "{as_quote(to_mailbox)}" '
              f'of account "{as_quote(account)}"')
    return _run_triage(account, mailbox, message_ids, action)


@mcp.tool()
def mail_trash(account: str, message_ids: list[int],
               mailbox: str = "INBOX") -> dict:
    """Move messages to the account's Trash by id, in one batch (max 50).
    This is Mail's normal reversible delete — there is deliberately no
    permanent-delete tool. Confirm with the user before large sweeps."""
    return _run_triage(account, mailbox, message_ids, "delete m")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@mcp.tool()
def contacts_search(query: str, limit: int = 10) -> list[dict]:
    """Search Contacts by name, organization, email, or phone (substring,
    case-insensitive). Returns full contact details."""
    script = f"""
const C = Application("Contacts");
const q = {js(query)}.toLowerCase();
const limit = {int(limit)};
const names = C.people.name();
const orgs = C.people.organization();
const emails = C.people.emails.value();
const phones = C.people.phones.value();
if (new Set([names.length, orgs.length, emails.length, phones.length]).size !== 1)
  throw new Error("Contacts changed while reading — please retry");
const idx = [];
for (let i = 0; i < names.length && idx.length < limit; i++) {{
  const hay = [names[i] || "", orgs[i] || "",
               (emails[i] || []).join(" "), (phones[i] || []).join(" ")
              ].join(" ").toLowerCase();
  if (hay.includes(q)) idx.push(i);
}}
const out = idx.map(i => {{
  const p = C.people[i];
  let bday = null;
  try {{
    const b = p.birthDate();
    if (b) {{
      const mm = String(b.getMonth() + 1).padStart(2, "0");
      const dd = String(b.getDate()).padStart(2, "0");
      // Contacts uses year 1604 as the "no year given" sentinel.
      bday = (b.getFullYear() <= 1604) ? `--${{mm}}-${{dd}}`
                                       : `${{b.getFullYear()}}-${{mm}}-${{dd}}`;
    }}
  }} catch (e) {{}}
  let addrs = [];
  try {{ addrs = p.addresses().map(a => a.formattedAddress()).filter(Boolean); }} catch (e) {{}}
  return {{
    name: names[i],
    organization: orgs[i],
    emails: emails[i] || [],
    phones: phones[i] || [],
    birthday: bday,
    addresses: addrs,
    note: (() => {{ try {{ return p.note(); }} catch (e) {{ return null; }} }})(),
  }};
}});
JSON.stringify(out);
"""
    return run_jxa_json(script)


# ---------------------------------------------------------------------------
# EventKit (Calendar + Reminders)
# ---------------------------------------------------------------------------

_ek_store = None
_ek_lock = threading.Lock()

EK_STATUS = {0: "notDetermined", 1: "restricted", 2: "denied",
             3: "fullAccess", 4: "writeOnly"}

SETTINGS_HINT = (
    "Grant Full Access in System Settings > Privacy & Security > {kind} for "
    "the app running this server (e.g. Claude), then retry."
)


def ek():
    global _ek_store
    import EventKit
    with _ek_lock:
        if _ek_store is None:
            _ek_store = EventKit.EKEventStore.alloc().init()
    return _ek_store


def ek_status(entity: str) -> int:
    import EventKit
    etype = (EventKit.EKEntityTypeEvent if entity == "event"
             else EventKit.EKEntityTypeReminder)
    return EventKit.EKEventStore.authorizationStatusForEntityType_(etype)


def ensure_full_access(entity: str):
    """entity: 'event' or 'reminder'. Requests access if undetermined;
    raises with instructions if not granted."""
    store = ek()
    status = ek_status(entity)
    if status == 0:  # not determined -> triggers the macOS prompt
        done = threading.Event()
        if entity == "event":
            store.requestFullAccessToEventsWithCompletion_(lambda g, e: done.set())
        else:
            store.requestFullAccessToRemindersWithCompletion_(lambda g, e: done.set())
        done.wait(120)
        status = ek_status(entity)
    if status != 3:
        kind = "Calendars" if entity == "event" else "Reminders"
        raise RuntimeError(
            f"{kind} access is '{EK_STATUS.get(status, status)}', need Full "
            f"Access. " + SETTINGS_HINT.format(kind=kind)
        )
    return store


def nsdate(dt: datetime):
    import Foundation
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def iso(nsd) -> str | None:
    if nsd is None:
        return None
    return datetime.fromtimestamp(nsd.timeIntervalSince1970()).isoformat(timespec="minutes")


@mcp.tool()
def calendar_list() -> list[dict]:
    """List all calendars with their account/source and whether events can
    be added to them."""
    import EventKit
    store = ensure_full_access("event")
    cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
    return [{
        "title": c.title(),
        "source": c.source().title() if c.source() else None,
        "writable": bool(c.allowsContentModifications()),
    } for c in cals]


@mcp.tool()
def calendar_events(start: str, end: str, calendars: list[str] = None) -> list[dict]:
    """List events between two ISO dates/datetimes (e.g. "2026-07-12" or
    "2026-07-12T09:00"). Optionally restrict to specific calendar titles.
    End date is exclusive-ish: use the day after the last day you want."""
    import EventKit
    store = ensure_full_access("event")
    all_cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
    cal_filter = None
    if calendars:
        wanted = {c.lower() for c in calendars}
        cal_filter = [c for c in all_cals if c.title().lower() in wanted]
        if not cal_filter:
            raise ValueError(f"No calendars match {calendars}. "
                             f"Available: {[c.title() for c in all_cals]}")
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        nsdate(parse_dt(start, "start")),
        nsdate(parse_dt(end, "end")),
        cal_filter,
    )
    events = store.eventsMatchingPredicate_(pred) or []
    events = sorted(events, key=lambda e: e.startDate().timeIntervalSince1970())
    return [{
        "id": e.eventIdentifier(),
        "title": e.title(),
        "calendar": e.calendar().title() if e.calendar() else None,
        "start": iso(e.startDate()),
        "end": iso(e.endDate()),
        "all_day": bool(e.isAllDay()),
        "location": e.location(),
        "notes": e.notes(),
    } for e in events]


@mcp.tool()
def calendar_create_event(title: str, start: str, end: str,
                          calendar: str = None, location: str = None,
                          notes: str = None, all_day: bool = False) -> dict:
    """Create a calendar event. start/end are ISO datetimes (e.g.
    "2026-07-14T15:00"). calendar is a calendar title from calendar_list;
    omit for the default calendar. Confirm details with the user first."""
    import EventKit
    store = ek()
    # Creating works even under write-only access, so don't demand full.
    target = None
    if calendar:
        cals = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        for c in cals:
            if c.title().lower() == calendar.lower() and c.allowsContentModifications():
                target = c
                break
        if target is None:
            raise ValueError(
                f"No writable calendar named '{calendar}' is visible. "
                + SETTINGS_HINT.format(kind="Calendars")
            )
    else:
        target = store.defaultCalendarForNewEvents()
    ev = EventKit.EKEvent.eventWithEventStore_(store)
    ev.setTitle_(title)
    ev.setStartDate_(nsdate(parse_dt(start, "start")))
    ev.setEndDate_(nsdate(parse_dt(end, "end")))
    ev.setCalendar_(target)
    ev.setAllDay_(all_day)
    if location:
        ev.setLocation_(location)
    if notes:
        ev.setNotes_(notes)
    ok, err = store.saveEvent_span_error_(ev, EventKit.EKSpanThisEvent, None)
    if not ok:
        raise RuntimeError(f"Failed to save event: {err}")
    return {"created": title, "calendar": target.title(),
            "start": start, "end": end, "id": ev.eventIdentifier()}


# ---------------------------------------------------------------------------
# Reminders (EventKit)
# ---------------------------------------------------------------------------

def _fetch_reminders(store, pred):
    done = threading.Event()
    box = {}

    def cb(reminders):
        box["r"] = reminders
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(pred, cb)
    if not done.wait(60):
        raise TimeoutError("Reminders didn't respond within 60s — try again")
    return list(box.get("r") or [])


def _due_iso(r):
    dd = r.dueDateComponents()
    if dd is None:
        return None
    d = dd.date()
    return iso(d) if d else None


@mcp.tool()
def reminders_list(list_name: str = None, include_completed: bool = False) -> list[dict]:
    """List reminders, open ones by default. Optionally filter to one list
    (e.g. "Family"). Returns id, title, due date, notes, and list."""
    import EventKit
    store = ensure_full_access("reminder")
    cals = store.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
    if list_name:
        cals = [c for c in cals if c.title().lower() == list_name.lower()]
        if not cals:
            raise ValueError(f"No reminder list named '{list_name}'")
    if include_completed:
        pred = store.predicateForRemindersInCalendars_(cals)
    else:
        pred = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
            None, None, cals)
    out = []
    for r in _fetch_reminders(store, pred):
        out.append({
            "id": r.calendarItemIdentifier(),
            "title": r.title(),
            "list": r.calendar().title() if r.calendar() else None,
            "due": _due_iso(r),
            "notes": r.notes(),
            "completed": bool(r.isCompleted()),
            "priority": r.priority(),
        })
    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return out


@mcp.tool()
def reminders_create(title: str, list_name: str = None, due: str = None,
                     notes: str = None) -> dict:
    """Create a reminder. due is an ISO date or datetime (e.g. "2026-07-15"
    or "2026-07-15T09:00"). list_name defaults to the default list."""
    import EventKit
    import Foundation
    store = ensure_full_access("reminder")
    target = store.defaultCalendarForNewReminders()
    if list_name:
        cals = store.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
        matches = [c for c in cals if c.title().lower() == list_name.lower()]
        if not matches:
            raise ValueError(f"No reminder list named '{list_name}'")
        target = matches[0]
    r = EventKit.EKReminder.reminderWithEventStore_(store)
    r.setTitle_(title)
    r.setCalendar_(target)
    if notes:
        r.setNotes_(notes)
    if due:
        dt = parse_dt(due, "due")
        comps = Foundation.NSDateComponents.alloc().init()
        comps.setTimeZone_(Foundation.NSTimeZone.localTimeZone())
        comps.setYear_(dt.year)
        comps.setMonth_(dt.month)
        comps.setDay_(dt.day)
        if "T" in due:
            comps.setHour_(dt.hour)
            comps.setMinute_(dt.minute)
        r.setDueDateComponents_(comps)
        # An alarm makes the reminder actually fire a notification at the
        # due time instead of just showing a date.
        if "T" in due:
            r.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(nsdate(dt)))
    ok, err = store.saveReminder_commit_error_(r, True, None)
    if not ok:
        raise RuntimeError(f"Failed to save reminder: {err}")
    return {"created": title, "list": target.title(), "due": due,
            "id": r.calendarItemIdentifier()}


@mcp.tool()
def reminders_complete(reminder_id: str) -> dict:
    """Mark a reminder complete by the id returned from reminders_list."""
    import EventKit
    store = ensure_full_access("reminder")
    item = store.calendarItemWithIdentifier_(reminder_id)
    if item is None:
        raise ValueError(f"No reminder with id {reminder_id}")
    if not item.isKindOfClass_(EventKit.EKReminder):
        raise ValueError(f"Id {reminder_id} is not a reminder (it may be a "
                         "calendar event id)")
    item.setCompleted_(True)
    ok, err = store.saveReminder_commit_error_(item, True, None)
    if not ok:
        raise RuntimeError(f"Failed to complete reminder: {err}")
    return {"completed": item.title()}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@mcp.tool()
def notes_folders() -> list[dict]:
    """List Notes folders with note counts, per account."""
    script = """
const N = Application("Notes");
const out = [];
for (const acct of N.accounts()) {
  for (const f of acct.folders()) {
    out.push({account: acct.name(), folder: f.name(), notes: f.notes.length});
  }
}
JSON.stringify(out);
"""
    return run_jxa_json(script)


@mcp.tool()
def notes_search(query: str, search_body: bool = True, limit: int = 10) -> list[dict]:
    """Search notes by title (and body text unless search_body is false).
    Returns id, title, folder, modification date, and a snippet."""
    clause = (
        '{_or: [{name: {_contains: q}}, {plaintext: {_contains: q}}]}'
        if search_body else '{name: {_contains: q}}'
    )
    script = f"""
const N = Application("Notes");
const q = {js(query)};
const matches = N.notes.whose({clause});
const cnt = matches.length;
const out = [];
for (let i = 0; i < Math.min(cnt, {int(limit)}); i++) {{
  const nt = matches[i];
  let folder = null;
  try {{ folder = nt.container().name(); }} catch (e) {{}}
  out.push({{
    id: nt.id(),
    title: nt.name(),
    folder: folder,
    modified: nt.modificationDate().toISOString(),
    snippet: (nt.plaintext() || "").slice(0, 300),
  }});
}}
JSON.stringify({{total: cnt, results: out}});
"""
    return run_jxa_json(script)


@mcp.tool()
def notes_read(note_id: str = None, title: str = None, max_chars: int = 30000) -> dict:
    """Read a note's full text by id (from notes_search) or exact title."""
    if not note_id and not title:
        raise ValueError("Provide note_id or title")
    getter = (f"N.notes.byId({js(note_id)})" if note_id
              else f"N.notes.byName({js(title)})")
    script = f"""
const N = Application("Notes");
let out;
try {{
  const nt = {getter};
  out = {{
    id: nt.id(),
    title: nt.name(),
    modified: nt.modificationDate().toISOString(),
    text: (nt.plaintext() || "").slice(0, {int(max_chars)}),
  }};
}} catch (e) {{
  out = {{error: "not_found"}};
}}
JSON.stringify(out);
"""
    result = run_jxa_json(script)
    if result.get("error") == "not_found":
        raise ValueError(f"No note found with "
                         f"{'id ' + note_id if note_id else 'title ' + repr(title)}")
    return result


@mcp.tool()
def notes_create(title: str, body: str, folder: str = None) -> dict:
    """Create a note. body is plain text (line breaks preserved). folder is
    a folder name from notes_folders; omit for the default Notes folder."""
    import html
    body_html = "<div>" + html.escape(body).replace("\n", "<br>") + "</div>"
    target = (f"N.folders.byName({js(folder)})" if folder else "N.defaultAccount()")
    script = f"""
const N = Application("Notes");
const note = N.Note({{name: {js(title)}, body: {js(body_html)}}});
{target}.notes.push(note);
JSON.stringify({{created: note.name(), id: note.id()}});
"""
    return run_jxa_json(script)


# ---------------------------------------------------------------------------
# Permission self-diagnosis
# ---------------------------------------------------------------------------

PANE = "x-apple.systempreferences:com.apple.preference.security"

FIXES = {
    "automation": f"System Settings > Privacy & Security > Automation — allow the host app to control the target app ({PANE}?Privacy_Automation)",
    "calendars": f"System Settings > Privacy & Security > Calendars — set the host app to Full Access ({PANE}?Privacy_Calendars)",
    "reminders": f"System Settings > Privacy & Security > Reminders — set the host app to Full Access ({PANE}?Privacy_Reminders)",
    "full_disk": f"System Settings > Privacy & Security > Full Disk Access — enable the host app ({PANE}?Privacy_AllFiles)",
}


@mcp.tool()
def check_access() -> dict:
    """Diagnose every permission this server needs: Automation (Mail,
    Contacts, Notes), Calendar/Reminders access level, and Full Disk Access
    for the fast mail path. Each failure includes the System Settings pane
    that fixes it. Note: this launches the target apps if not running."""
    report: dict = {
        "macos": platform.mac_ver()[0],
        "python": sys.version.split()[0],
    }
    fixes = []

    probes = {"Mail": "Application('Mail').accounts.length",
              "Contacts": "Application('Contacts').people.length",
              "Notes": "Application('Notes').accounts.length"}
    automation = {}
    for app, probe in probes.items():
        try:
            run_osascript(probe, lang="JavaScript", timeout=15)
            automation[app] = "ok"
        except TimeoutError:
            automation[app] = ("timed out — app busy, or an Allow dialog "
                               "is waiting on screen")
        except RuntimeError as e:
            if "-1743" in str(e):
                automation[app] = "denied"
                fixes.append(FIXES["automation"])
            else:
                automation[app] = f"error: {e}"
    report["automation"] = automation

    cal = EK_STATUS.get(ek_status("event"), "unknown")
    rem = EK_STATUS.get(ek_status("reminder"), "unknown")
    report["calendars"] = cal
    report["reminders"] = rem
    if cal != "fullAccess":
        fixes.append(FIXES["calendars"])
    if rem != "fullAccess":
        fixes.append(FIXES["reminders"])

    if mail_db() is not None:
        report["full_disk_access"] = "ok — fast mail path active"
    else:
        report["full_disk_access"] = ("no access — mail queries fall back "
                                      "to AppleScript (slow on large mailboxes)")
        fixes.append(FIXES["full_disk"])

    report["fixes_needed"] = sorted(set(fixes)) if fixes else []
    report["ok"] = not fixes
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        print(json.dumps(check_access(), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # e.g. python server.py test mail_unread_counts '{"limit": 5}'
        fn = globals()[sys.argv[2]]
        kwargs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(fn(**kwargs), indent=2, ensure_ascii=False, default=str))
    else:
        mcp.run()


if __name__ == "__main__":
    main()

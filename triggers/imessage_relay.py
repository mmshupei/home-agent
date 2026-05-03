"""iMessage relay: watch ~/Library/Messages/chat.db for inbound messages from
known family handles, route them through the orchestrator, reply via AppleScript.

Requires Full Disk Access for the executing process (or its launchd parent).
See README for the System Settings → Privacy & Security setup steps.

State:
- Last processed ROWID is persisted in data/imessage_relay.state so restarts
  don't reprocess the backlog. On first run with no state file, the relay
  jumps to the current MAX(ROWID) — only NEW messages are handled.

Conversation context:
- The last 4 exchanges (8 messages) from the same handle within a 30-minute
  window are passed as prior turns. Beyond that, memory is the only bridge.

Safety:
- Only handles whose phone/email matches a row in `users.imessage_handle`
  are processed. Unknown senders are silently ignored.
- Reply mode defaults to mobile profile so L2/L3 actions route through
  Pushover approval — not an inline auto-allow.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from orchestrator import auth, loop
from orchestrator.db import connect, ensure_schema
from tools.applescript import messages as msg_shim

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "imessage_relay.state"
)

POLL_INTERVAL_SEC = 3.0
CONTEXT_WINDOW_MINS = 30
CONTEXT_TURNS = 4  # exchanges (= 2 * messages) carried into the prompt


def _load_last_rowid() -> Optional[int]:
    if not STATE_FILE.exists():
        return None
    try:
        return int(STATE_FILE.read_text().strip())
    except ValueError:
        return None


def _save_last_rowid(rowid: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(rowid))


def _open_chat_db() -> sqlite3.Connection:
    """Read-only connection. Raises on FDA denial."""
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _max_rowid(conn) -> int:
    r = conn.execute("SELECT COALESCE(MAX(ROWID), 0) AS m FROM message").fetchone()
    return int(r["m"])


def _new_messages(conn, since_rowid: int) -> list[dict]:
    """Inbound iMessage rows (is_from_me=0) after `since_rowid`, joined with
    sender handle. Filters out empty/sticker/etc. (text IS NULL)."""
    sql = """
    SELECT
      m.ROWID                          AS rowid,
      m.text                           AS text,
      m.date                           AS date_apple,
      h.id                             AS handle,
      h.service                        AS service
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    WHERE m.ROWID > ?
      AND m.is_from_me = 0
      AND m.text IS NOT NULL
      AND m.text != ''
    ORDER BY m.ROWID ASC
    """
    return [dict(r) for r in conn.execute(sql, (since_rowid,)).fetchall()]


def _resolve_principal(handle: str) -> Optional[auth.Principal]:
    """Look up the user whose imessage_handle matches this phone/email.

    A user may register multiple handles by storing them comma-separated
    (e.g. "+16692827917,mshupei@gmail.com") since iMessage routes inbound
    to whichever handle the sender used. We split and check each.

    Phone normalization: iMessage stores in E.164 ('+1...') but the same
    person may register without the country code, so we also match on the
    last 10 digits.
    """
    norm = "".join(c for c in handle if c.isdigit())
    last10 = norm[-10:] if len(norm) >= 10 else None

    with connect() as c:
        rows = c.execute(
            "SELECT id, name, role, imessage_handle FROM users "
            "WHERE imessage_handle IS NOT NULL AND imessage_handle != ''"
        ).fetchall()

    for r in rows:
        for raw in (r["imessage_handle"] or "").split(","):
            stored = raw.strip()
            if not stored:
                continue
            if stored == handle:
                return _make_principal(r)
            stored_digits = "".join(c for c in stored if c.isdigit())
            if last10 and len(stored_digits) >= 10 and stored_digits[-10:] == last10:
                return _make_principal(r)
            # email vs lowercase email
            if "@" in stored and stored.lower() == handle.lower():
                return _make_principal(r)
    return None


def _make_principal(row) -> auth.Principal:
    return auth.Principal(
        user_id=row["id"], name=row["name"], role=row["role"], token_label="imessage"
    )


# ---------------------------------------------------------------------------
# Conversation thread context
# ---------------------------------------------------------------------------

# In-memory rolling buffer per handle. Each entry: (timestamp, role, text)
# role ∈ {"them", "us"}. Lost on restart — that's fine for short threads.
_thread_buffer: dict[str, deque] = defaultdict(lambda: deque(maxlen=CONTEXT_TURNS * 2))


def _record_thread(handle: str, role: str, text: str) -> None:
    _thread_buffer[handle].append((time.time(), role, text))


def _thread_context(handle: str) -> str:
    cutoff = time.time() - CONTEXT_WINDOW_MINS * 60
    rows = [r for r in _thread_buffer[handle] if r[0] >= cutoff]
    if len(rows) <= 1:  # the message we're replying to is in there; alone = no context
        return ""
    lines = ["## Recent thread (this conversation, last 30m)"]
    for ts, role, text in rows[:-1]:  # exclude the current incoming msg
        who = "user" if role == "them" else "you (agent)"
        lines.append(f"- {who}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Relay loop
# ---------------------------------------------------------------------------


async def handle_message(
    msg: dict, *, profile: str, dry_run: bool, max_chars: int
) -> Optional[str]:
    handle = msg["handle"] or ""
    text = msg["text"] or ""

    principal = _resolve_principal(handle)
    if not principal:
        print(f"[skip] unknown handle {handle!r}: {text[:60]!r}")
        return None

    _record_thread(handle, "them", text)

    # Inject thread context as a prefix to the task; the orchestrator's
    # compose_prompt() handles the rest (memory, identity, cautions).
    thread = _thread_context(handle)
    task = (thread + "\n\n" + text) if thread else text

    print(f"[in ] {principal.name} via {handle}: {text[:80]!r}")

    if dry_run:
        return None

    try:
        reply = await loop.run(
            task=task,
            principal=principal,
            profile=profile,
            model="claude-opus-4-7",
        )
    except Exception as e:
        reply = f"(agent error: {type(e).__name__}: {e})"

    if reply and len(reply) > max_chars:
        reply = reply[: max_chars - 1] + "…"

    if reply:
        _record_thread(handle, "us", reply)
        result = await msg_shim.send(to=handle, body=reply)
        print(f"[out] -> {handle}: {result}")
    return reply


async def relay(*, profile: str, dry_run: bool, max_reply_chars: int = 1500):
    load_dotenv()
    ensure_schema()

    conn = _open_chat_db()  # raises if FDA missing — fail loud at startup
    last = _load_last_rowid()
    if last is None:
        last = _max_rowid(conn)
        _save_last_rowid(last)
        print(f"[init] no state file; jumping to current max ROWID={last}")
    else:
        print(f"[init] resuming from ROWID={last}")

    print(f"[init] profile={profile}  dry_run={dry_run}")

    while True:
        try:
            msgs = _new_messages(conn, last)
        except sqlite3.OperationalError as e:
            print(f"[chat.db] {e}; sleeping 30s and retrying")
            await asyncio.sleep(30)
            continue

        for m in msgs:
            await handle_message(
                m, profile=profile, dry_run=dry_run, max_chars=max_reply_chars
            )
            last = m["rowid"]
            _save_last_rowid(last)

        await asyncio.sleep(POLL_INTERVAL_SEC)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="imessage_relay")
    p.add_argument(
        "--profile", default="mobile",
        help="profile under which to run inbound messages (default: mobile)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="log inbound messages but don't reply (use to verify handle lookup)",
    )
    args = p.parse_args(argv)
    try:
        asyncio.run(relay(profile=args.profile, dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("\n[bye]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

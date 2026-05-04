"""Telegram bot relay.

Long-polling bot that routes inbound messages through the orchestrator and
replies in the same chat. Mirrors the iMessage relay's shape but without the
same-Apple-ID gotcha — each Telegram user is a distinct chat with the bot.

Setup (see README §Telegram bot for the walkthrough):
1. Talk to @BotFather → /newbot → copy token into TELEGRAM_BOT_TOKEN
2. `agent user link-telegram --user shupei` → tap the printed t.me link
3. Run this module; each registered user can now message the bot

Identity:
- A Telegram user is bound to an agent user_id via /start <link_token>.
- Without a linked row, inbound messages get a polite "ask the operator
  for a link" reply rather than being silently dropped.

Auth chain:
- principal_for_telegram(tg_uid) → orchestrator/auth.Principal
- Loop runs under the mobile profile (L2/L3 actions route to Pushover prompt)
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from orchestrator import approvals, auth, loop, memory as mem, resident as residents, threads
from orchestrator.db import connect, ensure_schema
from orchestrator.dream import proposals as dream_proposals
from orchestrator.scheduler.heartbeat import Heartbeat
from tools.sdk_mcp import scheduler_tools as scheduler_tool_registry
from triggers import telegram_format

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LINK_TOKEN_TTL_SEC = 5 * 60  # 5 minutes
REPLY_MAX_CHARS = 4000  # Telegram limit is 4096
THREAD_TURNS = 4
THREAD_WINDOW_MIN = 30


# ---------------------------------------------------------------------------
# Outbound formatter: hot-reloadable leaf module
# ---------------------------------------------------------------------------
# `triggers/telegram_format.py` is a pure-function module — no held state, no
# I/O. That makes it safe to reload in-place when its file changes on disk,
# without restarting the bot. This is the pilot for self-modification of
# leaf surfaces; anything that owns long-lived objects still needs a process
# restart (see bead REN-76t for the launchd + staged-patch story).
_FORMATTER_PATH = Path(telegram_format.__file__)
_FORMATTER_MTIME: float = 0.0


def _formatter():
    """Return the telegram_format module, hot-reloading if its file changed."""
    global _FORMATTER_MTIME, telegram_format
    try:
        mtime = _FORMATTER_PATH.stat().st_mtime
    except OSError:
        return telegram_format
    if _FORMATTER_MTIME and mtime > _FORMATTER_MTIME:
        try:
            telegram_format = importlib.reload(telegram_format)
            print(f"[telegram_format] hot-reloaded (mtime={mtime})")
        except Exception as e:
            # Reload failed — keep the previous good module in scope.
            print(f"[telegram_format] reload failed, staying on previous: {e!r}")
    _FORMATTER_MTIME = mtime
    return telegram_format


async def _reply_html(message, raw: str) -> None:
    """Render `raw` as Telegram HTML and send it; on parse-mode error, fall
    back to plain text so the user always gets the message."""
    try:
        rendered = _formatter().render(raw)
    except Exception as e:
        print(f"[telegram_format] render failed: {e!r}; sending raw")
        await message.reply_text(raw)
        return
    try:
        await message.reply_text(rendered, parse_mode="HTML")
    except Exception as e:
        # Telegram rejected the HTML (unmatched tag, suspicious entity, ...).
        # Retry as plain text so the reply still lands.
        print(f"[telegram] HTML send failed: {e!r}; retrying raw")
        try:
            await message.reply_text(raw)
        except Exception as e2:
            print(f"[telegram] raw fallback also failed: {e2!r}")


# ---------------------------------------------------------------------------
# Link-token plumbing
# ---------------------------------------------------------------------------


def issue_link_token(user_id: str) -> str:
    """Generate a one-shot token binding a future Telegram /start to user_id."""
    token = secrets.token_urlsafe(16)
    with connect() as c:
        c.execute(
            "INSERT INTO telegram_link_tokens(token, user_id) VALUES (?, ?)",
            (token, user_id),
        )
    return token


def consume_link_token(token: str) -> Optional[str]:
    """Atomically consume a token. Returns the user_id it was bound to, or None."""
    with connect() as c:
        row = c.execute(
            "SELECT user_id, created_at, consumed_at FROM telegram_link_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row or row["consumed_at"]:
            return None
        # Lazy expiry — sqlite stores timestamps as text, easier to compare client-side.
        c.execute(
            "UPDATE telegram_link_tokens SET consumed_at = CURRENT_TIMESTAMP "
            "WHERE token = ? AND consumed_at IS NULL",
            (token,),
        )
        if c.total_changes == 0:
            return None
    return row["user_id"]


def gc_expired_tokens() -> None:
    with connect() as c:
        c.execute(
            "DELETE FROM telegram_link_tokens "
            "WHERE created_at < datetime('now', '-{} seconds') AND consumed_at IS NULL".format(
                LINK_TOKEN_TTL_SEC
            )
        )


# ---------------------------------------------------------------------------
# Identity binding
# ---------------------------------------------------------------------------


def bind_telegram_user(user_id: str, tg_user_id: int, tg_username: Optional[str]) -> None:
    with connect() as c:
        c.execute(
            "UPDATE users SET telegram_user_id = ?, telegram_username = ? WHERE id = ?",
            (tg_user_id, tg_username, user_id),
        )


def principal_for_telegram(tg_user_id: int) -> Optional[auth.Principal]:
    with connect() as c:
        r = c.execute(
            "SELECT id, name, role FROM users WHERE telegram_user_id = ?",
            (tg_user_id,),
        ).fetchone()
    if not r:
        return None
    return auth.Principal(
        user_id=r["id"], name=r["name"], role=r["role"], token_label="telegram"
    )


# ---------------------------------------------------------------------------
# Thread context (per-tg-user rolling buffer, in-memory)
# ---------------------------------------------------------------------------

_thread: dict[int, deque] = defaultdict(lambda: deque(maxlen=THREAD_TURNS * 2))


def _record_thread(tg_uid: int, role: str, text: str) -> None:
    _thread[tg_uid].append((time.time(), role, text))


def _thread_context(tg_uid: int) -> str:
    """Live in-process buffer first; if it's empty (or this is the first
    message after a bot restart), fall back to the persisted episodes table
    for the principal — last 4 episodes within the 30-min window. This keeps
    "try again" working across restarts."""
    cutoff = time.time() - THREAD_WINDOW_MIN * 60
    rows = [r for r in _thread[tg_uid] if r[0] >= cutoff]
    if len(rows) > 1:
        lines = ["## Recent thread (this conversation, last 30m)"]
        for _, role, text in rows[:-1]:
            who = "user" if role == "them" else "you (agent)"
            lines.append(f"- {who}: {text}")
        return "\n".join(lines)

    # Fallback: pull recent episodes from disk so post-restart messages still
    # have context. Looks up the principal that's bound to this Telegram user
    # and reads their last few telegram-source episodes within 30 min.
    principal = principal_for_telegram(tg_uid)
    if not principal:
        return ""
    with connect() as c:
        eps = c.execute(
            """SELECT started_at, transcript, summary
               FROM episodes
               WHERE principal = ? AND source = 'telegram'
                 AND ended_at IS NOT NULL
                 AND started_at > datetime('now', ?)
               ORDER BY started_at DESC LIMIT ?""",
            (principal.user_id, f"-{THREAD_WINDOW_MIN} minutes", THREAD_TURNS),
        ).fetchall()
    if not eps:
        return ""
    # Episodes come back newest-first; render oldest-first for chronology.
    lines = ["## Recent thread (last few exchanges, restored from history)"]
    for r in reversed(eps):
        # Transcript was stored as "USER: ...\n\nAGENT: ..." in loop.py
        t = (r["transcript"] or "").strip()
        if t:
            lines.append(t)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    tg_user = update.effective_user
    if not args:
        # Already linked? Greet by name.
        principal = principal_for_telegram(tg_user.id)
        if principal:
            await update.message.reply_text(
                f"Hi {principal.name}. I'm here. Send a message and I'll do my best."
            )
        else:
            await update.message.reply_text(
                "Hi. I don't know you yet. Ask the operator to send you a link "
                "from `agent user link-telegram --user <your-id>`."
            )
        return

    token = args[0]
    user_id = consume_link_token(token)
    if not user_id:
        await update.message.reply_text(
            "That link expired or was already used. Ask the operator for a new one."
        )
        return

    bind_telegram_user(user_id, tg_user.id, tg_user.username)
    print(f"[link] {user_id} <- tg_user_id={tg_user.id} username={tg_user.username!r}")
    await update.message.reply_text(
        f"Linked. You're {user_id}. Send anything to start."
    )


async def _keep_user_warm(
    chat_id: int, message: "object", task_done: asyncio.Event,
):
    """Background coroutine: re-ping the 'typing...' indicator every 4s,
    and after 30s drop a single 'still working...' message so the user knows
    the long task hasn't stalled. Cancels itself when task_done is set."""
    elapsed = 0.0
    nudged = False
    while not task_done.is_set():
        try:
            await message.chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(task_done.wait(), timeout=4.0)
            return  # done
        except asyncio.TimeoutError:
            elapsed += 4.0
            if elapsed >= 30.0 and not nudged:
                try:
                    await message.reply_text(
                        "_…still thinking; long task in progress._",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                nudged = True


async def _surface_pending_dream_proposal(
    update: Update, principal: auth.Principal
) -> bool:
    """If there's a pending dream proposal that hasn't been shown to this
    user yet, send it as a card with [Approve] [Reject] [Later] buttons.
    Returns True iff one was surfaced (so the caller can choose to skip
    the user's actual message this turn — though we proceed anyway, so the
    return value is informational only)."""
    if principal.role != "admin":
        return False
    pending = dream_proposals.list_pending()
    if not pending:
        return False
    # Only surface ONE per inbound message — don't dump three at once.
    # Use last_seen-style filter via in-memory: skip ones we've already shown
    # this session. Persistence across restarts: rely on user actually using
    # one of the buttons (which removes it from list_pending).
    for prop in pending:
        if prop["id"] in _surfaced_dream_props.get(update.effective_user.id, set()):
            continue
        _surfaced_dream_props.setdefault(update.effective_user.id, set()).add(prop["id"])
        body = (
            f"💭 *Overnight, I noticed something:*\n\n"
            f"_{prop['title']}_\n\n"
            f"{prop['rationale']}"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Yes, do it",  "callback_data": f"dream_approve:{prop['id']}"},
                {"text": "❌ No",          "callback_data": f"dream_reject:{prop['id']}"},
                {"text": "🕒 Ask later",   "callback_data": f"dream_later:{prop['id']}"},
            ]]
        }
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return False
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": update.effective_chat.id,
                    "text": body,
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        return True
    return False


# In-memory: which dream proposal ids have been surfaced to which tg user
# during this bot run. Cleared on restart; that's fine — list_pending only
# returns rows still pending in the DB anyway.
_surfaced_dream_props: dict[int, set[str]] = {}

# Same idea for stale beads tasks. Plus a per-user cooldown so we don't
# pester them with the same task over and over inside one chat session.
_surfaced_stale_tasks: dict[int, set[str]] = {}
_last_stale_check: dict[int, float] = {}
STALE_CHECK_COOLDOWN_SEC = 60 * 60 * 4   # don't re-scan more than every 4h


async def _surface_stale_task(
    update: Update, principal: auth.Principal
) -> bool:
    """If `bd stale` returns anything we haven't shown this session, surface
    ONE task as a card with [✓ Done] [✗ Drop] [⟳ Resume] buttons. Cheap
    cooldown so we don't shell out every message."""
    import json as _json
    import subprocess as _subprocess

    if principal.role not in ("admin", "adult"):
        return False

    now = time.time()
    last = _last_stale_check.get(update.effective_user.id, 0.0)
    if now - last < STALE_CHECK_COOLDOWN_SEC:
        return False
    _last_stale_check[update.effective_user.id] = now

    try:
        cp = _subprocess.run(
            ["bd", "stale", "--json"],
            capture_output=True, text=True, timeout=15,
            cwd="/Users/smo/repos/home-agent",
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return False
        parsed = _json.loads(cp.stdout)
    except Exception as e:
        print(f"[bd] stale check error (non-fatal): {e}")
        return False

    # Normalize possible shapes: [], [{...}], {"issues": [...]}, {"error": "..."}
    if isinstance(parsed, dict):
        if "error" in parsed:
            print(f"[bd] stale error from CLI: {parsed['error']}")
            return False
        items = parsed.get("issues") or []
    elif isinstance(parsed, list):
        items = parsed
    else:
        items = []

    if not items:
        return False

    seen = _surfaced_stale_tasks.setdefault(update.effective_user.id, set())
    for item in items:
        tid = item.get("id") or item.get("ID")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        title = item.get("title") or item.get("Title") or "(no title)"
        updated = item.get("updated_at") or item.get("UpdatedAt") or "?"
        body = (
            f"📌 *I've been holding onto this:*\n\n"
            f"_{title}_\n\n"
            f"`{tid}` · last touched {updated}\n\n"
            f"Want me to push on it, or drop it?"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "⟳ Resume",  "callback_data": f"bd_resume:{tid}"},
                {"text": "✓ Done",    "callback_data": f"bd_done:{tid}"},
                {"text": "✗ Drop",    "callback_data": f"bd_drop:{tid}"},
            ]]
        }
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            return False
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": update.effective_chat.id,
                    "text": body,
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        return True
    return False


def _quoted_context(update: Update) -> str:
    """If the user used Telegram's 'reply to' on a previous message, render
    the quoted text + a hint so the agent knows what's being responded to.

    Telegram includes the full original message in `reply_to_message` —
    text, sender (was it the bot or another user), date. We surface text
    only; the bot/human distinction is implicit (this is a 1:1 chat with
    the bot today).
    """
    rt = update.message.reply_to_message
    if not rt:
        return ""
    quoted = (rt.text or rt.caption or "").strip()
    if not quoted:
        return ""
    # Trim long quoted blocks; keep the most relevant slice.
    if len(quoted) > 600:
        quoted = quoted[:600] + "…"
    sender = "you (the agent, earlier)" if rt.from_user and rt.from_user.is_bot else "a previous message"
    return (
        f"## The user is replying to {sender}:\n"
        f"> {quoted}\n\n"
        f"Their reply follows. Treat it as a response to that quoted text — "
        f"if it's an approval/rejection of something you proposed, act on it; "
        f"if it's a clarification of an earlier topic, fold it in."
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return

    principal = principal_for_telegram(tg_user.id)
    if not principal:
        await update.message.reply_text(
            "I don't know you yet. Ask the operator for a /start link."
        )
        return

    # Proactively surface ONE pending dream proposal AND/OR ONE stale task
    # (if any) BEFORE we run the user's task. They can decide on those
    # asynchronously via the buttons while the agent works on whatever they
    # actually asked.
    try:
        await _surface_pending_dream_proposal(update, principal)
    except Exception as e:
        print(f"[surface] dream error (non-fatal): {e}")
    try:
        await _surface_stale_task(update, principal)
    except Exception as e:
        print(f"[surface] bd stale error (non-fatal): {e}")

    quoted = _quoted_context(update)
    if quoted:
        print(f"[in ] {principal.name} (tg={tg_user.username or tg_user.id}) [REPLY-TO]: {text[:80]!r}")
    else:
        print(f"[in ] {principal.name} (tg={tg_user.username or tg_user.id}): {text[:80]!r}")

    _record_thread(tg_user.id, "them", text)

    # Persistent conversation-thread state. Increments turn_count and
    # last_active on the open thread (or opens a new one). Ren can
    # introspect via thread__status / decide via thread__compact.
    thread_state = None
    try:
        thread_state = threads.record_turn(
            "telegram", principal.user_id, last_turn_text=text
        )
    except Exception as e:
        print(f"[thread] record_turn error (non-fatal): {e}")

    # Inject lightweight thread metadata so Ren can decide without having
    # to call thread__status on every message. They can still call it for
    # the full picture.
    thread_meta = ""
    if thread_state:
        thread_meta = (
            f"## Conversation thread state\n"
            f"- thread #{thread_state.id} on telegram\n"
            f"- turn {thread_state.turn_count} · started {thread_state.started_at}\n"
            f"- if this thread feels concluded or has drifted, consider "
            f"`thread__compact` with a summary; otherwise just continue."
        )

    thread = _thread_context(tg_user.id)
    # Order: rolling buffer (broad) → quoted (narrow) → thread meta (decision aid) → the message.
    parts = [p for p in (thread, quoted, thread_meta) if p]
    task = ("\n\n".join(parts) + "\n\n" + text) if parts else text

    # Submit through the resident agent for this principal. The resident
    # owns a long-lived ClaudeSDKClient; if the user sends another message
    # while we're streaming, the resident will interrupt and fold the new
    # text in as a steer (returning None for the superseded submission, so
    # we don't double-reply). See orchestrator/resident.py for the model.
    try:
        agent = await residents.get_resident(
            principal=principal, surface="telegram",
            profile="mobile", model="claude-opus-4-7",
        )
    except Exception as e:
        print(f"[resident] start error: {e!r}")
        await update.message.reply_text(f"(agent error: {type(e).__name__}: {e})")
        return

    task_done = asyncio.Event()
    warm_task = asyncio.create_task(
        _keep_user_warm(update.effective_chat.id, update.message, task_done)
    )
    try:
        try:
            reply = await agent.submit(task)
        except Exception as e:
            reply = f"(agent error: {type(e).__name__}: {e})"
    finally:
        task_done.set()
        await warm_task  # let it exit cleanly

    if reply is None:
        # Steering interrupt: a later message in this same window owns the
        # reply. Stay quiet so we don't double-send.
        print(f"[out] -> tg={tg_user.username or tg_user.id}: (steered, no reply sent)")
        return

    if reply and len(reply) > REPLY_MAX_CHARS:
        reply = reply[: REPLY_MAX_CHARS - 1] + "…"

    _record_thread(tg_user.id, "us", reply)
    await _reply_html(update.message, reply or "(no response)")
    print(f"[out] -> tg={tg_user.username or tg_user.id}: {reply[:80]!r}")


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-button callback router. Three flavors:
      - L3 approval:   'approve:<rid>' / 'deny:<rid>'
      - Dream proposal: 'dream_approve:<pid>' / 'dream_reject:<pid>' / 'dream_later:<pid>'
      - (Future) more here. Keep prefixes namespaced.
    """
    cq = update.callback_query
    if not cq:
        return
    data = (cq.data or "").strip()
    if ":" not in data:
        await cq.answer("invalid callback")
        return
    action, _, payload_id = data.partition(":")

    tg_principal = principal_for_telegram(update.effective_user.id)
    if not tg_principal:
        await cq.answer("you're not linked", show_alert=True)
        return

    # ----- L3 tool approval -----
    if action in ("approve", "deny"):
        state = approvals.fetch_state(payload_id)
        if not state:
            await cq.answer("request expired or unknown", show_alert=True)
            return
        if tg_principal.user_id != state["user_id"]:
            await cq.answer("not your approval to make", show_alert=True)
            return
        if state["state"] != "pending":
            await cq.answer(f"already {state['state']}", show_alert=True)
            return
        ok = approvals.decide(payload_id, approved=(action == "approve"), via="telegram")
        if not ok:
            await cq.answer("race lost", show_alert=True)
            return
        label = "✅ Approved" if action == "approve" else "❌ Denied"
        await cq.edit_message_text(
            text=f"{label}: `{state['tool_name']}`\n_{state['summary']}_",
            parse_mode="Markdown",
        )
        await cq.answer()
        print(f"[approval] {payload_id} -> {action} by tg={update.effective_user.id}")
        return

    # ----- Beads stale-task surface -----
    if action in ("bd_resume", "bd_done", "bd_drop"):
        import subprocess as _subprocess
        tid = payload_id
        try:
            if action == "bd_resume":
                # Bump priority so it shows up in `bd ready` next time and
                # add a note so the touch is real.
                _subprocess.run(["bd", "note", tid, "Resumed by Shupei via Telegram"],
                                capture_output=True, timeout=10,
                                cwd="/Users/smo/repos/home-agent")
                msg = f"⟳ Resumed: `{tid}`"
            elif action == "bd_done":
                _subprocess.run(["bd", "close", tid],
                                capture_output=True, timeout=10,
                                cwd="/Users/smo/repos/home-agent")
                msg = f"✓ Closed: `{tid}`"
            elif action == "bd_drop":
                # 'drop' = close with a reason; beads doesn't have a separate
                # 'abandoned' state, so we close + note.
                _subprocess.run(["bd", "note", tid, "Dropped (no longer relevant)"],
                                capture_output=True, timeout=10,
                                cwd="/Users/smo/repos/home-agent")
                _subprocess.run(["bd", "close", tid],
                                capture_output=True, timeout=10,
                                cwd="/Users/smo/repos/home-agent")
                msg = f"✗ Dropped: `{tid}`"
            await cq.edit_message_text(text=msg, parse_mode="Markdown")
        except Exception as e:
            await cq.edit_message_text(text=f"⚠️ couldn't update `{tid}`: {e}",
                                        parse_mode="Markdown")
        await cq.answer()
        print(f"[bd] {tid} -> {action} by tg={update.effective_user.id}")
        return

    # ----- Dream proposal -----
    if action in ("dream_approve", "dream_reject", "dream_later"):
        # Anyone admin-role can decide; for now restrict to the original
        # 'shupei' admin (multi-admin can come later).
        if tg_principal.role != "admin":
            await cq.answer("admin only", show_alert=True)
            return
        prop = dream_proposals.get(payload_id)
        if not prop:
            await cq.answer("proposal not found", show_alert=True)
            return
        if prop["state"] != "pending":
            await cq.answer(f"already {prop['state']}", show_alert=True)
            return

        if action == "dream_approve":
            ok, msg = dream_proposals.approve(
                payload_id, decided_by=f"telegram:{tg_principal.user_id}"
            )
            label = "✅ Applied" if ok else "⚠️ Could not apply"
            await cq.edit_message_text(
                text=f"{label}\n_{msg}_", parse_mode="Markdown",
            )
        elif action == "dream_reject":
            dream_proposals.reject(
                payload_id, decided_by=f"telegram:{tg_principal.user_id}",
                reason="rejected via telegram (no reason)",
            )
            await cq.edit_message_text(
                text=f"❌ Rejected: _{prop['title']}_", parse_mode="Markdown",
            )
        elif action == "dream_later":
            # Leave as pending; just acknowledge so the user knows we got it.
            await cq.edit_message_text(
                text=f"🕒 Will surface again next time: _{prop['title']}_",
                parse_mode="Markdown",
            )
        await cq.answer()
        print(f"[dream] {payload_id} -> {action} by tg={update.effective_user.id}")
        return

    await cq.answer(f"unknown action {action!r}", show_alert=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"[err] {context.error!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Startup self-check + drift detection
# ---------------------------------------------------------------------------
# After a restart (planned `launchctl kickstart -k`, KeepAlive crash recovery,
# host reboot), the post_init hook reads the previous shutdown snapshot, runs
# a health check, computes drift, and posts a one-line greeting to each
# linked admin so silence-after-restart isn't, well, silent.
#
# Trade-offs intentionally NOT covered here:
#   - Pre-restart countdown: would need a wrapper around launchctl kickstart.
#     Deferred — the restart sender (the user, or a future script) can add
#     that themselves.
#   - MCP server health probe: MCP servers spawn per agent invocation, not as
#     long-lived processes. The first user message after a restart is the
#     true MCP smoke test; nothing useful to probe at startup.

_SHUTDOWN_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "last_shutdown.json"

# Scheduler heartbeat — initialized in _announce_startup, stopped in _record_shutdown.
_HEARTBEAT: Optional[Heartbeat] = None


async def _scheduler_push_to_owner(owner_user_id: str, text: str) -> None:
    """Out-of-band notification path used by the scheduler subagent.
    Looks up the owner's telegram_user_id and sends via the bot directly —
    bypassing the resident agent so the message never enters the user's
    active conversation thread."""
    global _APPLICATION_REF
    app = _APPLICATION_REF
    if app is None:
        print(f"[scheduler] no app reference; dropping notify to {owner_user_id}")
        return
    with connect() as c:
        r = c.execute(
            "SELECT telegram_user_id FROM users WHERE id = ?", (owner_user_id,)
        ).fetchone()
    if not r or not r["telegram_user_id"]:
        print(
            f"[scheduler] no telegram binding for {owner_user_id}; dropping notify"
        )
        return
    chat_id = int(r["telegram_user_id"])
    try:
        await app.bot.send_message(chat_id=chat_id, text=text)
        print(f"[scheduler] notified {owner_user_id} (chat={chat_id}): {text[:80]!r}")
    except Exception as e:
        print(f"[scheduler] could not notify {owner_user_id}: {e!r}")


# Module-level reference set in _announce_startup so the notify bridge can
# reach the live Application without threading it through every callback.
_APPLICATION_REF: Optional[Application] = None


def _linked_admins() -> list[tuple[str, int]]:
    """(user_id, telegram_chat_id) for admin users with a Telegram binding.
    In a 1:1 chat with a bot, chat_id == user_id, so the tg_user_id column
    doubles as the chat_id we send to."""
    with connect() as c:
        rows = c.execute(
            "SELECT id, telegram_user_id FROM users "
            "WHERE role='admin' AND telegram_user_id IS NOT NULL"
        ).fetchall()
    return [(r["id"], int(r["telegram_user_id"])) for r in rows]


def _bd_ready_summary() -> tuple[int, list[str]]:
    """Return (open_count, top_3_titles). (-1, []) on any error so callers
    can flag without crashing startup."""
    try:
        cp = subprocess.run(
            ["bd", "ready", "--json"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if cp.returncode != 0:
            return -1, []
        items = json.loads(cp.stdout) or []
        if not isinstance(items, list):
            return -1, []
        titles = [(it.get("title") or "")[:80] for it in items[:3]]
        return len(items), titles
    except Exception:
        return -1, []


def _gather_state_snapshot() -> dict:
    """Capture state worth comparing across a restart. Used by the shutdown
    writer; the startup reader gathers a 'now' snapshot via the same function
    and diffs the two."""
    snap: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }

    # Open threads, per linked admin (currently only telegram surface, but
    # the structure is forward-compat for imessage etc).
    open_threads: list[dict] = []
    try:
        for user_id, _ in _linked_admins():
            t = threads.get_open("telegram", user_id)
            if t:
                open_threads.append({
                    "id": t.id, "surface": t.surface, "principal": t.principal,
                    "turn_count": t.turn_count,
                    "started_at": t.started_at, "last_active": t.last_active,
                    "last_turn_preview": (t.last_turn_text or "")[:80],
                })
    except Exception as e:
        print(f"[snapshot] threads error (non-fatal): {e}")
    snap["open_threads"] = open_threads

    # Beads
    bd_count, bd_titles = _bd_ready_summary()
    snap["bd_open_count"] = bd_count
    snap["bd_top_titles"] = bd_titles

    # Memory: max id is a cheap monotonic drift detector.
    try:
        with connect() as c:
            r = c.execute("SELECT MAX(id) AS m FROM memory").fetchone()
        snap["max_memory_id"] = int(r["m"] or 0)
    except Exception as e:
        print(f"[snapshot] memory query error: {e}")
        snap["max_memory_id"] = -1

    return snap


def _read_shutdown_snapshot() -> Optional[dict]:
    try:
        with _SHUTDOWN_SNAPSHOT_PATH.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[startup] could not read shutdown snapshot: {e}")
        return None


def _write_shutdown_snapshot(snap: dict) -> None:
    """Atomic write: tmp file + rename, so a crash mid-write doesn't leave
    a corrupt snapshot for the next startup."""
    try:
        _SHUTDOWN_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SHUTDOWN_SNAPSHOT_PATH.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(snap, f, indent=2, default=str)
        tmp.replace(_SHUTDOWN_SNAPSHOT_PATH)
    except Exception as e:
        print(f"[shutdown] could not write snapshot: {e}")


async def _record_shutdown(app: Application) -> None:
    """post_shutdown hook: stop scheduler + resident agents cleanly, then
    persist state for the next startup's drift check. Best-effort; never
    raise (we're already on the way out)."""
    global _HEARTBEAT
    # Stop the heartbeat before residents so any in-flight subagent gets a
    # chance to wind down on its own ClaudeSDKClient close path.
    try:
        if _HEARTBEAT is not None:
            await _HEARTBEAT.stop()
    except Exception as e:
        print(f"[shutdown] heartbeat stop failed: {e}")
    finally:
        scheduler_tool_registry.clear_heartbeat()
        _HEARTBEAT = None
    # Stop residents next so any in-flight ClaudeSDKClient closes its
    # subprocess + read-task before the event loop tears down.
    try:
        await residents.stop_all()
    except Exception as e:
        print(f"[shutdown] resident stop failed: {e}")
    try:
        snap = _gather_state_snapshot()
        snap["shutdown_clean"] = True
        _write_shutdown_snapshot(snap)
        print(
            f"[shutdown] snapshot written: "
            f"{snap.get('bd_open_count')} bd open, "
            f"{len(snap.get('open_threads', []))} threads open, "
            f"max_memory_id={snap.get('max_memory_id')}"
        )
    except Exception as e:
        print(f"[shutdown] snapshot failed: {e}")


def _format_startup_status(
    snap_prev: Optional[dict], snap_now: dict
) -> tuple[str, list[str], list[str]]:
    """Return (icon, notes, issues). icon ∈ {🟢, 🟡, 🔴}.
    🟢 = all checks pass, downtime explained.
    🟡 = functional but something noteworthy (long downtime, lost thread, ...).
    🔴 = a check failed (db unreachable, memory regression, ...).
    """
    notes: list[str] = []
    issues: list[str] = []

    # DB reachability
    if snap_now.get("max_memory_id", -1) < 0:
        issues.append("db unreachable")
    else:
        notes.append("db ok")

    # Beads CLI
    bd_n = snap_now.get("bd_open_count", -1)
    if bd_n < 0:
        issues.append("bd cli failed")
    else:
        notes.append(f"bd {bd_n} open")

    # Downtime + cross-restart drift
    if snap_prev is None:
        notes.append("first run (no prior snapshot)")
    else:
        try:
            prev_t = datetime.fromisoformat(snap_prev["captured_at"])
            now_t = datetime.fromisoformat(snap_now["captured_at"])
            delta = (now_t - prev_t).total_seconds()
            if delta < 60:
                notes.append(f"down {int(delta)}s (kickstart-clean)")
            elif delta < 3600:
                notes.append(f"down {int(delta // 60)}m")
            else:
                notes.append(f"down {delta / 3600:.1f}h")
        except Exception:
            notes.append("downtime unknown")

        if not snap_prev.get("shutdown_clean"):
            issues.append("prior shutdown not clean (crash or hard kill)")

        # Thread drift: any thread that was open before is no longer open?
        prev_thread_ids = {t["id"] for t in snap_prev.get("open_threads", [])}
        now_thread_ids = {t["id"] for t in snap_now.get("open_threads", [])}
        lost = prev_thread_ids - now_thread_ids
        if lost:
            issues.append(f"{len(lost)} thread(s) closed across restart")

        # Memory id should never decrease.
        prev_max = snap_prev.get("max_memory_id", -1)
        now_max = snap_now.get("max_memory_id", -1)
        if prev_max > 0 and 0 <= now_max < prev_max:
            issues.append(f"memory id regressed ({prev_max} → {now_max})")

    if any("unreachable" in i or "regressed" in i or "cli failed" in i for i in issues):
        icon = "🔴"
    elif issues:
        icon = "🟡"
    else:
        icon = "🟢"

    return icon, notes, issues


async def _announce_startup(app: Application) -> None:
    """post_init hook: greet each linked admin with a one-line health line +
    drift summary. Catches and logs every failure so a flaky check never
    blocks polling startup."""
    global _APPLICATION_REF, _HEARTBEAT
    _APPLICATION_REF = app

    # Start the scheduler heartbeat. Survives any exception in the rest of
    # this hook — schedule firing must not depend on the greeting succeeding.
    try:
        _HEARTBEAT = Heartbeat(on_notify=_scheduler_push_to_owner)
        scheduler_tool_registry.set_heartbeat(_HEARTBEAT)
        _HEARTBEAT.start()
    except Exception as e:
        print(f"[startup] heartbeat failed to start: {e!r}")

    try:
        snap_prev = _read_shutdown_snapshot()
        snap_now = _gather_state_snapshot()

        icon, notes, issues = _format_startup_status(snap_prev, snap_now)

        head = f"{icon} Ren back up · " + " · ".join(notes)
        if issues:
            head += "\n⚠️ " + " · ".join(issues)

        # Mention any still-open threads so the user (and I) know I'm
        # continuing, not starting fresh.
        for t in snap_now.get("open_threads", []):
            preview = t.get("last_turn_preview") or ""
            head += (
                f"\n↳ thread #{t['id']} on {t['surface']} "
                f"({t['turn_count']} turns) — last: \"{preview}\""
            )

        for user_id, chat_id in _linked_admins():
            try:
                await app.bot.send_message(chat_id=chat_id, text=head)
                print(f"[startup] greeted {user_id} (chat={chat_id}): {icon}")
            except Exception as e:
                print(f"[startup] could not greet {user_id}: {e}")
    except Exception as e:
        print(f"[startup] announce failed: {e}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def build_app() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set. Create a bot via @BotFather, paste the "
            "token into .env, then re-run."
        )
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(_announce_startup)
        .post_shutdown(_record_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CallbackQueryHandler(on_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def main() -> int:
    load_dotenv()
    ensure_schema()
    gc_expired_tokens()
    app = build_app()
    print("[init] telegram bot starting (long-polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())

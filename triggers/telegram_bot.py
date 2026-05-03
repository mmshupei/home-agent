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
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from orchestrator import auth, loop
from orchestrator.db import connect, ensure_schema

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LINK_TOKEN_TTL_SEC = 5 * 60  # 5 minutes
REPLY_MAX_CHARS = 4000  # Telegram limit is 4096
THREAD_TURNS = 4
THREAD_WINDOW_MIN = 30


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
    cutoff = time.time() - THREAD_WINDOW_MIN * 60
    rows = [r for r in _thread[tg_uid] if r[0] >= cutoff]
    if len(rows) <= 1:
        return ""
    lines = ["## Recent thread (this conversation, last 30m)"]
    for _, role, text in rows[:-1]:
        who = "user" if role == "them" else "you (agent)"
        lines.append(f"- {who}: {text}")
    return "\n".join(lines)


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

    print(f"[in ] {principal.name} (tg={tg_user.username or tg_user.id}): {text[:80]!r}")
    _record_thread(tg_user.id, "them", text)

    # Show "typing..." while the orchestrator works
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    thread = _thread_context(tg_user.id)
    task = (thread + "\n\n" + text) if thread else text

    try:
        reply = await loop.run(
            task=task, principal=principal, profile="mobile", model="claude-opus-4-7"
        )
    except Exception as e:
        reply = f"(agent error: {type(e).__name__}: {e})"

    if reply and len(reply) > REPLY_MAX_CHARS:
        reply = reply[: REPLY_MAX_CHARS - 1] + "…"

    _record_thread(tg_user.id, "us", reply)
    await update.message.reply_text(reply or "(no response)")
    print(f"[out] -> tg={tg_user.username or tg_user.id}: {reply[:80]!r}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"[err] {context.error!r}", file=sys.stderr)


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
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", on_start))
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

"""Cross-process L3 approval queue.

Architecture: the gate (running in the agent process) inserts a pending
approval_requests row and asks Telegram to ping the user with inline
[Approve] / [Deny] buttons. The Telegram bot (running in its own launchd
process) handles the button callback and UPDATEs the row to
approved/denied. The gate polls every 1s up to the expiry deadline.

This works the same regardless of where the agent is running (REPL, CLI,
HTTP, launchd job) — the only requirement is the bot daemon is alive and
the user has a linked Telegram account.

Fallback: if Telegram isn't configured for the principal, the function
returns False (default deny). No silent allows.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from .auth import Principal
from .db import connect

DEFAULT_WAIT_SECONDS = 60
POLL_INTERVAL_SEC = 1.0
TELEGRAM_API = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Persistence helpers (used by both gate side and bot side)
# ---------------------------------------------------------------------------


def create_request(
    *, principal: Principal, tier: int, tool_name: str,
    summary: str, payload: dict, ttl_seconds: int,
) -> str:
    """Insert a pending request and return its id."""
    rid = secrets.token_urlsafe(12)
    expires = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    with connect() as c:
        c.execute(
            """INSERT INTO approval_requests
               (id, user_id, tier, tool_name, summary, payload_json, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rid, principal.user_id, tier, tool_name, summary,
             json.dumps(payload, default=str), expires),
        )
    return rid


def fetch_state(request_id: str) -> Optional[dict]:
    with connect() as c:
        r = c.execute(
            "SELECT id, user_id, tier, tool_name, summary, payload_json, "
            "expires_at, state, decided_at, decided_via "
            "FROM approval_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    return dict(r) if r else None


def decide(request_id: str, *, approved: bool, via: str) -> bool:
    """Atomically transition pending → approved/denied. Returns False if the
    request had already been decided (race protection)."""
    new_state = "approved" if approved else "denied"
    with connect() as c:
        cur = c.execute(
            "UPDATE approval_requests "
            "SET state=?, decided_at=CURRENT_TIMESTAMP, decided_via=? "
            "WHERE id=? AND state='pending'",
            (new_state, via, request_id),
        )
        return cur.rowcount > 0


def expire_overdue() -> int:
    """Sweep any past-expiry pending rows to state='expired'. Called
    opportunistically by the gate's polling loop and by the bot."""
    with connect() as c:
        cur = c.execute(
            "UPDATE approval_requests SET state='expired', "
            "decided_at=CURRENT_TIMESTAMP, decided_via='timeout' "
            "WHERE state='pending' AND expires_at < datetime('now')",
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Telegram side
# ---------------------------------------------------------------------------


def telegram_user_id_for(user_id: str) -> Optional[int]:
    with connect() as c:
        r = c.execute(
            "SELECT telegram_user_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not r:
        return None
    tg = r["telegram_user_id"]
    return int(tg) if tg is not None else None


async def _send_telegram_prompt(
    *, chat_id: int, request_id: str, tier: int, tool_name: str, summary: str,
) -> bool:
    """Send the inline-keyboard approval message. Returns True on HTTP 200."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    body = (
        f"⚠️ *L{tier} approval needed*\n\n"
        f"`{tool_name}`\n"
        f"{summary}\n\n"
        f"_Auto-denies in 60s._"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{request_id}"},
            {"text": "❌ Deny",    "callback_data": f"deny:{request_id}"},
        ]]
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, "text": body,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
        )
    return r.status_code == 200


async def request_telegram(
    *, principal: Principal, tier: int, tool_input: dict,
    summary: str | None = None, wait_seconds: int = DEFAULT_WAIT_SECONDS,
) -> bool:
    """Ask the principal via Telegram. Returns True iff approved within
    wait_seconds. Default deny on:
    - principal has no linked Telegram account
    - bot token unset
    - Telegram API error
    - timeout (no button pressed in wait_seconds)
    """
    chat_id = telegram_user_id_for(principal.user_id)
    if chat_id is None:
        # Cannot reach the user; safest is deny.
        return False

    summary = summary or _auto_summary(tool_input)
    rid = create_request(
        principal=principal, tier=tier,
        tool_name=tool_input.get("tool_name", "?"),
        summary=summary, payload=tool_input,
        ttl_seconds=wait_seconds,
    )

    sent = await _send_telegram_prompt(
        chat_id=chat_id, request_id=rid, tier=tier,
        tool_name=tool_input.get("tool_name", "?"), summary=summary,
    )
    if not sent:
        # Sweep the row so it doesn't sit forever
        decide(rid, approved=False, via="send_failed")
        return False

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        st = fetch_state(rid)
        if st and st["state"] in ("approved", "denied"):
            return st["state"] == "approved"

    expire_overdue()
    return False


def _auto_summary(tool_input: dict) -> str:
    name = tool_input.get("tool_name", "?")
    args = tool_input.get("tool_input", {})
    if isinstance(args, dict):
        bits = ", ".join(f"{k}={_short(v)}" for k, v in list(args.items())[:3])
        return f"{name}({bits})"
    return name


def _short(v) -> str:
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "…"

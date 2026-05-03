"""Pushover client + emergency-priority receipt polling.

Two flavors:
- send(): one-shot push to a user_key (or device list)
- send_emergency_and_wait(): priority=2, polls /1/receipts until acknowledged
  or `wait_seconds` elapses; default deny on timeout/error

Configuration via .env:
  PUSHOVER_APP_TOKEN          required
  PUSHOVER_USER_KEY_<UID>     per-user key (e.g. PUSHOVER_USER_KEY_SHUPEI)
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx

API_BASE = "https://api.pushover.net/1"


def app_token() -> str | None:
    return os.environ.get("PUSHOVER_APP_TOKEN") or None


def user_key(user_id: str) -> str | None:
    return os.environ.get(f"PUSHOVER_USER_KEY_{user_id.upper()}") or None


def is_configured(user_id: str) -> bool:
    return bool(app_token() and user_key(user_id))


@dataclass
class PushReceipt:
    receipt: str
    expires_at: int


async def send(
    *,
    user_id: str,
    message: str,
    title: str = "Agent",
    priority: int = 0,
    sound: str | None = None,
) -> dict:
    """One-shot push. priority 0 = normal, 1 = high (bypasses quiet hours),
    2 = emergency (use send_emergency_and_wait instead)."""
    token = app_token()
    key = user_key(user_id)
    if not token or not key:
        return {"ok": False, "error": f"pushover not configured for {user_id}"}
    payload = {
        "token": token,
        "user": key,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if sound:
        payload["sound"] = sound
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{API_BASE}/messages.json", data=payload)
    return r.json() | {"ok": r.status_code == 200}


async def send_emergency_and_wait(
    *,
    user_id: str,
    message: str,
    title: str = "Agent — approve?",
    wait_seconds: int = 60,
    retry: int = 30,
    expire: int | None = None,
    poll_interval: float = 3.0,
) -> bool:
    """Send a priority=2 push and poll its receipt until acknowledged or
    timeout. Returns True iff the user explicitly acknowledged in time.

    Pushover's `expire` (max 10800s) bounds how long the device keeps re-alerting.
    Our `wait_seconds` is a soft cap on how long the agent waits before giving
    up and denying — it's much shorter (60s default) so the gate doesn't stall
    the conversation.
    """
    token = app_token()
    key = user_key(user_id)
    if not token or not key:
        return False
    if expire is None:
        # Make the device's re-alert window match our wait + a little slack.
        expire = max(wait_seconds + 10, retry * 2)

    payload = {
        "token": token,
        "user": key,
        "title": title,
        "message": message,
        "priority": 2,
        "retry": retry,
        "expire": expire,
    }
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{API_BASE}/messages.json", data=payload)
        body = r.json()
        if r.status_code != 200 or not body.get("receipt"):
            return False
        receipt = body["receipt"]

        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            rr = await c.get(
                f"{API_BASE}/receipts/{receipt}.json", params={"token": token}
            )
            if rr.status_code != 200:
                continue
            rb = rr.json()
            if int(rb.get("acknowledged", 0)) == 1:
                # Stop further re-alerts.
                await c.post(
                    f"{API_BASE}/receipts/{receipt}/cancel.json",
                    data={"token": token},
                )
                return True
            if int(rb.get("expired", 0)) == 1:
                return False
        # Timed out without ack — cancel re-alerts and deny.
        try:
            await c.post(
                f"{API_BASE}/receipts/{receipt}/cancel.json",
                data={"token": token},
            )
        except Exception:
            pass
    return False

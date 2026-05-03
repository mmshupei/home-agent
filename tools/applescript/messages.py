"""Messages.app shim — send iMessage / SMS via osascript.

L3 in tier policy. Always show recipient + body in the prompt before sending.
"""
from __future__ import annotations

from ._run import escape, run


async def send(*, to: str, body: str, service: str = "iMessage") -> dict:
    """Send a message. `to` is a phone (+15555550100) or Apple ID email.
    Returns {'ok', 'error'}."""
    script = f"""
    tell application "Messages"
        set targetService to first service whose service type = {service}
        set targetBuddy to buddy {escape(to)} of targetService
        send {escape(body)} to targetBuddy
        return "sent"
    end tell
    """
    r = await run(script, timeout=20.0)
    return {
        "ok": r.ok and "sent" in r.stdout,
        "error": None if r.ok else r.stderr.strip(),
    }

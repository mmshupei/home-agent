"""Reminders.app shims via osascript.

The default list is the user's primary list (whichever Reminders.app considers
default). Pass list_name to target a specific one (e.g. 'Family').
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ._run import escape, run


async def add(
    title: str,
    *,
    list_name: Optional[str] = None,
    due: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create a reminder. Returns {'ok': bool, 'id': str | None, 'error': str | None}."""
    list_clause = (
        f"set targetList to (first list whose name is {escape(list_name)})"
        if list_name
        else "set targetList to default list"
    )
    props = [f"name:{escape(title)}"]
    if notes:
        props.append(f"body:{escape(notes)}")
    if due:
        # AppleScript date literal: "Monday, January 1, 2026 at 4:00:00 PM"
        ds = due.strftime("%A, %B %-d, %Y at %-I:%M:%S %p")
        props.append(f'due date:date {escape(ds)}')
    props_str = ", ".join(props)

    script = f"""
    tell application "Reminders"
        {list_clause}
        set newReminder to make new reminder at end of reminders of targetList ¬
            with properties {{{props_str}}}
        return id of newReminder
    end tell
    """
    r = await run(script)
    if not r.ok:
        return {"ok": False, "id": None, "error": r.stderr.strip()}
    return {"ok": True, "id": r.text(), "error": None}


async def complete(reminder_id: str) -> dict:
    script = f"""
    tell application "Reminders"
        set r to (first reminder whose id is {escape(reminder_id)})
        set completed of r to true
        return "ok"
    end tell
    """
    r = await run(script)
    return {"ok": r.ok, "error": None if r.ok else r.stderr.strip()}


async def list_reminders(
    *, list_name: Optional[str] = None, completed: bool = False, limit: int = 25
) -> list[dict]:
    """Return open (or completed) reminders. Each row: {id, name, due, list}."""
    list_clause = (
        f"set src to (first list whose name is {escape(list_name)})"
        if list_name
        else "set src to default list"
    )
    completed_clause = "true" if completed else "false"
    # We emit JSON-ish lines we parse on this side, since AppleScript records
    # don't serialize cleanly.
    script = f"""
    set out to ""
    tell application "Reminders"
        {list_clause}
        set lname to name of src
        set rs to (reminders of src whose completed is {completed_clause})
        set n to count of rs
        if n > {limit} then set n to {limit}
        repeat with i from 1 to n
            set r to item i of rs
            set rid to id of r
            set rn to name of r
            try
                set rd to (due date of r) as string
            on error
                set rd to ""
            end try
            set out to out & rid & "\\t" & rn & "\\t" & rd & "\\t" & lname & linefeed
        end repeat
    end tell
    return out
    """
    r = await run(script)
    if not r.ok:
        return []
    rows: list[dict] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append({"id": parts[0], "name": parts[1], "due": parts[2], "list": parts[3]})
    return rows

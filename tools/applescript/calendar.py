"""Calendar.app shims via osascript."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ._run import escape, run

DEFAULT_CAL = "Home"  # users can override per-call; M4 default is the family-shared one


async def add_event(
    title: str,
    *,
    starts_at: datetime,
    ends_at: Optional[datetime] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    calendar: str = DEFAULT_CAL,
) -> dict:
    """Create a calendar event. Returns {'ok', 'id', 'error'}."""
    if ends_at is None:
        # Default to 1 hour
        from datetime import timedelta
        ends_at = starts_at + timedelta(hours=1)

    sd = starts_at.strftime("%A, %B %-d, %Y at %-I:%M:%S %p")
    ed = ends_at.strftime("%A, %B %-d, %Y at %-I:%M:%S %p")

    props = [
        f"summary:{escape(title)}",
        f"start date:date {escape(sd)}",
        f"end date:date {escape(ed)}",
    ]
    if location:
        props.append(f"location:{escape(location)}")
    if notes:
        props.append(f"description:{escape(notes)}")

    script = f"""
    tell application "Calendar"
        set targetCal to (first calendar whose name is {escape(calendar)})
        set newEvent to make new event at end of events of targetCal ¬
            with properties {{{", ".join(props)}}}
        return uid of newEvent
    end tell
    """
    r = await run(script)
    if not r.ok:
        return {"ok": False, "id": None, "error": r.stderr.strip()}
    return {"ok": True, "id": r.text(), "error": None}


async def list_events(
    *, days_ahead: int = 7, calendar: Optional[str] = None
) -> list[dict]:
    """Return upcoming events across one or all calendars."""
    cal_clause = (
        f"set targetCals to (every calendar whose name is {escape(calendar)})"
        if calendar
        else "set targetCals to every calendar"
    )
    script = f"""
    set out to ""
    set rangeStart to current date
    set rangeEnd to (current date) + ({days_ahead} * days)
    tell application "Calendar"
        {cal_clause}
        repeat with c in targetCals
            set evs to (every event of c whose start date ≥ rangeStart and start date ≤ rangeEnd)
            repeat with e in evs
                set out to out & (uid of e) & "\\t" & (summary of e) & "\\t" & ((start date of e) as string) & "\\t" & ((end date of e) as string) & "\\t" & (name of c) & linefeed
            end repeat
        end repeat
    end tell
    return out
    """
    r = await run(script, timeout=30.0)
    if not r.ok:
        return []
    events: list[dict] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            events.append({
                "id": parts[0],
                "title": parts[1],
                "starts_at": parts[2],
                "ends_at": parts[3],
                "calendar": parts[4],
            })
    events.sort(key=lambda e: e["starts_at"])
    return events

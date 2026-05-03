"""SDK MCP wrappers around tools/applescript/*.

Naming matches config/tiers.toml regexes:
  applescript__reminders__add       L2
  applescript__reminders__complete  L2
  applescript__reminders__list      L1 (matches *__list_)
  applescript__calendar__add        L2
  applescript__calendar__list       L1
  applescript__messages__send       L3
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from claude_agent_sdk import tool

from tools.applescript import calendar as cal_shim
from tools.applescript import messages as msg_shim
from tools.applescript import reminders as rem_shim


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # Accept ISO 8601 (with or without seconds, with or without TZ).
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@tool(
    "applescript__reminders__add",
    "Add a reminder to Reminders.app. Optional list_name (default user's primary), "
    "due (ISO 8601 datetime), notes.",
    {"title": str, "list_name": str, "due": str, "notes": str},
)
async def reminders_add(args: dict[str, Any]):
    res = await rem_shim.add(
        title=args["title"],
        list_name=args.get("list_name") or None,
        due=_parse_dt(args.get("due")),
        notes=args.get("notes") or None,
    )
    if not res["ok"]:
        return {"is_error": True, "content": [{"type": "text", "text": f"failed: {res['error']}"}]}
    return {"content": [{"type": "text", "text": f"added reminder id={res['id']}"}]}


@tool(
    "applescript__reminders__complete",
    "Mark a reminder complete by its id (returned from add or list).",
    {"reminder_id": str},
)
async def reminders_complete(args):
    res = await rem_shim.complete(args["reminder_id"])
    if not res["ok"]:
        return {"is_error": True, "content": [{"type": "text", "text": f"failed: {res['error']}"}]}
    return {"content": [{"type": "text", "text": "completed"}]}


@tool(
    "applescript__reminders__list",
    "List open (or completed) reminders. Optional list_name; default is user's primary.",
    {"list_name": str, "completed": bool, "limit": int},
)
async def reminders_list(args):
    rows = await rem_shim.list_reminders(
        list_name=args.get("list_name") or None,
        completed=bool(args.get("completed", False)),
        limit=int(args.get("limit") or 25),
    )
    if not rows:
        return {"content": [{"type": "text", "text": "(no reminders)"}]}
    lines = [f"- [{r['id'][:8]}] {r['name']} (due={r['due'] or '—'}) [{r['list']}]" for r in rows]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "applescript__calendar__add",
    "Add an event to Calendar.app. starts_at and (optional) ends_at are ISO 8601. "
    "calendar defaults to 'Home'.",
    {"title": str, "starts_at": str, "ends_at": str, "location": str, "notes": str, "calendar": str},
)
async def calendar_add(args):
    starts = _parse_dt(args["starts_at"])
    if not starts:
        return {"is_error": True, "content": [{"type": "text", "text": "starts_at required (ISO 8601)"}]}
    res = await cal_shim.add_event(
        title=args["title"],
        starts_at=starts,
        ends_at=_parse_dt(args.get("ends_at")),
        location=args.get("location") or None,
        notes=args.get("notes") or None,
        calendar=args.get("calendar") or cal_shim.DEFAULT_CAL,
    )
    if not res["ok"]:
        return {"is_error": True, "content": [{"type": "text", "text": f"failed: {res['error']}"}]}
    return {"content": [{"type": "text", "text": f"added event id={res['id']}"}]}


@tool(
    "applescript__calendar__list",
    "List upcoming calendar events. days_ahead defaults to 7.",
    {"days_ahead": int, "calendar": str},
)
async def calendar_list(args):
    events = await cal_shim.list_events(
        days_ahead=int(args.get("days_ahead") or 7),
        calendar=args.get("calendar") or None,
    )
    if not events:
        return {"content": [{"type": "text", "text": "(no events in window)"}]}
    lines = [f"- {e['starts_at']} — {e['title']} [{e['calendar']}]" for e in events[:50]]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "applescript__messages__send",
    "Send an iMessage. ALWAYS confirm recipient + body with the user before "
    "calling this — it is L3 (irreversible).",
    {"to": str, "body": str},
)
async def messages_send(args):
    res = await msg_shim.send(to=args["to"], body=args["body"])
    if not res["ok"]:
        return {"is_error": True, "content": [{"type": "text", "text": f"failed: {res['error']}"}]}
    return {"content": [{"type": "text", "text": f"sent to {args['to']}"}]}


def applescript_tools():
    return [
        reminders_add,
        reminders_complete,
        reminders_list,
        calendar_add,
        calendar_list,
        messages_send,
    ]

"""Reachy Mini curated tools (M10 stub).

These are the high-level capability surface the agent reasons about — the
underlying motor/audio/wake-word primitives live in the Reachy app codebase
and are NOT exposed here.

All tools currently return a "not deployed" message. When the Reachy hardware
arrives, replace each handler with a call into the Reachy app's HTTP API
(the same surface the Reachy app exposes for its own UI).
"""
from __future__ import annotations

from claude_agent_sdk import tool

NOT_DEPLOYED_MSG = (
    "Reachy is not deployed yet. This tool will work once the Reachy Mini "
    "is in the room and its app is reachable."
)


@tool(
    "reachy__tell_story",
    "Have Reachy tell a story to a child on a given theme. Used in storytelling "
    "sessions; bedtime, quiet time, or by request.",
    {"theme": str, "length_minutes": int, "audience_user_id": str},
)
async def tell_story(args):
    return {"is_error": True, "content": [{"type": "text", "text": NOT_DEPLOYED_MSG}]}


@tool(
    "reachy__ask_about_day",
    "Have Reachy ask the audience open-ended questions about their day. "
    "Use sparingly — mostly during natural quiet moments.",
    {"audience_user_id": str},
)
async def ask_about_day(args):
    return {"is_error": True, "content": [{"type": "text", "text": NOT_DEPLOYED_MSG}]}


@tool(
    "reachy__set_quiet_mode",
    "Tell Reachy to stop speaking and listen passively until further notice.",
    {},
)
async def set_quiet_mode(args):
    return {"is_error": True, "content": [{"type": "text", "text": NOT_DEPLOYED_MSG}]}


def reachy_tools():
    return [tell_story, ask_about_day, set_quiet_mode]

"""SDK MCP tools for conversation thread introspection + compacting.

Per design:
  thread__status   L1 — read current thread state
  thread__compact  L2 — close current thread with a summary you wrote
  thread__keep     L1 — explicit "I considered compacting and chose not to"
                       (no-op; useful for the audit trail and for telling the
                       user without committing to anything)
  thread__recent   L1 — see the last few closed threads' summaries
"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from orchestrator import threads
from orchestrator.auth import Principal


# Default surface inferred per-principal binding. The bot side passes
# 'telegram' through; CLI sessions don't currently use threads (they're
# spawn-per-task). We keep the surface argument open so future surfaces
# work without code changes.

def build_thread_tools(principal: Principal, *, default_surface: str = "telegram"):

    @tool(
        "thread__status",
        "Inspect the current conversational thread on this surface. Returns: "
        "whether one is open, when it started, turn count, minutes idle since "
        "the user's last message, and a preview of their last turn. Use this "
        "when you want to ground 'where are we' before answering.",
        {"surface": str},
    )
    async def thread_status(args):
        surface = (args.get("surface") or default_surface).lower()
        s = threads.status(surface, principal.user_id)
        return {"content": [{"type": "text", "text": json.dumps(s, indent=2, default=str)}]}

    @tool(
        "thread__compact",
        "Close the current thread with a summary you write. The summary is "
        "saved as a family-scope lesson memory so it shows up in future "
        "retrievals; the thread itself is marked closed and the next "
        "inbound message starts a new one. Use when:\n"
        "- the topic concluded naturally,\n"
        "- the conversation drifted into a clearly different topic,\n"
        "- turn_count is high (10+) and you want to free the context, or\n"
        "- you've been idle a long time and the prior thread is stale.\n"
        "The summary should be concrete: what was discussed, what was "
        "decided, what's open. It survives forever; treat it like a "
        "handoff to your future self.",
        {"summary": str, "surface": str, "reason": str},
    )
    async def thread_compact(args):
        surface = (args.get("surface") or default_surface).lower()
        ok, msg = threads.compact(
            surface, principal.user_id,
            summary=args.get("summary", ""),
            reason=args.get("reason") or "compacted",
        )
        return ({"content": [{"type": "text", "text": msg}]}
                if ok else
                {"is_error": True, "content": [{"type": "text", "text": msg}]})

    @tool(
        "thread__keep",
        "Affirm that you've thought about whether to compact and chose to "
        "stay in the current thread. No-op for state, but acknowledges the "
        "decision. Use when the user has paused but you expect them to "
        "return on the same topic.",
        {"reason": str},
    )
    async def thread_keep(args):
        s = threads.status(default_surface, principal.user_id)
        return {"content": [{"type": "text",
            "text": f"Keeping thread open. Current state: {json.dumps(s, default=str)}"}]}

    @tool(
        "thread__recent",
        "Show summaries of the last few closed threads on this surface. "
        "Useful for 'remind me where we landed last time' kinds of asks.",
        {"surface": str, "limit": int},
    )
    async def thread_recent(args):
        surface = (args.get("surface") or default_surface).lower()
        rows = threads.recent_closed(surface, principal.user_id,
                                     limit=int(args.get("limit") or 5))
        if not rows:
            return {"content": [{"type": "text", "text": "(no closed threads yet)"}]}
        lines = []
        for t in rows:
            lines.append(
                f"- #{t.id} closed {t.closed_at} ({t.turn_count} turns, "
                f"reason={t.closed_reason}): {t.summary or '(no summary)'}"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return [thread_status, thread_compact, thread_keep, thread_recent]

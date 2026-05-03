"""SDK MCP tools the agent calls to manage memory.

These are surfaced through the in-process MCP server mounted by
orchestrator/loop.py. Naming convention matches config/tiers.toml:

  - memory__read   -> L1
  - memory__write  -> L2
  - memory__forget -> L3 (memory__delete pattern in tiers.toml)

The principal-bound versions are produced by build_memory_tools() so the
gate's role/scope rules apply correctly per invocation.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from orchestrator import memory as mem
from orchestrator.auth import Principal


def build_memory_tools(principal: Principal):
    """Return a list of @tool-decorated callables bound to this principal.
    Promotion to family/system requires admin role (and `can_promote_memory`)
    — enforced here as a defense-in-depth check on top of the role ceiling."""

    @tool(
        "memory__read",
        "Search the agent's memory by natural-language query. Returns a list of "
        "matching facts, preferences, events, and lessons within the speaker's "
        "scope. Use this whenever the user asks about people, schedules, "
        "preferences, or prior decisions.",
        {"query": str, "top_k": int},
    )
    async def memory_read(args: dict[str, Any]):
        rows = mem.search(
            query=args["query"],
            principal=principal,
            top_k=int(args.get("top_k") or 8),
        )
        text = mem.to_jsonable(rows) if rows else "[]"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "memory__write",
        "Save a memory. Use kind='fact' for stable truths, 'preference' for "
        "user choices, 'event' for one-time happenings, 'lesson' for "
        "agent-internal corrections. scope must be 'user', 'family', or "
        "'system'. 'family' and 'system' require admin role.",
        {
            "content": str,
            "kind": str,
            "scope": str,  # 'user' | 'family' | 'system'
            "confidence": float,
        },
    )
    async def memory_write(args: dict[str, Any]):
        scope_in = (args.get("scope") or "user").lower()
        if scope_in == "user":
            scope = f"user:{principal.user_id}"
        elif scope_in in ("family", "system"):
            if principal.role != "admin":
                # adult cannot write 'system'; this also blocks 'family' for
                # adult — design says adult.can_promote_memory=true but
                # promotion is a separate L2 action; for M3 we keep family
                # writes admin-only and revisit when adult flow lands.
                return {
                    "is_error": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"role {principal.role} cannot write to scope {scope_in}",
                        }
                    ],
                }
            scope = scope_in
        else:
            return {
                "is_error": True,
                "content": [
                    {"type": "text", "text": f"invalid scope: {scope_in}"}
                ],
            }

        kind = (args.get("kind") or "fact").lower()
        confidence = float(args.get("confidence") or 1.0)
        content = (args.get("content") or "").strip()
        if not content:
            return {
                "is_error": True,
                "content": [{"type": "text", "text": "content is required"}],
            }

        contradictions = mem.find_contradictions(content, principal)
        mid = mem.write(
            content=content,
            scope=scope,
            kind=kind,
            created_by=principal.user_id,
            confidence=confidence,
        )

        msg = f"saved memory id={mid} scope={scope} kind={kind} conf={confidence}"
        if contradictions:
            top = contradictions[0]
            msg += (
                f"\n[contradiction warning] this looks similar to existing "
                f"memory id={top.id}: {top.content!r}. Surface to the user."
            )
        return {"content": [{"type": "text", "text": msg}]}

    @tool(
        "memory__forget",
        "Soft-delete a memory by id. Sets confidence to 0 and removes from the "
        "vector index. Use when the user says 'forget that' or corrects a "
        "previously saved fact.",
        {"memory_id": int},
    )
    async def memory_forget(args: dict[str, Any]):
        mid = int(args["memory_id"])
        existing = mem.read_by_id(mid)
        if not existing:
            return {
                "is_error": True,
                "content": [{"type": "text", "text": f"no memory with id={mid}"}],
            }
        # Scope check: cannot forget another user's memory unless admin.
        if existing.scope.startswith("user:") and existing.scope != f"user:{principal.user_id}":
            if principal.role != "admin":
                return {
                    "is_error": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"cannot forget memory in scope {existing.scope}",
                        }
                    ],
                }
        ok = mem.forget(mid)
        return {
            "content": [
                {"type": "text", "text": f"forgot id={mid} (was: {existing.content!r})" if ok else f"no-op for id={mid}"}
            ]
        }

    return [memory_read, memory_write, memory_forget]

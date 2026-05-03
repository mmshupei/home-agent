"""SDK MCP tools for Ren's mutable skill registry.

Naming matches config/tiers.toml:
  skill__list      L1 (read)
  skill__describe  L1 (read)
  skill__register  L2 (DB write)
  skill__update    L2 (DB write)
  skill__invoke    L1 (returns prompt text — agent then acts via normal tools,
                       which fire the gate per constituent action)
  skill__delete    L3 (capability removal — irreversible from Ren's POV
                       even though the row is soft-deleted)
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from orchestrator import skills
from orchestrator.auth import Principal


def build_skill_tools(principal: Principal):
    """Per-principal binding so created_by gets recorded honestly."""

    @tool(
        "skill__list",
        "List all your registered skills with their one-line descriptions. "
        "Use this when the user asks 'what can you do' or when you want to "
        "check whether a procedure already exists before re-deriving it.",
        {},
    )
    async def skill_list(args):
        rows = skills.list_all()
        if not rows:
            return {"content": [{"type": "text",
                "text": "(no skills registered yet — use skill__register to save one)"}]}
        lines = [f"- `{s.name}`: {s.description}  (invoked {s.invoke_count}x)" for s in rows]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "skill__describe",
        "Show the full prompt and metadata for a specific skill. Use this "
        "when you've decided to invoke a skill but want to see exactly what "
        "it instructs you to do.",
        {"name": str},
    )
    async def skill_describe(args):
        s = skills.get(args["name"])
        if not s:
            return {"is_error": True,
                "content": [{"type": "text", "text": f"no skill named {args['name']!r}"}]}
        return {"content": [{"type": "text", "text":
            f"# {s.name}\n\n"
            f"{s.description}\n\n"
            f"Created by {s.created_by} on {s.created_at} · invoked {s.invoke_count}x\n\n"
            f"## Prompt\n\n{s.prompt}"
        }]}

    @tool(
        "skill__register",
        "Save a new skill — a named procedure you'll be able to recall and "
        "follow in future sessions. Use this when you find yourself doing "
        "something useful enough that re-deriving it next time would be "
        "wasteful. The 'prompt' field is what you'll read on invoke; write "
        "it to your future self. name must be lowercase a-z0-9_, 2-41 chars.",
        {
            "name": str,
            "description": str,
            "prompt": str,
            "notes": str,
        },
    )
    async def skill_register(args):
        ok, msg = skills.register(
            name=args.get("name", ""),
            description=args.get("description", ""),
            prompt=args.get("prompt", ""),
            created_by=principal.user_id if principal.user_id != "shupei" else "ren",
            notes=args.get("notes") or None,
        )
        return ({"content": [{"type": "text", "text": msg}]}
                if ok else
                {"is_error": True, "content": [{"type": "text", "text": msg}]})

    @tool(
        "skill__update",
        "Replace the prompt of an existing skill (description and name stay). "
        "Use this to refine a skill after using it a few times — sharper "
        "instructions, removed dead branches, added gotchas you discovered.",
        {"name": str, "prompt": str},
    )
    async def skill_update(args):
        ok, msg = skills.update_prompt(
            name=args.get("name", ""),
            prompt=args.get("prompt", ""),
            edited_by="ren" if principal.user_id == "shupei" else principal.user_id,
        )
        return ({"content": [{"type": "text", "text": msg}]}
                if ok else
                {"is_error": True, "content": [{"type": "text", "text": msg}]})

    @tool(
        "skill__invoke",
        "Recall and execute a saved skill. Returns the skill's prompt; you "
        "then follow those instructions in the current turn using whatever "
        "tools are appropriate. Each tool you call from inside the skill "
        "still goes through the normal gate.",
        {"name": str},
    )
    async def skill_invoke(args):
        s = skills.get(args["name"])
        if not s:
            return {"is_error": True,
                "content": [{"type": "text", "text": f"no skill named {args['name']!r}"}]}
        skills.touch(s.name)
        return {"content": [{"type": "text", "text":
            f"## Invoking skill: {s.name}\n\n"
            f"_{s.description}_\n\n"
            f"Follow these instructions for this turn. When you're done, "
            f"return to the user's actual question naturally.\n\n"
            f"---\n\n{s.prompt}"
        }]}

    @tool(
        "skill__delete",
        "Disable a skill (soft delete — the row stays for audit). The skill "
        "is removed from skill__list and cannot be invoked. Use sparingly: "
        "this removes a capability you've decided is no longer useful.",
        {"name": str},
    )
    async def skill_delete(args):
        ok, msg = skills.delete(name=args.get("name", ""))
        return ({"content": [{"type": "text", "text": msg}]}
                if ok else
                {"is_error": True, "content": [{"type": "text", "text": msg}]})

    return [skill_list, skill_describe, skill_register, skill_update,
            skill_invoke, skill_delete]

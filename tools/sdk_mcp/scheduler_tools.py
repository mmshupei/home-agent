"""SDK MCP tools for the pulse-driven scheduler.

Naming matches config/tiers.toml:
  schedule__list      L1 (read)
  schedule__register  L2 (DB write)
  schedule__cancel    L2 (DB write — soft-delete)
  schedule__fire_now  L2 (out-of-band LLM spend; may fire household actions)

The tools work directly against orchestrator.scheduler.store. ``fire_now``
needs the in-process Heartbeat instance to dispatch a subagent; we look it
up via a module-level registry keyed by the bot process. If no heartbeat
is registered (e.g. running from CLI rather than the bot), ``fire_now``
falls back to a synchronous one-off run via the runner directly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from claude_agent_sdk import tool

from orchestrator.auth import Principal
from orchestrator.scheduler import store
from orchestrator.scheduler.runner import run_due_action


# Process-wide registry: telegram_bot.py sets this on startup so the
# schedule__fire_now tool can reach the live heartbeat instance.
_HEARTBEAT_REF: Any = None


def set_heartbeat(heartbeat: Any) -> None:
    """Called by triggers/telegram_bot.py post_init."""
    global _HEARTBEAT_REF
    _HEARTBEAT_REF = heartbeat


def clear_heartbeat() -> None:
    """Called by triggers/telegram_bot.py post_shutdown."""
    global _HEARTBEAT_REF
    _HEARTBEAT_REF = None


def _format_action_line(a: store.ScheduledAction) -> str:
    when = (
        a.cron_expr
        if a.cron_expr
        else (a.run_once_at.isoformat() if a.run_once_at else "?")
    )
    last = a.last_fired_at.isoformat() if a.last_fired_at else "never"
    status_tag = ""
    if a.last_status == "error":
        status_tag = " [last=error]"
    elif a.last_status == "ok":
        status_tag = " [last=ok]"
    return (
        f"- `{a.name}` ({a.target_kind}={a.target!r:.60})  "
        f"when=`{when}` tz={a.timezone}  last_fired={last}{status_tag}"
    )


def build_scheduler_tools(principal: Principal):
    """Per-principal binding so ownership/created_by are recorded honestly."""

    @tool(
        "schedule__list",
        "List scheduled actions. Shows name, target, cron/run-once, last "
        "fire time, and last status. Use this when the user asks 'what "
        "scheduled actions do I have?' or before registering a new one.",
        {},
    )
    async def schedule_list(args):
        # Admins see everything; non-admins see only their own.
        owner = None if principal.role == "admin" else principal.user_id
        rows = store.list_all(owner_user=owner, enabled_only=True)
        if not rows:
            return {"content": [{
                "type": "text",
                "text": "(no enabled scheduled actions)",
            }]}
        lines = [_format_action_line(a) for a in rows]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "schedule__register",
        "Register a recurring or one-shot scheduled action. Provide either "
        "cron_expr (5-field cron like '30 21 * * *') OR run_once_at "
        "(ISO 8601 UTC timestamp), not both. target_kind='skill' invokes a "
        "saved skill by name; target_kind='prompt' runs the target text as "
        "a free-form heartbeat instruction. The action fires at most once "
        "per cron period; an isolated subagent acts and posts a one-line "
        "Telegram notification to the owner.",
        {
            "name": str,
            "cron_expr": str,
            "run_once_at": str,
            "target_kind": str,
            "target": str,
            "timezone": str,
            "owner_user": str,
            "notes": str,
        },
    )
    async def schedule_register(args):
        cron_expr = (args.get("cron_expr") or "").strip() or None
        run_once_str = (args.get("run_once_at") or "").strip() or None
        run_once_at: Optional[datetime] = None
        if run_once_str:
            try:
                # Accept "Z" suffix and naive forms; coerce to UTC.
                s = run_once_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                run_once_at = dt.astimezone(timezone.utc)
            except Exception as e:
                return {"is_error": True, "content": [{
                    "type": "text",
                    "text": f"could not parse run_once_at {run_once_str!r}: {e}",
                }]}

        # Default owner = caller; admins may explicitly target another user.
        owner_user = (args.get("owner_user") or "").strip() or principal.user_id
        if owner_user != principal.user_id and principal.role != "admin":
            return {"is_error": True, "content": [{
                "type": "text",
                "text": "only admins can register actions for another user",
            }]}

        ok, msg = store.register(
            name=args.get("name", ""),
            cron_expr=cron_expr,
            run_once_at=run_once_at,
            target_kind=args.get("target_kind", ""),
            target=args.get("target", ""),
            owner_user=owner_user,
            timezone_name=(args.get("timezone") or "America/Los_Angeles"),
            created_by=(
                "ren" if principal.user_id == "shupei" else principal.user_id
            ),
            notes=(args.get("notes") or None),
        )
        return (
            {"content": [{"type": "text", "text": msg}]}
            if ok
            else {"is_error": True, "content": [{"type": "text", "text": msg}]}
        )

    @tool(
        "schedule__cancel",
        "Disable a scheduled action by name (soft delete). The row remains "
        "for audit; re-registering with the same name re-enables and "
        "overwrites it.",
        {"name": str},
    )
    async def schedule_cancel(args):
        name = (args.get("name") or "").strip()
        action = store.get(name)
        if action is None:
            return {"is_error": True, "content": [{
                "type": "text", "text": f"no scheduled action named {name!r}",
            }]}
        if action.owner_user != principal.user_id and principal.role != "admin":
            return {"is_error": True, "content": [{
                "type": "text",
                "text": "only the owner or an admin can cancel this action",
            }]}
        ok, msg = store.cancel(name=name)
        return (
            {"content": [{"type": "text", "text": msg}]}
            if ok
            else {"is_error": True, "content": [{"type": "text", "text": msg}]}
        )

    @tool(
        "schedule__fire_now",
        "Fire a scheduled action immediately, regardless of its next cron "
        "tick. Useful for smoke-testing a newly-registered action. The "
        "subagent runs out-of-band and posts its status line to the owner; "
        "this tool returns a short acknowledgement.",
        {"name": str},
    )
    async def schedule_fire_now(args):
        name = (args.get("name") or "").strip()
        action = store.get(name)
        if action is None or not action.enabled:
            return {"is_error": True, "content": [{
                "type": "text",
                "text": f"no enabled scheduled action named {name!r}",
            }]}
        if action.owner_user != principal.user_id and principal.role != "admin":
            return {"is_error": True, "content": [{
                "type": "text",
                "text": "only the owner or an admin can fire this action",
            }]}

        if _HEARTBEAT_REF is not None:
            try:
                ok = await _HEARTBEAT_REF.force_fire(name)
            except Exception as e:
                return {"is_error": True, "content": [{
                    "type": "text",
                    "text": f"heartbeat dispatch failed: {type(e).__name__}: {e}",
                }]}
            if not ok:
                return {"is_error": True, "content": [{
                    "type": "text",
                    "text": f"heartbeat could not find {name!r}",
                }]}
            return {"content": [{
                "type": "text",
                "text": f"fired {name!r} via heartbeat — owner will get the status line shortly",
            }]}

        # CLI / non-bot fallback: run synchronously, no Telegram bridge.
        async def _stdout_notify(owner_user: str, text: str) -> None:
            print(f"[scheduler/fire_now] -> {owner_user}: {text}")

        now_utc = datetime.now(timezone.utc)
        await run_due_action(
            action=action,
            expected_prev_fire=now_utc,
            on_notify=_stdout_notify,
        )
        return {"content": [{
            "type": "text",
            "text": f"fired {name!r} (no heartbeat live; status printed to stdout)",
        }]}

    return [schedule_list, schedule_register, schedule_cancel, schedule_fire_now]

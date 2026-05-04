"""Heartbeat-spawned subagent runner.

When the heartbeat (orchestrator.scheduler.heartbeat) detects a due action,
it calls ``run_due_action`` for each. Each call spins up a *separate*
ClaudeSDKClient — completely isolated from the user's resident agent — runs
under the ``scheduled`` profile (L1+L2 allow, L3 deny), and returns its
final-text reply for the heartbeat to push as a Telegram notification.

Key invariants:
- Subagents must NOT submit into the user's resident. They get their own
  Session, their own episode, and their own out-of-band Telegram message.
- ``mark_fired`` uses the optimistic-update pattern (advisory lock on
  ``expected_prev_fire``) so two heartbeats can never double-fire the same
  period.
- Errors do NOT crash the heartbeat. Each subagent invocation is wrapped
  and any exception becomes a "(scheduler error: …)" notification.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from orchestrator import episodes
from orchestrator.auth import Principal
from orchestrator.db import connect
from orchestrator.gating import build_gate_hook, get_profile
from orchestrator.prompts import prompt_telegram
from orchestrator.session import Session

from .store import ScheduledAction, mark_fired

# orchestrator.loop is imported lazily inside run_due_action to break the
# cycle: loop → scheduler_tools → runner → loop. The runner only needs
# build_options at invocation time, never at import time.

SCHEDULER_SUBAGENT_SYSTEM_PROMPT = """You are the household agent's scheduler subagent.

A scheduled action just came due. The user is NOT in a live conversation
with you right now — you are running out-of-band, triggered by the heartbeat.

Your job:
1. Carry out the action's target. If target_kind is 'skill', invoke
   skill__describe to read the skill's prompt and follow it. If target_kind
   is 'prompt', treat the target text as your task directly.
2. When done, return ONE short status line summarizing what happened (or
   what failed). That single line is what gets pushed to the owner via
   Telegram. Keep it <= 200 characters. No reasoning chatter, no markdown
   headers — just the line. Examples:
       "Dimmed 3/3 Living Room bulbs to 30%."
       "Skipped — all bulbs were already off."
       "Failed: homectl binary missing at expected path."

Constraints:
- Don't ask follow-up questions. There's no human at the other end during
  this turn. If something is ambiguous, do the safe thing (skip or report).
- Don't write to memory unless the action explicitly requires it.
- Don't open a new bead unless the action requires it.
- Stay focused. Exit as soon as the status line is composed.
"""


def _principal_for_user(user_id: str) -> Principal:
    """Resolve a Principal from the users table for this scheduled action's
    owner. The role determines memory scope and gate behavior.
    """
    with connect() as c:
        r = c.execute(
            "SELECT id, name, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not r:
        raise RuntimeError(f"unknown owner_user {user_id!r}")
    return Principal(
        user_id=r["id"], name=r["name"], role=r["role"], token_label="scheduler"
    )


async def _deny_no_human(tool_input: dict, tier: int, principal: Principal) -> bool:
    """Default-deny callback. The scheduled subagent has no human at the
    keyboard, so any prompt-on-tool-use means we hit a gate; deny it."""
    return False


def _format_user_payload(action: ScheduledAction) -> str:
    tz = ZoneInfo(action.timezone)
    now_local = datetime.now(tz).strftime("%a %Y-%m-%d %H:%M %Z")
    return (
        f"Heartbeat {now_local}.\n\n"
        f"Due action: {action.name}\n"
        f"target_kind: {action.target_kind}\n"
        f"target: {action.target}\n\n"
        f"Decide and act, then return ONE short status line."
    )


async def run_due_action(
    *,
    action: ScheduledAction,
    expected_prev_fire: datetime,
    on_notify: Callable[[str, str], Awaitable[None]],
    model: str = "claude-sonnet-4-5",
) -> str:
    """Run a single due action in an isolated SDK client. Returns the
    final-text status line. Best-effort: any exception is caught and turned
    into an error string + recorded via mark_fired(status='error').
    """
    fired_at = datetime.now(timezone.utc)
    final_text = ""
    cost_usd: Optional[float] = None
    status = "ok"
    error: Optional[str] = None

    try:
        from orchestrator.loop import build_options  # lazy: break cycle
        principal = _principal_for_user(action.owner_user)
        profile_name = "scheduled"
        profile_obj = get_profile(profile_name)
        session = Session.new(
            task=f"heartbeat:{action.name}",
            profile=profile_name,
            principal=principal,
        )
        episode_id = episodes.start(
            source="scheduler", principal=principal, session_id=session.id
        )

        gate = build_gate_hook(
            session,
            profile_obj,
            principal,
            prompt_cli=_deny_no_human,
            prompt_push=prompt_telegram,
        )

        options = build_options(
            principal=principal,
            system_prompt=SCHEDULER_SUBAGENT_SYSTEM_PROMPT,
            gate=gate,
            model=model,
            source="scheduler",
        )

        user_payload = _format_user_payload(action)

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user_payload)
                async for msg in client.receive_response():
                    session.record(msg)
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                final_text += block.text
                    elif isinstance(msg, ResultMessage):
                        cost_usd = getattr(msg, "total_cost_usd", None)
                        if getattr(msg, "result", None):
                            final_text = msg.result
        finally:
            try:
                session.finalize(
                    final_message=final_text, cost_usd=cost_usd,
                )
            except Exception:
                pass
            try:
                episodes.close(
                    episode_id,
                    transcript=(
                        f"HEARTBEAT: {action.name}\n\nAGENT: {final_text}"
                    ),
                    summary=(final_text[:200] if final_text else None),
                    affect={
                        "cost_usd": cost_usd,
                        "profile": profile_name,
                        "scheduled_action": action.name,
                    },
                )
            except Exception:
                pass

    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"
        final_text = f"(scheduler error firing {action.name}: {error})"
        print(f"[scheduler] {action.name} crashed: {error!r}")

    # Optimistic update — only succeeds if no concurrent worker already
    # marked this period. If we lose the race, don't notify (the winning
    # worker will).
    won = mark_fired(
        name=action.name,
        fired_at=fired_at,
        status=status,
        error=error,
        cost_usd=cost_usd,
        expected_prev_fire=expected_prev_fire,
    )
    if not won:
        print(f"[scheduler] {action.name} mark_fired raced; skipping notify")
        return final_text

    # Always send something so the owner knows the heartbeat fired. Truncate
    # very long replies — the notification is meant to be a glance.
    snippet = (final_text or f"({action.name} fired silently)").strip()
    if len(snippet) > 600:
        snippet = snippet[:597] + "…"
    try:
        await on_notify(action.owner_user, snippet)
    except Exception as e:
        print(f"[scheduler] notify failed for {action.name}: {e!r}")
    return final_text


async def run_due_actions(
    *,
    due: list[tuple[ScheduledAction, datetime]],
    on_notify: Callable[[str, str], Awaitable[None]],
    model: str = "claude-sonnet-4-5",
) -> None:
    """Convenience: serially run a batch of due actions. Sequential rather
    than parallel to keep cost predictable and avoid simultaneous Telegram
    notifications stepping on each other; revisit if N>5 becomes common."""
    for action, prev in due:
        try:
            await run_due_action(
                action=action,
                expected_prev_fire=prev,
                on_notify=on_notify,
                model=model,
            )
        except Exception as e:
            # mark_fired already happened (or didn't, in which case the next
            # tick will retry). Log and move to the next.
            print(f"[scheduler] uncaught run_due_action error: {e!r}")

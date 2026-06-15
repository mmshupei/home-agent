"""Orchestrator entrypoint.

M2: PreToolUse gating wired in via HookMatcher. permission_mode is now
'default' so the gate is the source of truth. No memory yet (M3), no critic
(M6), no real MCP servers beyond the in-process test SDK MCP (M4 brings the
real ones).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

from . import critic, episodes, morning_surface
from .auth import Principal
from .gating import build_gate_hook, get_profile
from .memory import retrieve_context
from .prompts import prompt_telegram, prompt_terminal
from .session import Session
from tools.sdk_mcp.applescript_tools import applescript_tools
from tools.sdk_mcp.finance_tools import finance_tools
from tools.sdk_mcp.ha_tools import ha_tools
from tools.sdk_mcp.memory_tools import build_memory_tools
from tools.sdk_mcp.reachy_tools import reachy_tools
from tools.sdk_mcp.routines import routines
from tools.sdk_mcp.scheduler_tools import build_scheduler_tools
from tools.sdk_mcp.skill_tools import build_skill_tools
from tools.sdk_mcp.test_tools import test_tools
from tools.sdk_mcp.thread_tools import build_thread_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_DOMAINS = ["home", "finance", "chores", "browse"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _identity_block(principal: Principal, *, with_time: bool) -> str:
    """The 'who am I talking to' block. Time is omitted in the resident's
    static system prompt so the prompt is stable across turns; it's then
    re-injected per-turn via compose_per_turn_header()."""
    head = (
        f"## Current speaker\n"
        f"You are speaking with {principal.name} (user_id={principal.user_id}, "
        f"role={principal.role}). Respect their scope: a {principal.role} user "
        f"sees system + family memory plus their own user-scoped memory."
    )
    if with_time:
        now = datetime.now(PACIFIC).strftime("%A, %Y-%m-%d %H:%M %Z")
        return head + f"\nCurrent time: {now}."
    return head


def compose_prompt(domains: list[str], principal: Principal, task: str) -> str:
    """Full per-invocation system prompt for one-shot run(). Combines the
    static and dynamic sections in the order Ren is used to seeing them."""
    base = _read(PROMPTS_DIR / "base.md")
    domain_blocks = [_read(PROMPTS_DIR / "domains" / f"{d}.md") for d in domains]
    identity = _identity_block(principal, with_time=True)
    context_block = retrieve_context(task, principal)
    cautions = critic.render_cautions_block()
    morning = morning_surface.render(principal)

    # Order matters: identity → morning's pending Q (most actionable) →
    # critic cautions → memory context → domain stylings.
    parts = [base, identity, morning, cautions, context_block, *domain_blocks]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def compose_static_system_prompt(domains: list[str], principal: Principal) -> str:
    """Stable system-prompt portion for a long-lived ResidentAgent. Excludes
    anything that should refresh per turn: current time, morning surface
    (recomputes each call), and per-task memory retrieval. The dynamic bits
    are prepended to each user message via compose_per_turn_header()."""
    base = _read(PROMPTS_DIR / "base.md")
    domain_blocks = [_read(PROMPTS_DIR / "domains" / f"{d}.md") for d in domains]
    identity = _identity_block(principal, with_time=False)
    cautions = critic.render_cautions_block()
    parts = [base, identity, cautions, *domain_blocks]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def compose_per_turn_header(task: str, principal: Principal) -> str:
    """Live, per-turn preamble prepended to the user's message in the
    resident model. Carries the current time, morning-surface pending
    question (if any), and a fresh memory retrieval scoped to this turn's
    task. Cheap to recompute; freshness matters more than cache reuse here."""
    now = datetime.now(PACIFIC).strftime("%A, %Y-%m-%d %H:%M %Z")
    now_block = f"## Now\n{now}"
    morning = morning_surface.render(principal)
    context_block = retrieve_context(task, principal)
    parts = [now_block, morning, context_block]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def build_options(
    *,
    principal: Principal,
    system_prompt: str,
    gate,
    model: str,
    source: str,
) -> ClaudeAgentOptions:
    """Construct ClaudeAgentOptions for either a one-shot run or a resident
    client. Spins up the in-process MCP servers (per-principal binding for
    memory/skill/thread; the rest are stateless). Tools whose backends
    aren't configured (e.g. HA absent) report an error from inside the
    tool so the agent can surface it cleanly."""
    memory_server = create_sdk_mcp_server(
        "memory", "0.1.0", tools=build_memory_tools(principal)
    )
    test_server = create_sdk_mcp_server("agent_test", "0.1.0", tools=test_tools())
    apple_server = create_sdk_mcp_server("apple", "0.1.0", tools=applescript_tools())
    finance_server = create_sdk_mcp_server("finance", "0.1.0", tools=finance_tools())
    ha_server = create_sdk_mcp_server("ha", "0.1.0", tools=ha_tools())
    routines_server = create_sdk_mcp_server("routines", "0.1.0", tools=routines())
    reachy_server = create_sdk_mcp_server("reachy", "0.1.0", tools=reachy_tools())
    skill_server = create_sdk_mcp_server("skill", "0.1.0", tools=build_skill_tools(principal))
    scheduler_server = create_sdk_mcp_server(
        "scheduler", "0.1.0", tools=build_scheduler_tools(principal),
    )
    thread_server = create_sdk_mcp_server(
        "thread", "0.1.0",
        tools=build_thread_tools(principal, default_surface=source or "telegram"),
    )

    # Auth: subscription (claude /login) by default; set AGENT_USE_SUBSCRIPTION=0
    # to fall back to the API key in the parent shell env.
    use_sub = os.environ.get("AGENT_USE_SUBSCRIPTION", "1") != "0"
    sdk_env = {"ANTHROPIC_API_KEY": ""} if use_sub else {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")
    }

    return ClaudeAgentOptions(
        model=model,
        fallback_model=["claude-opus-4-8", "claude-sonnet-4-6"],
        system_prompt=system_prompt,
        mcp_servers={
            "agent_test": test_server,
            "memory": memory_server,
            "apple": apple_server,
            "finance": finance_server,
            "ha": ha_server,
            "routines": routines_server,
            "reachy": reachy_server,
            "skill": skill_server,
            "scheduler": scheduler_server,
            "thread": thread_server,
        },
        permission_mode="default",
        cwd=str(REPO_ROOT),
        env=sdk_env,
        hooks={
            "PreToolUse": [HookMatcher(hooks=[gate])],
        },
        # Claude Code interop: load user-level settings (enabled marketplaces,
        # installed plugins) and expose all discovered skills via the native
        # `Skill` tool. Coexists with our DB-backed `skill__*` MCP surface —
        # CC skills are read-only filesystem; ours are Ren's own procedures.
        # PreToolUse gate still applies to every tool a CC skill might fire.
        setting_sources=["user"],
        skills="all",
    )


async def run(
    task: str,
    principal: Principal,
    profile: str = "interactive",
    domains: list[str] | None = None,
    model: str = "claude-fable-5",
    source: str | None = None,
) -> str:
    """Run one agent invocation. Returns the final assistant text.

    `source` tags the resulting episode (M9). Defaults inferred from profile:
    interactive→cli, mobile→mobile, home→home, unattended→launchd.
    """
    profile_obj = get_profile(profile)
    domains = domains or profile_obj.domains or DEFAULT_DOMAINS
    session = Session.new(task=task, profile=profile, principal=principal)
    if source is None:
        source = {
            "interactive": "cli", "mobile": "mobile",
            "home": "home", "unattended": "launchd",
            "embedded": "reachy",
        }.get(profile, "cli")
    episode_id = episodes.start(
        source=source, principal=principal, session_id=session.id
    )

    gate = build_gate_hook(
        session,
        profile_obj,
        principal,
        prompt_cli=prompt_terminal,
        prompt_push=prompt_telegram,
    )

    options = build_options(
        principal=principal,
        system_prompt=compose_prompt(domains, principal, task),
        gate=gate,
        model=model,
        source=source,
    )

    final_text = ""
    token_count: int | None = None
    cost_usd: float | None = None

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            async for msg in client.receive_response():
                session.record(msg)
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final_text += block.text
                elif isinstance(msg, ResultMessage):
                    cost_usd = getattr(msg, "total_cost_usd", None)
                    usage = getattr(msg, "usage", None) or {}
                    if isinstance(usage, dict):
                        token_count = (usage.get("input_tokens") or 0) + (
                            usage.get("output_tokens") or 0
                        ) or None
                    if getattr(msg, "result", None):
                        final_text = msg.result
    finally:
        session.finalize(
            final_message=final_text, token_count=token_count, cost_usd=cost_usd
        )
        # Close the M9 episode. Transcript = task + final reply (the per-tool
        # detail lives in the session jsonl; the episode just needs the gist).
        try:
            transcript = f"USER: {task}\n\nAGENT: {final_text}"
            episodes.close(
                episode_id,
                transcript=transcript,
                summary=final_text[:200] if final_text else None,
                affect={
                    "cost_usd": cost_usd,
                    "token_count": token_count,
                    "profile": profile,
                },
            )
        except Exception:
            pass  # episode close failures must not break the response path
        # Schedule the post-hoc critic. Fire-and-forget: in a short-lived CLI
        # process the loop closes before completion (skipped). In long-lived
        # contexts (HTTP server, REPL) it runs to completion in the background.
        critic.schedule(session)

    return final_text or "(no response)"

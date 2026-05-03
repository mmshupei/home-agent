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

from .auth import Principal
from .gating import build_gate_hook, get_profile
from .memory import retrieve_context
from .prompts import prompt_pushover_stub, prompt_terminal
from .session import Session
from tools.sdk_mcp.applescript_tools import applescript_tools
from tools.sdk_mcp.finance_tools import finance_tools
from tools.sdk_mcp.ha_tools import ha_tools
from tools.sdk_mcp.memory_tools import build_memory_tools
from tools.sdk_mcp.routines import routines
from tools.sdk_mcp.test_tools import test_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_DOMAINS = ["home", "finance", "chores", "browse"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def compose_prompt(domains: list[str], principal: Principal, task: str) -> str:
    base = _read(PROMPTS_DIR / "base.md")
    domain_blocks = [_read(PROMPTS_DIR / "domains" / f"{d}.md") for d in domains]
    now = datetime.now(PACIFIC).strftime("%A, %Y-%m-%d %H:%M %Z")

    identity = (
        f"## Current speaker\n"
        f"You are speaking with {principal.name} (user_id={principal.user_id}, "
        f"role={principal.role}). Respect their scope: a {principal.role} user "
        f"sees system + family memory plus their own user-scoped memory.\n"
        f"Current time: {now}.\n"
    )

    context_block = retrieve_context(task, principal)

    parts = [base, identity, context_block, *domain_blocks]
    return "\n\n".join(p.strip() for p in parts if p.strip())


async def run(
    task: str,
    principal: Principal,
    profile: str = "interactive",
    domains: list[str] | None = None,
    model: str = "claude-opus-4-7",
) -> str:
    """Run one agent invocation. Returns the final assistant text."""
    profile_obj = get_profile(profile)
    domains = domains or profile_obj.domains or DEFAULT_DOMAINS
    session = Session.new(task=task, profile=profile, principal=principal)

    gate = build_gate_hook(
        session,
        profile_obj,
        principal,
        prompt_cli=prompt_terminal,
        prompt_push=prompt_pushover_stub,
    )

    # In-process MCP servers. Per-principal binding for memory; the rest are
    # stateless. Tools whose backends aren't configured (e.g. HA absent) report
    # an error from inside the tool so the agent can surface it cleanly.
    memory_server = create_sdk_mcp_server(
        "memory", "0.1.0", tools=build_memory_tools(principal)
    )
    test_server = create_sdk_mcp_server("agent_test", "0.1.0", tools=test_tools())
    apple_server = create_sdk_mcp_server("apple", "0.1.0", tools=applescript_tools())
    finance_server = create_sdk_mcp_server("finance", "0.1.0", tools=finance_tools())
    ha_server = create_sdk_mcp_server("ha", "0.1.0", tools=ha_tools())
    routines_server = create_sdk_mcp_server("routines", "0.1.0", tools=routines())

    # Auth: subscription (claude /login) by default; set AGENT_USE_SUBSCRIPTION=0
    # to fall back to the API key in the parent shell env.
    use_sub = os.environ.get("AGENT_USE_SUBSCRIPTION", "1") != "0"
    sdk_env = {"ANTHROPIC_API_KEY": ""} if use_sub else {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")
    }

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=compose_prompt(domains, principal, task),
        mcp_servers={
            "agent_test": test_server,
            "memory": memory_server,
            "apple": apple_server,
            "finance": finance_server,
            "ha": ha_server,
            "routines": routines_server,
        },
        permission_mode="default",
        cwd=str(REPO_ROOT),
        env=sdk_env,
        hooks={
            "PreToolUse": [HookMatcher(hooks=[gate])],
        },
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

    return final_text or "(no response)"

"""Orchestrator entrypoint.

M1: minimal. SDK client + composed system prompt. No memory, no gating, no
critic. Those land in M2 / M3 / M6.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .auth import Principal
from .session import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "config" / "prompts"
PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_DOMAINS = ["home", "finance", "chores", "browse"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def compose_prompt(domains: list[str], principal: Principal) -> str:
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

    parts = [base, identity, *domain_blocks]
    return "\n\n".join(p.strip() for p in parts if p.strip())


async def run(
    task: str,
    principal: Principal,
    profile: str = "interactive",
    domains: list[str] | None = None,
    model: str = "claude-opus-4-7",
) -> str:
    """Run one agent invocation. Returns the final assistant text."""
    domains = domains or DEFAULT_DOMAINS
    session = Session.new(task=task, profile=profile, principal=principal)

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=compose_prompt(domains, principal),
        # M1: no MCP servers, no hooks, no allowed_tools restriction. The SDK's
        # default Claude-Code tool preset is used so basic file/web operations
        # work out of the box.
        permission_mode="bypassPermissions",
        cwd=str(REPO_ROOT),
        env={"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")},
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
                        # Prefer the SDK's final result string when present.
                        final_text = msg.result
    finally:
        session.finalize(
            final_message=final_text, token_count=token_count, cost_usd=cost_usd
        )

    return final_text or "(no response)"

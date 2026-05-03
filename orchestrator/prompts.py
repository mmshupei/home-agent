"""Approval prompts: terminal (rich) and Pushover (real, M5).

Profiles select which to call; the gate stays IO-agnostic.
"""
from __future__ import annotations

import asyncio
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax

from . import approvals, pushover
from .auth import Principal

_console = Console(file=sys.stderr)


def _summary(tool_input: dict) -> str:
    """Single-line human description for the prompt header."""
    name = tool_input.get("tool_name", "?")
    args = tool_input.get("tool_input", {})
    if isinstance(args, dict):
        keys = ", ".join(f"{k}={_short(v)}" for k, v in list(args.items())[:3])
        return f"{name}({keys})"
    return name


def _short(v) -> str:
    s = repr(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _render(tool_input: dict, tier: int, principal: Principal) -> Panel:
    body = json.dumps(tool_input.get("tool_input", {}), indent=2, default=str)
    return Panel(
        Syntax(body, "json", line_numbers=False, word_wrap=True),
        title=f"[bold]L{tier}[/bold] tool: [cyan]{tool_input.get('tool_name','?')}[/cyan]   "
        f"speaker: [magenta]{principal.name}[/magenta] ({principal.role})",
        subtitle=f"approve?  ({_summary(tool_input)})",
        border_style="yellow" if tier == 2 else "red",
    )


async def prompt_terminal(tool_input: dict, tier: int, principal: Principal) -> bool:
    """Render the call, ask y/N at the controlling tty. Default deny on EOF."""
    panel = _render(tool_input, tier, principal)

    def ask() -> bool:
        _console.print(panel)
        try:
            return Confirm.ask(
                f"[yellow]Approve L{tier} call?[/yellow]",
                default=False,
                console=_console,
            )
        except (EOFError, KeyboardInterrupt):
            _console.print("[red]\n(no tty / interrupted) → DENY[/red]")
            return False

    # Confirm.ask is sync; offload to a thread so we don't block the event loop.
    return await asyncio.to_thread(ask)


def _pushover_summary(tool_input: dict) -> str:
    """One-line human description for the Pushover body."""
    name = tool_input.get("tool_name", "?")
    args = tool_input.get("tool_input", {})
    bits = []
    if isinstance(args, dict):
        for k, v in list(args.items())[:4]:
            s = str(v)
            if len(s) > 80:
                s = s[:77] + "…"
            bits.append(f"{k}={s}")
    return f"{name}\n" + "\n".join(bits) if bits else name


async def prompt_telegram(
    tool_input: dict, tier: int, principal: Principal
) -> bool:
    """Default L3 approval path. Asks the principal via their linked Telegram
    chat with inline Approve/Deny buttons. Default deny on:
      - principal has no linked Telegram account
      - bot token unset
      - 60s timeout with no button press
      - send error
    """
    has_tg = approvals.telegram_user_id_for(principal.user_id) is not None
    if not has_tg:
        _console.print(
            f"[red](telegram) {principal.user_id!r} has no linked Telegram chat. "
            f"Denying L{tier} {tool_input.get('tool_name')!r}.\n"
            f"  Run: agent user link-telegram --user {principal.user_id}[/red]"
        )
        return False

    summary = _pushover_summary(tool_input)
    _console.print(
        f"[yellow](telegram) prompting {principal.name} for "
        f"L{tier} {tool_input.get('tool_name')!r} — waiting up to 60s...[/yellow]"
    )
    ok = await approvals.request_telegram(
        principal=principal, tier=tier, tool_input=tool_input,
        summary=summary, wait_seconds=60,
    )
    _console.print(
        f"[{'green' if ok else 'red'}](telegram) "
        f"{'APPROVED' if ok else 'DENIED (timeout/no-ack/error)'}[/]"
    )
    return ok


async def prompt_pushover(
    tool_input: dict, tier: int, principal: Principal
) -> bool:
    """Legacy Pushover path, kept for users who configured it before Telegram
    became the primary push surface. Same return contract as prompt_telegram."""
    if not pushover.is_configured(principal.user_id):
        _console.print(
            f"[red](pushover) not configured for {principal.user_id}; denying L{tier}[/red]"
        )
        return False
    body = _pushover_summary(tool_input)
    ok = await pushover.send_emergency_and_wait(
        user_id=principal.user_id, title=f"Agent — approve L{tier}?",
        message=body, wait_seconds=60, retry=30,
    )
    return ok


# Back-compat alias so older wiring (loop.py) keeps working — points to the
# new Telegram path now that we've consolidated.
prompt_pushover_stub = prompt_telegram

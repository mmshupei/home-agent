"""Approval prompts: terminal (rich) and Pushover (M2 stub).

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


async def prompt_pushover_stub(
    tool_input: dict, tier: int, principal: Principal
) -> bool:
    """M2 stub: log to console and approve. M5 replaces with real Pushover.

    Returning True here would silently allow remote-profile calls; that's
    explicitly noted in the design (M2 just exercises the wiring). M5 swaps
    in the real implementation with a 60s wait + default deny.
    """
    _console.print(
        f"[dim](pushover stub) would prompt {principal.name} for "
        f"L{tier} {tool_input.get('tool_name')!r} → auto-approving for M2[/dim]"
    )
    return True

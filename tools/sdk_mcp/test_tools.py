"""Test tools for exercising the gate at each tier.

These exist so M2 has something to gate against without needing real
side-effects. M4 replaces them with the real applescript / HA / playwright
tools.

Tool naming convention (matches config/tiers.toml regexes):
  - read-style       -> L1
  - filesystem__write / memory__write -> L2
  - applescript__messages__send       -> L3
"""
from __future__ import annotations

from claude_agent_sdk import tool


@tool(
    "filesystem__read",
    "Echo the provided text. Stand-in for a read-only L1 tool.",
    {"text": str},
)
async def fs_read(args):
    return {"content": [{"type": "text", "text": f"read: {args['text']}"}]}


@tool(
    "filesystem__write",
    "Echo the provided text. Stand-in for an L2 reversible-write tool.",
    {"text": str},
)
async def fs_write(args):
    return {"content": [{"type": "text", "text": f"wrote: {args['text']}"}]}


@tool(
    "applescript__messages__send",
    "Pretend to send an iMessage. Stand-in for an L3 irreversible tool.",
    {"to": str, "body": str},
)
async def messages_send(args):
    return {
        "content": [
            {
                "type": "text",
                "text": f"(simulated) sent to {args['to']}: {args['body']}",
            }
        ]
    }


def test_tools():
    return [fs_read, fs_write, messages_send]

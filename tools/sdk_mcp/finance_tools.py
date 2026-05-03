"""SDK MCP wrappers around tools/finance/lots.py.

L1 — read-only by design. No order placement, no transfers, no sells.
"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from tools.finance import lots as ledger


@tool(
    "finance__get_lot_summary",
    "Return aggregate stats over the household's AAPL lot ledger: count, total "
    "shares, average cost basis, long/short-term breakdown.",
    {},
)
async def get_lot_summary(args):
    s = ledger.lot_summary()
    return {"content": [{"type": "text", "text": json.dumps(s, indent=2, default=str)}]}


@tool(
    "finance__list_lots",
    "List individual AAPL tax lots: id, acquired date, shares, cost basis, type "
    "(purchase / rsu_vest / espp).",
    {},
)
async def list_lots(args):
    rows = ledger.lots()
    if not rows:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "(no lots in ledger; populate data/finance.json)",
                }
            ]
        }
    lines = [
        f"- {l.id}  {l.acquired}  {l.shares:>6} sh @ ${l.cost_basis:.2f}  ({l.type})"
        for l in rows
    ]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "finance__list_vests",
    "List upcoming RSU vest events within `days` (default 365).",
    {"days": int},
)
async def list_vests(args):
    rows = ledger.upcoming_vests(int(args.get("days") or 365))
    if not rows:
        return {"content": [{"type": "text", "text": "(no upcoming vests in window)"}]}
    return {"content": [{"type": "text", "text": json.dumps(rows, indent=2)}]}


def finance_tools():
    return [get_lot_summary, list_lots, list_vests]

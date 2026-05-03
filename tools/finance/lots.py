"""AAPL lot ledger + RSU vest calendar — read-only.

Source of truth is a JSON file at data/finance.json. The agent never modifies
it from inside the loop; edits happen out-of-band (manual). This file just
parses + computes derived values (current basis, total cost, days-to-vest).

Schema:
{
  "ticker": "AAPL",
  "lots": [
    {"id": "L001", "acquired": "2022-09-15", "shares": 100, "cost_basis": 154.32, "type": "purchase"},
    {"id": "L002", "acquired": "2023-04-01", "shares": 50,  "cost_basis": 165.10, "type": "rsu_vest"},
    {"id": "L003", "acquired": "2024-08-15", "shares": 25,  "cost_basis": 0.00,  "type": "espp", "discount": 0.15}
  ],
  "vests": [
    {"grant_id": "G2023-01", "vest_date": "2026-04-01", "shares": 50, "fmv_at_grant": 165.10, "notes": "Q1 2026 vest"},
    {"grant_id": "G2023-01", "vest_date": "2026-07-01", "shares": 50, "fmv_at_grant": 165.10}
  ]
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "finance.json"


@dataclass
class Lot:
    id: str
    acquired: date
    shares: float
    cost_basis: float
    type: str
    discount: float = 0.0


@dataclass
class Vest:
    grant_id: str
    vest_date: date
    shares: float
    fmv_at_grant: float
    notes: str = ""


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load() -> dict:
    if not LEDGER_PATH.exists():
        return {"ticker": "AAPL", "lots": [], "vests": []}
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def lots() -> list[Lot]:
    raw = load()
    return [
        Lot(
            id=l["id"],
            acquired=_parse_date(l["acquired"]),
            shares=float(l["shares"]),
            cost_basis=float(l["cost_basis"]),
            type=l.get("type", "purchase"),
            discount=float(l.get("discount", 0.0)),
        )
        for l in raw.get("lots", [])
    ]


def vests() -> list[Vest]:
    raw = load()
    return [
        Vest(
            grant_id=v["grant_id"],
            vest_date=_parse_date(v["vest_date"]),
            shares=float(v["shares"]),
            fmv_at_grant=float(v["fmv_at_grant"]),
            notes=v.get("notes", ""),
        )
        for v in raw.get("vests", [])
    ]


def lot_summary() -> dict:
    """Aggregate stats across all lots."""
    ll = lots()
    total_shares = sum(l.shares for l in ll)
    total_cost = sum(l.shares * l.cost_basis for l in ll)
    avg_basis = (total_cost / total_shares) if total_shares else 0.0
    long_term = [l for l in ll if (date.today() - l.acquired).days >= 365]
    short_term = [l for l in ll if (date.today() - l.acquired).days < 365]
    return {
        "lot_count": len(ll),
        "total_shares": total_shares,
        "total_cost_basis": total_cost,
        "avg_cost_basis": avg_basis,
        "long_term_lots": len(long_term),
        "short_term_lots": len(short_term),
        "long_term_shares": sum(l.shares for l in long_term),
        "short_term_shares": sum(l.shares for l in short_term),
    }


def upcoming_vests(days: int = 365) -> list[dict]:
    today = date.today()
    out = []
    for v in sorted(vests(), key=lambda x: x.vest_date):
        delta = (v.vest_date - today).days
        if 0 <= delta <= days:
            out.append({
                "grant_id": v.grant_id,
                "vest_date": v.vest_date.isoformat(),
                "shares": v.shares,
                "fmv_at_grant": v.fmv_at_grant,
                "days_until": delta,
                "notes": v.notes,
            })
    return out

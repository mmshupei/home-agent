"""Proposal artifact emission + persistence + apply.

Each accepted proposal:
1. Lands as files in the sandbox's artifacts/proposals/ dir (JSON metadata
   plus optional per-kind sidecar files)
2. Gets a row in the `proposals` table marked state='pending'
3. Is later approved/rejected by a human via `agent dream approve|reject`

For M11.1 only memory_correction is supported, so there's no .patch sidecar
yet — the JSON metadata fully describes the action.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from orchestrator.db import connect
from .constitution import validate_proposal, ValidationResult, log_rejection


@dataclass
class ProposalDraft:
    kind: str
    title: str
    rationale: str
    payload: dict
    evidence: list[dict]   # opaque list, surfaced in CLI for spot-checking


def emit_proposal(
    *,
    cycle_id: str,
    artifacts_dir: Path,
    seq: int,
    draft: ProposalDraft,
) -> tuple[bool, str]:
    """Validate + persist a proposal. Returns (accepted, message).

    On constitution rejection, logs to constitution_rejections (and a
    rejections/<seq>.json sidecar in the sandbox) but does NOT raise."""
    pid = f"{cycle_id}/{seq:03d}"
    result = validate_proposal(kind=draft.kind, payload=draft.payload)

    if not result.ok:
        log_rejection(
            cycle_id=cycle_id,
            declared_kind=draft.kind,
            reason=result.reason,
            layer="post_patch",
        )
        rej_path = artifacts_dir / "rejections" / f"{seq:03d}.json"
        rej_path.write_text(json.dumps({
            "id": pid,
            "kind": draft.kind,
            "title": draft.title,
            "rationale": draft.rationale,
            "payload": draft.payload,
            "rejected_reason": result.reason,
        }, indent=2, default=str), encoding="utf-8")
        return False, result.reason

    # Accepted — write the proposal JSON sidecar and DB row
    metadata = {
        "id": pid,
        "cycle_id": cycle_id,
        "kind": draft.kind,
        "title": draft.title,
        "rationale": draft.rationale,
        "payload": draft.payload,
        "evidence": draft.evidence,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "approval_state": "pending",
    }
    prop_path = artifacts_dir / "proposals" / f"{seq:03d}.json"
    prop_path.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    with connect() as c:
        c.execute(
            """INSERT INTO proposals
               (id, cycle_id, kind, title, rationale, artifact_dir,
                constraints_passed, tests_passed, state, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'pending', ?)""",
            (
                pid, cycle_id, draft.kind, draft.title, draft.rationale,
                str(artifacts_dir), json.dumps(draft.payload, default=str),
            ),
        )
    return True, pid


# ---------------------------------------------------------------------------
# Application (memory_correction only for M11.1)
# ---------------------------------------------------------------------------


def list_pending() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT id, cycle_id, kind, title, rationale, payload_json,
                      artifact_dir, state, decided_at, decision_reason
               FROM proposals
               WHERE state = 'pending'
               ORDER BY id""",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        out.append(d)
    return out


def get(proposal_id: str) -> Optional[dict]:
    with connect() as c:
        r = c.execute(
            "SELECT id, cycle_id, kind, title, rationale, payload_json, "
            "artifact_dir, state, decided_at, decided_by, decision_reason "
            "FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except json.JSONDecodeError:
        d["payload"] = {}
    return d


def reject(proposal_id: str, *, decided_by: str, reason: str) -> bool:
    with connect() as c:
        cur = c.execute(
            "UPDATE proposals SET state='rejected', decided_at=CURRENT_TIMESTAMP, "
            "decided_by=?, decision_reason=? WHERE id=? AND state='pending'",
            (decided_by, reason, proposal_id),
        )
    return cur.rowcount > 0


def approve(proposal_id: str, *, decided_by: str) -> tuple[bool, str]:
    """Apply a pending proposal. M11.1 supports memory_correction only.
    Returns (ok, message)."""
    p = get(proposal_id)
    if not p:
        return False, f"no such proposal: {proposal_id}"
    if p["state"] != "pending":
        return False, f"proposal is {p['state']}, not pending"

    if p["kind"] != "memory_correction":
        return False, f"M11.1 only supports memory_correction; got {p['kind']!r}"

    ok, msg = _apply_memory_correction(p["payload"])
    if not ok:
        return False, msg

    with connect() as c:
        c.execute(
            "UPDATE proposals SET state='approved', decided_at=CURRENT_TIMESTAMP, "
            "decided_by=? WHERE id=?",
            (decided_by, proposal_id),
        )
    return True, msg


def _apply_memory_correction(payload: dict) -> tuple[bool, str]:
    from orchestrator import memory as mem

    action = payload["action"]
    target_id = int(payload["target_memory_id"])
    target = mem.read_by_id(target_id)
    if not target:
        return False, f"target memory id={target_id} no longer exists"

    if action == "archive":
        ok = mem.forget(target_id)
        return ok, f"archived id={target_id} (was: {target.content!r})"

    if action == "lower_confidence":
        nc = float(payload["new_confidence"])
        with connect() as c:
            c.execute(
                "UPDATE memory SET confidence = ? WHERE id = ?", (nc, target_id)
            )
        return True, f"lowered confidence id={target_id} -> {nc:.2f}"

    if action == "mark_superseded":
        sup_id = int(payload["superseded_by_memory_id"])
        sup = mem.read_by_id(sup_id)
        if not sup:
            return False, f"superseded_by_memory_id={sup_id} not found"
        # M11.1: archive the older one. M11.2 may add a real `superseded_by`
        # link column once the schema gets versioning.
        ok = mem.forget(target_id)
        return ok, (
            f"archived id={target_id} (superseded by id={sup_id}: {sup.content!r})"
        )

    return False, f"unknown action: {action!r}"

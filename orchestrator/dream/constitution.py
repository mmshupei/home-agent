"""The constitution — safety layer for self-modification.

M11.1 enforces only the memory-related kinds (memory_correction, schema_promotion).
M11.2 will add the path-based and patch-based rules from §5 of the design doc.

The constitution is intentionally simple and additive. Adding a kind
requires editing this file by hand; the constitution is meta-protected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# M11.1 kinds — DB-only operations, no code patches yet.
ALLOWED_KINDS_M11_1 = {
    "memory_correction",   # archive a memory, mark superseded, lower confidence
    "schema_promotion",    # not used until M9 lands; reserved
}

# Hard cap per cycle to prevent proposal storms (§9 of design).
MAX_PROPOSALS_PER_CYCLE = 5

# Memory IDs whose scope is in this set can be the target of a memory_correction.
# Excludes anything in user:* scopes so cross-user moderation isn't possible
# from a dream cycle. Family/system/own-user only.
ALLOWED_TARGET_SCOPES_M11_1 = ("system", "family")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""


def Accept() -> ValidationResult:
    return ValidationResult(ok=True)


def Reject(reason: str) -> ValidationResult:
    return ValidationResult(ok=False, reason=reason)


def validate_proposal(*, kind: str, payload: dict) -> ValidationResult:
    """Per-proposal validation. M11.1 only handles memory_correction.

    payload schema for memory_correction:
        {
          "action": "archive" | "lower_confidence" | "mark_superseded",
          "target_memory_id": int,
          "target_scope": "system" | "family",
          "evidence_memory_ids": [int, ...],   # supporting memories
          "rationale_summary": str,
          # action-specific:
          "new_confidence": float,             # for lower_confidence
          "superseded_by_memory_id": int,      # for mark_superseded
        }
    """
    if kind not in ALLOWED_KINDS_M11_1:
        return Reject(f"kind {kind!r} not allowed in M11.1 (M11.2 unlocks code kinds)")

    if kind == "memory_correction":
        return _validate_memory_correction(payload)

    return Reject(f"no validator wired for kind {kind!r}")


def _validate_memory_correction(p: dict) -> ValidationResult:
    action = p.get("action")
    if action not in ("archive", "lower_confidence", "mark_superseded"):
        return Reject(f"unknown memory_correction action: {action!r}")

    if not isinstance(p.get("target_memory_id"), int):
        return Reject("target_memory_id must be an int")

    target_scope = p.get("target_scope", "")
    if target_scope not in ALLOWED_TARGET_SCOPES_M11_1:
        return Reject(
            f"target_scope {target_scope!r} not allowed for dream-cycle correction "
            f"(only {ALLOWED_TARGET_SCOPES_M11_1!r})"
        )

    evidence = p.get("evidence_memory_ids") or []
    if not isinstance(evidence, list) or len(evidence) < 1:
        return Reject("memory_correction requires at least one evidence_memory_id")

    if not isinstance(p.get("rationale_summary"), str) or len(p["rationale_summary"]) < 20:
        return Reject("rationale_summary must be >= 20 chars")

    if action == "lower_confidence":
        nc = p.get("new_confidence")
        if not isinstance(nc, (int, float)) or not (0.0 < nc < 1.0):
            return Reject("lower_confidence requires new_confidence in (0, 1)")

    if action == "mark_superseded":
        if not isinstance(p.get("superseded_by_memory_id"), int):
            return Reject("mark_superseded requires superseded_by_memory_id (int)")

    return Accept()


def validate_cycle_caps(num_proposals: int) -> ValidationResult:
    if num_proposals > MAX_PROPOSALS_PER_CYCLE:
        return Reject(
            f"cycle emitted {num_proposals} proposals; cap is {MAX_PROPOSALS_PER_CYCLE}"
        )
    return Accept()


def log_rejection(
    *,
    cycle_id: str | None,
    declared_kind: str | None,
    reason: str,
    layer: str,
    patch_summary: str | None = None,
) -> None:
    """Persist a constitution rejection so monthly audits can spot calibration
    issues (§8 of design)."""
    from orchestrator.db import connect

    with connect() as c:
        c.execute(
            """INSERT INTO constitution_rejections
               (cycle_id, declared_kind, reason, patch_summary, layer)
               VALUES (?, ?, ?, ?, ?)""",
            (cycle_id, declared_kind, reason, patch_summary, layer),
        )

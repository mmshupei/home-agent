"""M11.1 dream cycle entrypoint.

Orchestrates: spawn sandbox → build inventory from snapshot → run dream agent →
validate proposals through constitution → emit accepted ones to dream-queue/
and the proposals table → write reflection.md + manifest.json → cleanup.

Sandbox isolation in M11.1 is filesystem + DB-snapshot only. M11.2 will wrap
the same shape in a container with network restrictions per the design doc.
For M11.1 the dream agent emits memory_correction proposals only — DB-only,
no code patches — so process-level isolation is sufficient.

Production never reads from the sandbox after this function returns. The
dream-queue/ artifact dir is the only thing that crosses the boundary.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .agent import run_dream_pass, _format_memory_inventory, _format_similarity_pairs, _format_stale_facts
from .constitution import (
    MAX_PROPOSALS_PER_CYCLE,
    log_rejection,
    validate_cycle_caps,
)
from .inventory import (
    fetch_inventory,
    fetch_similarity_pairs,
    fetch_stale_candidates,
)
from .proposals import emit_proposal
from .sandbox import emit_to_queue, spawn_filesystem_sandbox
from orchestrator.db import connect


@contextmanager
def _swap_db_path(snapshot_path: Path) -> Iterator[None]:
    """Temporarily point AGENT_DB_PATH at the sandbox snapshot so the
    in-process inventory queries (and embedding loads) read from the
    snapshot. Restores on exit."""
    prev = os.environ.get("AGENT_DB_PATH")
    os.environ["AGENT_DB_PATH"] = str(snapshot_path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AGENT_DB_PATH", None)
        else:
            os.environ["AGENT_DB_PATH"] = prev


def _new_cycle_id() -> str:
    return "dream-" + datetime.now().strftime("%Y%m%d-%H%M")


def _record_cycle_running(cycle_id: str, sandbox_kind: str) -> None:
    with connect() as c:
        c.execute(
            """INSERT INTO dream_cycles
               (id, started_at, sandbox_kind, status)
               VALUES (?, CURRENT_TIMESTAMP, ?, 'running')""",
            (cycle_id, sandbox_kind),
        )


def _finalize_cycle(
    cycle_id: str,
    *,
    proposals_emitted: int,
    rejections_logged: int,
    cost_usd: float | None,
    reflection_path: str,
    status: str,
) -> None:
    with connect() as c:
        c.execute(
            """UPDATE dream_cycles
               SET ended_at = CURRENT_TIMESTAMP,
                   proposals_emitted = ?, rejections_logged = ?,
                   cost_usd = ?, reflection_path = ?, status = ?
               WHERE id = ?""",
            (proposals_emitted, rejections_logged, cost_usd, reflection_path, status, cycle_id),
        )


async def run() -> dict:
    """Run one dream cycle. Returns a result dict the CLI prints."""
    cycle_id = _new_cycle_id()
    started = time.time()

    sandbox = spawn_filesystem_sandbox(cycle_id)
    _record_cycle_running(cycle_id, sandbox.kind)

    try:
        # Step 1: gather inputs FROM THE SNAPSHOT (so production isn't read
        # while the dream agent is reasoning).
        with _swap_db_path(sandbox.db):
            inv_rows = fetch_inventory()
            sim_pairs = fetch_similarity_pairs()
            stale = fetch_stale_candidates()

        inventory_md = _format_memory_inventory(inv_rows, events=[])
        sim_md = _format_similarity_pairs(sim_pairs)
        stale_md = _format_stale_facts(stale)

        # Step 2: drive the dream agent.
        out = await run_dream_pass(
            inventory=inventory_md,
            similarity_pairs=sim_md,
            stale=stale_md,
        )

        # Step 3: write transcripts + reflection to artifacts dir
        (sandbox.artifacts / "transcripts" / "dream-agent.txt").write_text(
            out.raw_text, encoding="utf-8"
        )
        reflection_path = sandbox.artifacts / "reflection.md"
        reflection_path.write_text(
            f"# Dream cycle {cycle_id}\n\n"
            f"Ran at: {datetime.utcnow().isoformat()}Z\n"
            f"Inventory size: {len(inv_rows)} memories\n"
            f"Similarity pairs surfaced: {len(sim_pairs)}\n"
            f"Stale candidates: {len(stale)}\n\n"
            f"## Reflection\n\n{out.reflection or '(no reflection)'}\n",
            encoding="utf-8",
        )

        # Step 4: validate + emit proposals (writes both sidecar JSON and
        # the proposals table row in the production DB).
        accepted = 0
        rejected = 0
        for seq, draft in enumerate(out.proposals[: MAX_PROPOSALS_PER_CYCLE * 2], start=1):
            ok, _msg = emit_proposal(
                cycle_id=cycle_id,
                artifacts_dir=sandbox.artifacts,
                seq=seq,
                draft=draft,
            )
            if ok:
                accepted += 1
            else:
                rejected += 1

        # Cycle-level constitution check
        cap_check = validate_cycle_caps(accepted)
        if not cap_check.ok:
            log_rejection(
                cycle_id=cycle_id, declared_kind=None,
                reason=cap_check.reason, layer="post_cycle",
            )

        # Step 5: write manifest.json (top-level cycle metadata)
        manifest = {
            "cycle_id": cycle_id,
            "ran_at": datetime.utcnow().isoformat() + "Z",
            "sandbox_kind": sandbox.kind,
            "inventory_size": len(inv_rows),
            "similarity_pairs": len(sim_pairs),
            "stale_candidates": len(stale),
            "proposals_emitted": accepted,
            "proposals_rejected": rejected,
            "reflection": out.reflection,
        }
        (sandbox.artifacts / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )

        # Step 6: emit artifacts to the production review queue
        queue_path = emit_to_queue(sandbox)

        # Rewrite proposals.artifact_dir to the persistent queue path (the
        # sandbox path is about to be cleaned up).
        with connect() as c:
            c.execute(
                "UPDATE proposals SET artifact_dir = ? WHERE cycle_id = ?",
                (str(queue_path), cycle_id),
            )

        _finalize_cycle(
            cycle_id,
            proposals_emitted=accepted,
            rejections_logged=rejected,
            cost_usd=None,  # M11.2 will plumb this through from the SDK ResultMessage
            reflection_path=str(queue_path / "reflection.md"),
            status="completed",
        )

        return {
            "cycle_id": cycle_id,
            "duration_s": int(time.time() - started),
            "inventory_size": len(inv_rows),
            "similarity_pairs": len(sim_pairs),
            "stale_candidates": len(stale),
            "proposals_emitted": accepted,
            "proposals_rejected": rejected,
            "queue_path": str(queue_path),
            "reflection": out.reflection,
        }
    except Exception as e:
        _finalize_cycle(
            cycle_id,
            proposals_emitted=0, rejections_logged=0,
            cost_usd=None, reflection_path="",
            status="failed",
        )
        raise
    finally:
        sandbox.cleanup()

"""M9 night cycle.

Runs nightly at 3am. Sonnet with extended thinking. Reads unconsolidated
episodes + recent memory + pending questions + last cycle's notes; writes
schema/observation/question_pending memory rows; records every change as
a memory_revision so revert-cycle is one SQL statement.

Trust rules (§5 of the M9 doc):
- Episodes are immutable.
- The cycle never silently overwrites a fact. Contradictions become
  question_pending memories surfaced the next morning.
- Every confidence/scope/content change is logged with revised_by='night_cycle'
  and the cycle's run timestamp, so revert-cycle YYYY-MM-DD undoes a bad pass.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from . import episodes, memory as mem
from .auth import Principal
from .db import connect

NIGHT_MODEL = os.environ.get("AGENT_NIGHT_MODEL", "claude-sonnet-4-6")
THINKING_BUDGET = int(os.environ.get("AGENT_NIGHT_THINKING_TOKENS", "16000"))

# The internal "principal" the night cycle writes under.
NIGHT_PRINCIPAL = Principal(
    user_id="night_cycle", name="Night Cycle", role="admin", token_label="night"
)

# Caps from §4 of the design.
MAX_QUESTIONS_QUEUED_PER_CYCLE = 3
MAX_SCHEMA_PROPOSALS = 5
MAX_OBSERVATION_PROPOSALS = 10


NIGHT_PROMPT = """You are running the household agent's nightly consolidation cycle.
The household is asleep. Take your time. Be thoughtful, conservative, and explicit
about what you're proposing versus deciding.

You have full read access to memory and episodes. You can WRITE three kinds of
memory only — `schema`, `observation`, `question_pending` — and you can adjust
confidence on existing rows. You CANNOT delete, overwrite, or change scope of
any user-explicit fact (confidence ≥ 0.99); when something looks wrong with
one, the right move is to queue a `question_pending` for the morning.

Inputs follow the prompt. Walk through four phases in order. Output a single
JSON block at the end (and nothing else outside it):

```json
{
  "reflection": "<3-5 sentence prose summary of what you noticed and proposed>",
  "replay_notes": "<short prose>",
  "commitments": [
    {"summary": "...", "due_date": "YYYY-MM-DD or null", "from_episode_id": <int>}
  ],
  "schema_proposals": [
    {
      "scope": "family|system",
      "content": "<the schema, e.g. 'Tuesday/Thursday are Yunyan's pickup days, ~4pm'>",
      "confidence": 0.6,
      "evidence": [{"episode_id": <int>, "matched_pattern": "..."},
                    {"memory_id": <int>, "matched_pattern": "..."}]
    }
  ],
  "observation_proposals": [
    {"scope": "family|system", "content": "...", "confidence": 0.4,
     "evidence": [...]}
  ],
  "questions_to_queue": [
    {"scope": "family|system",
     "content": "<phrased as a single question for the morning>",
     "rationale": "<why ask>",
     "evidence_memory_ids": [<int>, ...]}
  ],
  "decay_actions": [
    {"memory_id": <int>, "old_confidence": 0.8, "new_confidence": 0.6,
     "reason": "untouched > 30d, kind != fact"}
  ],
  "archive_actions": [
    {"memory_id": <int>, "reason": "confidence < 0.3 and last_seen > 60d ago"}
  ]
}
```

PHASE 1 — REPLAY
For each episode (≤20), note successes/failures, commitments made, surprises.

PHASE 2 — ABSTRACT
Find recurring patterns. ≥3 supporting items → schema_proposal.
≥2 occurrences not yet patterned → observation_proposal.
Prefer observation over schema when uncertain.

PHASE 3 — RECONCILE
Find contradictions, redundancies, decay candidates.
- Contradiction → questions_to_queue (NEVER silently resolve).
- Redundancy → archive_actions targeting the LESS specific copy.
- Decay → decay_actions for kind != 'fact', untouched > 30d (multiply by 0.8).
- Archive → archive_actions for confidence < 0.3 AND last_seen > 60d.

PHASE 4 — REORGANIZE
Tie schema proposals to their evidence. Surface unexpected proximities
(without acting on them — they go in the reflection).

Hard caps for this cycle:
- ≤5 schema proposals, ≤10 observation proposals, ≤3 questions queued.
- If you have more candidates, prioritize and explain in reflection.

Important: only TWO scopes are valid for your writes — `family` and `system`.
Never propose against user:* scopes. Never propose changes to anything in
user:* scope; that is the user's private memory."""


@dataclass
class CycleResult:
    cycle_id: int
    duration_s: int
    episodes_replayed: int
    schemas_added: int
    observations_added: int
    questions_queued: int
    archived: int
    decayed: int
    cost_usd: float | None
    notes: str
    raw_output: str
    status: str = "completed"
    revisions: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _format_episodes(rows: list[dict]) -> str:
    if not rows:
        return "## Unconsolidated episodes\n_(none)_"
    lines = ["## Unconsolidated episodes"]
    for r in rows[:20]:
        affect = r.get("affect")
        try:
            a = json.loads(affect) if affect else {}
        except json.JSONDecodeError:
            a = {}
        lines.append(
            f"- id={r['id']} src={r['source']:<8} principal={r['principal']!r} "
            f"started={r['started_at']} cost=${a.get('cost_usd', 0):.3f}\n"
            f"  summary: {(r.get('summary') or '')[:200]}\n"
            f"  transcript: {(r.get('transcript') or '')[:400]}"
        )
    return "\n".join(lines)


def _format_recent_memory(days: int = 30) -> str:
    with connect() as c:
        rows = c.execute(
            """SELECT id, scope, kind, content, confidence, created_at, last_seen
               FROM memory
               WHERE scope IN ('family','system')
                 AND confidence > 0.0
                 AND created_at > datetime('now', ?)
               ORDER BY created_at DESC LIMIT 80""",
            (f"-{days} days",),
        ).fetchall()
    if not rows:
        return "## Recent family/system memory\n_(empty)_"
    lines = ["## Recent family/system memory (last 30 days)"]
    for r in rows:
        lines.append(
            f"- id={r['id']} scope={r['scope']} kind={r['kind']} "
            f"conf={r['confidence']:.2f} last_seen={r['last_seen']}: "
            f"{r['content']!r}"
        )
    return "\n".join(lines)


def _format_pending_questions() -> str:
    with connect() as c:
        rows = c.execute(
            "SELECT id, scope, content, created_at, confidence "
            "FROM memory WHERE kind = 'question_pending' AND confidence > 0 "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "## Pending questions (already queued, awaiting answer)\n_(none)_"
    return "## Pending questions (already queued, awaiting answer)\n" + "\n".join(
        f"- id={r['id']} {r['scope']} created={r['created_at']}: {r['content']!r}"
        for r in rows
    )


def _last_cycle_notes() -> str:
    with connect() as c:
        r = c.execute(
            "SELECT ran_at, notes FROM night_cycles ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
    if not r:
        return "## Last cycle\n_(no prior cycle)_"
    return f"## Last cycle ({r['ran_at']}) notes\n{r['notes'] or '(empty)'}"


# ---------------------------------------------------------------------------
# Output application
# ---------------------------------------------------------------------------


def _apply_outputs(
    parsed: dict, cycle_run_at: datetime, episode_ids: list[int]
) -> tuple[CycleResult, list[int]]:
    schemas_added = 0
    observations_added = 0
    questions_queued = 0
    archived = 0
    decayed = 0
    revision_ids: list[int] = []

    def _allowed_scope(s: str) -> bool:
        return s in ("family", "system")

    # Schemas
    for sp in (parsed.get("schema_proposals") or [])[:MAX_SCHEMA_PROPOSALS]:
        scope = sp.get("scope", "family")
        if not _allowed_scope(scope):
            continue
        sid = mem.write(
            content=sp["content"],
            scope=scope,
            kind="schema",
            created_by=NIGHT_PRINCIPAL.user_id,
            confidence=float(sp.get("confidence", 0.6)),
        )
        schemas_added += 1
        # Link evidence (best-effort; ignore unknown ids)
        with connect() as c:
            for ev in (sp.get("evidence") or [])[:20]:
                c.execute(
                    """INSERT OR IGNORE INTO schema_evidence
                       (schema_id, episode_id, memory_id, weight)
                       VALUES (?, ?, ?, 1.0)""",
                    (sid, ev.get("episode_id"), ev.get("memory_id")),
                )

    # Observations
    for op in (parsed.get("observation_proposals") or [])[:MAX_OBSERVATION_PROPOSALS]:
        scope = op.get("scope", "family")
        if not _allowed_scope(scope):
            continue
        mem.write(
            content=op["content"],
            scope=scope,
            kind="observation",
            created_by=NIGHT_PRINCIPAL.user_id,
            confidence=float(op.get("confidence", 0.4)),
        )
        observations_added += 1

    # Questions queued
    for q in (parsed.get("questions_to_queue") or [])[:MAX_QUESTIONS_QUEUED_PER_CYCLE]:
        scope = q.get("scope", "family")
        if not _allowed_scope(scope):
            continue
        mem.write(
            content=q["content"],
            scope=scope,
            kind="question_pending",
            created_by=NIGHT_PRINCIPAL.user_id,
            confidence=1.0,  # questions are themselves certain — what's uncertain is what they ask about
        )
        questions_queued += 1

    # Decay
    for d in parsed.get("decay_actions") or []:
        mid = d.get("memory_id")
        nc = d.get("new_confidence")
        if not isinstance(mid, int) or not isinstance(nc, (int, float)):
            continue
        # Trust rule: never decay user-explicit (conf >= 0.99) facts silently.
        existing = mem.read_by_id(mid)
        if not existing or existing.confidence >= 0.99 and existing.kind == "fact":
            continue
        mem.update_confidence(
            mid, float(nc),
            revised_by="night_cycle",
            reason=d.get("reason") or "decay",
            cycle_run_at=cycle_run_at,
        )
        decayed += 1

    # Archive
    for a in parsed.get("archive_actions") or []:
        mid = a.get("memory_id")
        if not isinstance(mid, int):
            continue
        existing = mem.read_by_id(mid)
        if not existing:
            continue
        # Trust rule: don't auto-archive user-explicit facts.
        if existing.confidence >= 0.99 and existing.kind == "fact":
            continue
        mem.forget(
            mid,
            revised_by="night_cycle",
            reason=a.get("reason") or "archive",
            cycle_run_at=cycle_run_at,
        )
        archived += 1

    # Mark episodes consolidated
    notes = (parsed.get("reflection") or "")[:4000]
    episodes.mark_consolidated(episode_ids, notes=notes[:200])

    return (
        CycleResult(
            cycle_id=0,  # filled in by run()
            duration_s=0,
            episodes_replayed=len(episode_ids),
            schemas_added=schemas_added,
            observations_added=observations_added,
            questions_queued=questions_queued,
            archived=archived,
            decayed=decayed,
            cost_usd=None,
            notes=notes,
            raw_output="",
        ),
        revision_ids,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _parse(text: str) -> dict | None:
    s = text.strip()
    if "```" in s:
        try:
            after = s.split("```", 1)[1]
            if after.lower().startswith("json"):
                after = after[4:]
            s = after.split("```", 1)[0].strip()
        except IndexError:
            pass
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None


async def run() -> CycleResult:
    """Run one night cycle. Persists results to night_cycles. Idempotent in the
    sense that re-running won't double-process episodes (they're marked
    consolidated_at on first pass)."""
    started = time.time()
    cycle_started = datetime.utcnow()

    eps = episodes.fetch_unconsolidated(limit=50)
    episode_ids = [e["id"] for e in eps]
    payload_parts = [
        _format_episodes(eps),
        _format_recent_memory(),
        _format_pending_questions(),
        _last_cycle_notes(),
    ]
    user_payload = "\n\n".join(payload_parts)

    use_sub = os.environ.get("AGENT_USE_SUBSCRIPTION", "1") != "0"
    sdk_env = {"ANTHROPIC_API_KEY": ""} if use_sub else {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")
    }

    options = ClaudeAgentOptions(
        model=NIGHT_MODEL,
        system_prompt=NIGHT_PROMPT,
        permission_mode="default",
        env=sdk_env,
        thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
    )

    text = ""
    cost: float | None = None
    status = "completed"
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_payload)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text += b.text
                elif isinstance(msg, ResultMessage):
                    cost = getattr(msg, "total_cost_usd", None)
                    if getattr(msg, "result", None):
                        text = msg.result
    except Exception as e:
        status = "failed"
        text = json.dumps({
            "reflection": f"(night cycle failed: {type(e).__name__}: {e})",
            "schema_proposals": [], "observation_proposals": [],
            "questions_to_queue": [], "decay_actions": [], "archive_actions": [],
        })

    parsed = _parse(text) or {}
    result, _revs = _apply_outputs(parsed, cycle_started, episode_ids)
    duration_s = int(time.time() - started)
    result.duration_s = duration_s
    result.cost_usd = cost
    result.raw_output = text
    result.status = status

    with connect() as c:
        cur = c.execute(
            """INSERT INTO night_cycles
               (ran_at, duration_s, episodes_replayed,
                commitments_extracted, schemas_proposed, observations_added,
                contradictions_found, questions_queued, archived_count,
                cost_usd, notes, raw_output, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cycle_started, duration_s, result.episodes_replayed,
                len(parsed.get("commitments") or []),
                result.schemas_added, result.observations_added,
                len(parsed.get("questions_to_queue") or []),
                result.questions_queued, result.archived,
                cost, result.notes, text[:60000], status,
            ),
        )
        result.cycle_id = cur.lastrowid

    return result

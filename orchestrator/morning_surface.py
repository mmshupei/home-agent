"""Surface night-cycle outputs into the next session's system prompt.

Two streams:
- question_pending memories — at most 3 surfaced/day total across all sessions
  - 14-day timeout: anything not engaged with for 14 days gets soft-archived
    and a 'lesson' written so the night cycle won't immediately re-ask
- commitments due today — pulled from night_cycles.raw_output[*].commitments
  - For now, derived inline from the most recent cycle's reflection. M9
    promotion: a `commitments` table when the data justifies it.

Surfacing is a side effect — calling render() marks the surfaced rows as
last_seen=now so they decay slower if the user engages.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .auth import Principal
from .db import connect

DAILY_QUESTION_CAP = 3
QUESTION_AUTO_ARCHIVE_DAYS = 14


def _today_surfaced_count() -> int:
    with connect() as c:
        r = c.execute(
            """SELECT COUNT(*) AS n FROM memory
               WHERE kind = 'question_pending'
                 AND last_seen >= date('now', 'localtime')"""
        ).fetchone()
    return int(r["n"]) if r else 0


def _candidates() -> list[dict]:
    """Highest-priority unanswered questions, in family scope, oldest first
    (older = more stale, more pressing)."""
    with connect() as c:
        rows = c.execute(
            """SELECT id, scope, content, created_at, last_seen, confidence
               FROM memory
               WHERE kind = 'question_pending'
                 AND scope = 'family'
                 AND confidence > 0
               ORDER BY created_at ASC LIMIT 10"""
        ).fetchall()
    return [dict(r) for r in rows]


def _auto_archive_stale() -> int:
    cutoff = (datetime.utcnow() - timedelta(days=QUESTION_AUTO_ARCHIVE_DAYS)).isoformat()
    with connect() as c:
        rows = c.execute(
            """SELECT id, content FROM memory
               WHERE kind = 'question_pending' AND confidence > 0
                 AND created_at < ?""",
            (cutoff,),
        ).fetchall()
        n = 0
        for r in rows:
            c.execute(
                "UPDATE memory SET confidence = 0 WHERE id = ?", (r["id"],)
            )
            c.execute(
                """INSERT INTO memory(scope, kind, content, created_by, confidence)
                   VALUES ('system', 'lesson', ?, 'morning_surface', 0.6)""",
                (
                    f"Question went unanswered for {QUESTION_AUTO_ARCHIVE_DAYS} days; "
                    f"do not re-ask without new evidence: {r['content']!r}",
                ),
            )
            n += 1
    return n


def render(principal: Principal) -> str:
    """Return a markdown block to inject into compose_prompt, or '' if nothing
    to surface. Side-effects: marks surfaced rows last_seen=now;
    archives questions older than 14 days with a 'lesson' so the cycle
    doesn't re-ask."""
    _auto_archive_stale()

    # Nothing to surface to non-family-visible roles in M9 (kept simple).
    if principal.role not in ("admin", "adult"):
        return ""

    today_used = _today_surfaced_count()
    remaining = max(0, DAILY_QUESTION_CAP - today_used)
    if remaining == 0:
        return ""

    cands = _candidates()
    if not cands:
        return ""

    pick = cands[: min(remaining, 1)]  # one per session, even if cap allows more

    surfaced_ids = [r["id"] for r in pick]
    with connect() as c:
        c.execute(
            "UPDATE memory SET last_seen = CURRENT_TIMESTAMP "
            "WHERE id IN ({})".format(",".join("?" * len(surfaced_ids))),
            surfaced_ids,
        )

    lines = ["## Pending question from last night's consolidation"]
    for r in pick:
        lines.append(f"- {r['content']}  _(queued {r['created_at']})_")
    lines.append(
        "Surface this naturally if it fits the conversation. Don't interrogate. "
        "If the user answers, write the answer as a high-confidence memory and "
        "note the question is resolved."
    )
    return "\n".join(lines)

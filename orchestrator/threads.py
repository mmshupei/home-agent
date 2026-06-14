"""Conversation thread state.

A *thread* is a multi-turn conversational arc on one surface for one
principal. Ren uses it to know "are we still in something?" and to
decide whether to keep the thread warm or compact it (summarize and
close so the next turn starts fresh).

Compacting is the operation that makes long arcs sustainable. Without
it the in-memory thread buffer just keeps getting longer until it ages
out by time alone. Compacting writes a summary memory tied to the
thread, marks the thread closed, and frees the next turn to start
clean.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .auth import Principal
from .db import connect


@dataclass
class Thread:
    id: int
    surface: str
    principal: str
    started_at: str
    last_active: str
    closed_at: Optional[str]
    closed_reason: Optional[str]
    summary: Optional[str]
    turn_count: int
    last_turn_text: Optional[str]


def _row(r) -> Thread:
    return Thread(
        id=r["id"], surface=r["surface"], principal=r["principal"],
        started_at=str(r["started_at"]), last_active=str(r["last_active"]),
        closed_at=(str(r["closed_at"]) if r["closed_at"] else None),
        closed_reason=r["closed_reason"], summary=r["summary"],
        turn_count=int(r["turn_count"] or 0),
        last_turn_text=r["last_turn_text"],
    )


def get_open(surface: str, principal_user_id: str) -> Optional[Thread]:
    with connect() as c:
        r = c.execute(
            "SELECT * FROM conversation_threads "
            "WHERE surface=? AND principal=? AND closed_at IS NULL "
            "LIMIT 1",
            (surface, principal_user_id),
        ).fetchone()
    return _row(r) if r else None


def record_turn(
    surface: str, principal_user_id: str, *, last_turn_text: str
) -> Thread:
    """Increment turn_count + last_active on the open thread, creating
    one if none exists. Returns the thread."""
    short = (last_turn_text or "")[:200]
    with connect() as c:
        existing = c.execute(
            "SELECT id FROM conversation_threads "
            "WHERE surface=? AND principal=? AND closed_at IS NULL",
            (surface, principal_user_id),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE conversation_threads "
                "SET turn_count = turn_count + 1, "
                "    last_active = CURRENT_TIMESTAMP, "
                "    last_turn_text = ? "
                "WHERE id = ?",
                (short, existing["id"]),
            )
        else:
            c.execute(
                "INSERT INTO conversation_threads "
                "(surface, principal, turn_count, last_turn_text) "
                "VALUES (?, ?, 1, ?)",
                (surface, principal_user_id, short),
            )
    return get_open(surface, principal_user_id)  # type: ignore[return-value]


def compact(
    surface: str, principal_user_id: str, *,
    summary: str, reason: str = "compacted",
) -> tuple[bool, str]:
    """Close the open thread with a summary. Also writes a memory row in
    family scope so the summary becomes part of long-term context."""
    summary = (summary or "").strip()
    if len(summary) < 20:
        return False, "summary must be >= 20 chars"

    open_thread = get_open(surface, principal_user_id)
    if not open_thread:
        return False, f"no open thread on {surface} for {principal_user_id}"

    with connect() as c:
        c.execute(
            "UPDATE conversation_threads "
            "SET closed_at = CURRENT_TIMESTAMP, closed_reason = ?, summary = ? "
            "WHERE id = ?",
            (reason, summary, open_thread.id),
        )

    # Also persist the summary as a memory row so retrieval picks it up.
    from . import memory as mem
    mem.write(
        content=f"[Past thread, {open_thread.started_at}, {surface}, {open_thread.turn_count} turns] {summary}",
        scope="family",
        kind="lesson",
        created_by=principal_user_id,
        confidence=0.9,
    )

    return True, f"compacted thread #{open_thread.id} ({open_thread.turn_count} turns)"


def status(surface: str, principal_user_id: str) -> dict:
    """Cheap structured snapshot for thread__status."""
    t = get_open(surface, principal_user_id)
    if not t:
        return {"open": False, "surface": surface, "principal": principal_user_id}

    with connect() as c:
        # Compute deltas in DB so timezones don't drift.
        r = c.execute(
            "SELECT (julianday('now') - julianday(started_at)) * 24 * 60 AS minutes_since_start, "
            "       (julianday('now') - julianday(last_active)) * 24 * 60 AS minutes_idle "
            "FROM conversation_threads WHERE id = ?",
            (t.id,),
        ).fetchone()

    return {
        "open": True,
        "thread_id": t.id,
        "surface": t.surface,
        "started_at": t.started_at,
        "last_active": t.last_active,
        "turn_count": t.turn_count,
        "last_turn_preview": (t.last_turn_text or "")[:100],
        "minutes_since_start": round(r["minutes_since_start"], 1),
        "minutes_idle": round(r["minutes_idle"], 1),
    }


def recent_closed(
    surface: str, principal_user_id: str, *, limit: int = 5
) -> list[Thread]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM conversation_threads "
            "WHERE surface=? AND principal=? AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?",
            (surface, principal_user_id, limit),
        ).fetchall()
    return [_row(r) for r in rows]

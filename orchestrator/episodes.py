"""Episode lifecycle.

Every loop.run() invocation is an episode. M10 will add Reachy interactions as
episodes too. The night cycle (M9) consumes unconsolidated episodes nightly.

Lifecycle:
1. start(source, principal, session_id) -> episode_id
2. ... interaction happens (transcript accumulates in the session jsonl) ...
3. close(episode_id, transcript, summary, affect) — sets ended_at, embeds the
   summary, and leaves consolidated_at NULL for the night cycle.

Episodes are immutable after close() returns.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from .auth import Principal
from .db import connect, has_vec
from .embed import embed, serialize


def start(
    *,
    source: str,
    principal: Principal,
    session_id: str | None = None,
    participants: list[str] | None = None,
) -> int:
    """Open an episode. Returns the new id."""
    with connect() as c:
        cur = c.execute(
            """INSERT INTO episodes(source, principal, participants, started_at, session_id)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)""",
            (
                source,
                principal.user_id,
                json.dumps(participants or [principal.user_id]),
                session_id,
            ),
        )
        return int(cur.lastrowid)


def close(
    episode_id: int,
    *,
    transcript: str,
    summary: str | None = None,
    affect: dict | None = None,
    audio_path: str | None = None,
) -> None:
    """Finalize an episode: set ended_at, store transcript+summary, embed
    the summary so the night cycle can cluster across episodes."""
    summary = (summary or "").strip() or _auto_summary(transcript)
    embedding_blob: Optional[bytes] = None
    try:
        if has_vec_at_runtime():
            vec = embed(summary, input_type="document")
            embedding_blob = serialize(vec)
    except Exception:
        embedding_blob = None  # never fail close() on embedding errors

    with connect() as c:
        c.execute(
            """UPDATE episodes
               SET ended_at = CURRENT_TIMESTAMP,
                   transcript = ?,
                   summary = ?,
                   affect = ?,
                   audio_path = ?,
                   embedding = ?
               WHERE id = ? AND ended_at IS NULL""",
            (
                transcript,
                summary,
                json.dumps(affect or {}),
                audio_path,
                embedding_blob,
                episode_id,
            ),
        )


def has_vec_at_runtime() -> bool:
    with connect() as c:
        return has_vec(c)


def _auto_summary(transcript: str, max_chars: int = 300) -> str:
    """Cheap fallback summary when none provided. Just the head of the
    transcript. The night cycle's pass will add real consolidation_notes."""
    s = (transcript or "").strip().splitlines()
    head = " · ".join(s[:5]) if s else ""
    return head[:max_chars]


def fetch_unconsolidated(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT id, source, principal, participants, started_at, ended_at,
                      summary, transcript, affect
               FROM episodes
               WHERE consolidated_at IS NULL AND ended_at IS NOT NULL
               ORDER BY started_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_consolidated(episode_ids: list[int], notes: str | None = None) -> int:
    if not episode_ids:
        return 0
    with connect() as c:
        cur = c.execute(
            "UPDATE episodes SET consolidated_at = CURRENT_TIMESTAMP, "
            "consolidation_notes = ? "
            "WHERE id IN ({})".format(",".join("?" * len(episode_ids))),
            (notes, *episode_ids),
        )
        return cur.rowcount

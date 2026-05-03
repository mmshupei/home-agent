"""Memory inventory snapshot the dream agent reasons over.

All queries run against whatever DB is at AGENT_DB_PATH at call time, so the
cycle entrypoint can swap in the sandbox snapshot before calling these.

We compute three things:
1. Inventory — every system/family memory row (compact, includes last_seen)
2. Similarity pairs — pairs whose cosine sim is in (low, high) range and
   whose content differs textually. The dream agent looks at these for
   contradictions and near-duplicates.
3. Stale candidates — confidence < 0.5 AND last_seen > 30 days ago AND
   not user-explicit (kind != 'fact' or confidence < 0.99).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from orchestrator.db import connect, has_vec
from orchestrator.embed import serialize


SIMILARITY_LOW = 0.80   # below this, ignore (unrelated)
SIMILARITY_HIGH = 1.0   # above this, identical (vec table dedupes anyway)


def fetch_inventory() -> list[dict]:
    """All active system+family memory rows."""
    with connect() as c:
        rows = c.execute(
            """SELECT id, scope, kind, content, confidence,
                      created_at, created_by, last_seen
               FROM memory
               WHERE scope IN ('system','family')
                 AND confidence > 0
               ORDER BY id"""
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_similarity_pairs(threshold: float = SIMILARITY_LOW) -> list[dict]:
    """Find pairs of system+family memories whose cosine sim is >= threshold
    AND whose content differs. Uses vec0's KNN per row, then dedupes pairs.
    Returns at most 25 pairs (highest sim first)."""
    with connect() as c:
        if not has_vec(c):
            return []
        # Pull (id, content, embedding) for all system+family rows
        ids = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM memory WHERE scope IN ('system','family') "
                "AND confidence > 0"
            ).fetchall()
        ]
        if len(ids) < 2:
            return []

        seen: set[tuple[int, int]] = set()
        pairs: list[dict] = []

        for mid in ids:
            row = c.execute(
                "SELECT embedding FROM memory_vec WHERE rowid = ?", (mid,)
            ).fetchone()
            if not row or row["embedding"] is None:
                continue
            qvec = bytes(row["embedding"])
            # KNN against the same table; k=6 (mid + 5 neighbors)
            knn = c.execute(
                """SELECT m.id, m.scope, m.content, m.confidence,
                          m.created_at, v.distance
                   FROM memory_vec v JOIN memory m ON m.id = v.rowid
                   WHERE v.embedding MATCH ? AND k = 6
                     AND m.scope IN ('system','family') AND m.confidence > 0
                   ORDER BY v.distance""",
                (qvec,),
            ).fetchall()
            base = c.execute(
                "SELECT id, scope, content, confidence, created_at "
                "FROM memory WHERE id = ?",
                (mid,),
            ).fetchone()
            if not base:
                continue
            for n in knn:
                if n["id"] == mid:
                    continue
                a, b = sorted((mid, n["id"]))
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                sim = 1.0 - float(n["distance"])
                if sim < threshold or sim > SIMILARITY_HIGH:
                    continue
                if base["content"].strip() == n["content"].strip():
                    continue
                pairs.append({
                    "sim": round(sim, 3),
                    "a_id": base["id"], "a_scope": base["scope"],
                    "a_content": base["content"],
                    "a_confidence": float(base["confidence"]),
                    "a_created_at": str(base["created_at"]),
                    "b_id": n["id"], "b_scope": n["scope"],
                    "b_content": n["content"],
                    "b_confidence": float(n["confidence"]),
                    "b_created_at": str(n["created_at"]),
                })
        pairs.sort(key=lambda p: p["sim"], reverse=True)
        return pairs[:25]


def fetch_stale_candidates() -> list[dict]:
    """Memories worth considering for archive: low confidence, untouched.
    Excludes user-explicit facts (confidence >= 0.99 AND kind = 'fact')."""
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    with connect() as c:
        rows = c.execute(
            """SELECT id, scope, kind, content, confidence, last_seen, created_at
               FROM memory
               WHERE scope IN ('system','family')
                 AND confidence > 0 AND confidence < 0.5
                 AND last_seen < ?
                 AND NOT (kind = 'fact' AND confidence >= 0.99)
               ORDER BY last_seen ASC LIMIT 30""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]

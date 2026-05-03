"""Scoped memory: write, read, retrieve context, forget, contradict.

A query from principal P sees:
  - scope = 'system'
  - scope = 'family'
  - scope = 'user:' || P.user_id
  - all 'user:*' scopes if P.role == 'admin' (logged as a privileged read)

Vector retrieval uses sqlite-vec when available; otherwise falls back to
recency-ordered rows. Pinned facts (kind='fact', confidence=1.0, scope in
system|family) and upcoming events (next 7 days) are always included.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .auth import Principal
from .db import connect, has_vec
from .embed import embed, serialize

CONTRADICTION_SIM_THRESHOLD = 0.85  # cosine sim above which "different content" looks like a contradiction
# Calibrated against bge-m3: a numeric edit ("4729" → "9999") in an otherwise
# identical sentence scores ~0.90; a paraphrased rewrite ~0.82; unrelated ~0.28.
# 0.85 catches numeric-edit contradictions without flagging paraphrases.


@dataclass
class MemoryRow:
    id: int
    scope: str
    kind: str
    content: str
    confidence: float
    created_at: str
    created_by: str


# ---------------------------------------------------------------------------
# Scope visibility
# ---------------------------------------------------------------------------


def visible_scopes(principal: Principal) -> list[str]:
    base = ["system", "family", f"user:{principal.user_id}"]
    if principal.role == "admin":
        # admin sees all user scopes; resolved at query time via LIKE pattern.
        base.append("__ALL_USERS__")
    return base


def _scope_clause(scopes: Iterable[str]) -> tuple[str, list]:
    """Build a SQL fragment + params from a scope list. The sentinel
    '__ALL_USERS__' becomes scope LIKE 'user:%'."""
    eq, like = [], []
    for s in scopes:
        if s == "__ALL_USERS__":
            like.append("user:%")
        else:
            eq.append(s)
    parts, params = [], []
    if eq:
        parts.append("scope IN ({})".format(",".join("?" * len(eq))))
        params.extend(eq)
    if like:
        like_terms = " OR ".join("scope LIKE ?" for _ in like)
        parts.append(f"({like_terms})")
        params.extend(like)
    return "(" + " OR ".join(parts) + ")", params


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def write(
    *,
    content: str,
    scope: str,
    kind: str = "fact",
    created_by: str,
    confidence: float = 1.0,
    expires_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert a memory row + its embedding (if vec available). Returns the row id."""
    own = conn is None
    if own:
        conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO memory(scope, kind, content, created_by, expires_at, confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scope, kind, content, created_by, expires_at, confidence),
        )
        mem_id = cur.lastrowid
        if has_vec(conn):
            vec = embed(content, input_type="document")
            conn.execute(
                "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                (mem_id, serialize(vec)),
            )
        return mem_id
    finally:
        if own:
            conn.close()


def forget(
    memory_id: int,
    *,
    revised_by: str = "memory_tool",
    reason: str | None = None,
    cycle_run_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Soft-delete: set confidence to 0 and remove from vec index.
    Logs a memory_revision so revert-cycle can undo it."""
    own = conn is None
    if own:
        conn = connect()
    try:
        old = conn.execute(
            "SELECT confidence FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()
        if not old:
            return False
        cur = conn.execute(
            "UPDATE memory SET confidence = 0 WHERE id = ?", (memory_id,)
        )
        if cur.rowcount > 0:
            _log_revision(
                conn, memory_id, revised_by, "archived",
                old_value=str(old["confidence"]), new_value="0",
                reason=reason, cycle_run_at=cycle_run_at,
            )
        if has_vec(conn):
            conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def update_confidence(
    memory_id: int,
    new_confidence: float,
    *,
    revised_by: str,
    reason: str | None = None,
    cycle_run_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Adjust a memory's confidence. Logged. Used by night cycle decay and by
    M11.1 lower_confidence proposals after approval."""
    own = conn is None
    if own:
        conn = connect()
    try:
        old = conn.execute(
            "SELECT confidence FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()
        if not old:
            return False
        if abs(float(old["confidence"]) - float(new_confidence)) < 1e-9:
            return True  # no-op
        conn.execute(
            "UPDATE memory SET confidence = ? WHERE id = ?",
            (new_confidence, memory_id),
        )
        _log_revision(
            conn, memory_id, revised_by, "confidence",
            old_value=str(old["confidence"]), new_value=str(new_confidence),
            reason=reason, cycle_run_at=cycle_run_at,
        )
        return True
    finally:
        if own:
            conn.close()


def _log_revision(
    conn: sqlite3.Connection,
    memory_id: int,
    revised_by: str,
    field: str,
    *,
    old_value: str | None,
    new_value: str | None,
    reason: str | None,
    cycle_run_at: datetime | None,
) -> None:
    conn.execute(
        """INSERT INTO memory_revisions
           (memory_id, revised_by, field, old_value, new_value, reason, cycle_run_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (memory_id, revised_by, field, old_value, new_value, reason, cycle_run_at),
    )


def revert_cycle(cycle_run_at: datetime) -> int:
    """Undo every revision a single night cycle made. Returns the count of
    rows reverted. Operates by replaying old_value back into the memory row."""
    with connect() as c:
        revs = c.execute(
            """SELECT id, memory_id, field, old_value
               FROM memory_revisions
               WHERE cycle_run_at = ? AND revised_by = 'night_cycle'
               ORDER BY id DESC""",
            (cycle_run_at,),
        ).fetchall()
        n = 0
        for r in revs:
            field, old = r["field"], r["old_value"]
            if field == "confidence" or field == "archived":
                try:
                    val = float(old) if old is not None else None
                except (TypeError, ValueError):
                    continue
                c.execute(
                    "UPDATE memory SET confidence = ? WHERE id = ?",
                    (val, r["memory_id"]),
                )
            elif field == "content":
                c.execute(
                    "UPDATE memory SET content = ? WHERE id = ?",
                    (old, r["memory_id"]),
                )
            elif field == "scope":
                c.execute(
                    "UPDATE memory SET scope = ? WHERE id = ?",
                    (old, r["memory_id"]),
                )
            else:
                continue
            n += 1
        # Mark the original revisions reverted by appending a counter-revision
        for r in revs:
            c.execute(
                """INSERT INTO memory_revisions
                   (memory_id, revised_by, field, old_value, new_value, reason)
                   VALUES (?, 'revert', ?, NULL, NULL, ?)""",
                (r["memory_id"], r["field"], f"reverted revision id={r['id']}"),
            )
    return n


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_by_id(memory_id: int) -> MemoryRow | None:
    with connect() as c:
        r = c.execute(
            "SELECT id, scope, kind, content, confidence, created_at, created_by "
            "FROM memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
    return _row(r) if r else None


def search(
    *,
    query: str,
    principal: Principal,
    top_k: int = 12,
    min_confidence: float = 0.3,
) -> list[MemoryRow]:
    """Semantic search if vec available; else LIKE + recency."""
    visible = visible_scopes(principal)
    scope_sql, scope_params = _scope_clause(visible)
    with connect() as c:
        if has_vec(c):
            qvec = serialize(embed(query, input_type="query"))
            # vec0 KNN: MATCH binds the query vector, k filters to N nearest.
            # Over-fetch (4x), then apply scope/confidence filters in the join,
            # then trim to top_k.
            sql = f"""
                SELECT m.id, m.scope, m.kind, m.content, m.confidence,
                       m.created_at, m.created_by, v.distance
                FROM memory_vec v
                JOIN memory m ON m.id = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                  AND {scope_sql}
                  AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
                  AND m.confidence > ?
                ORDER BY v.distance
                LIMIT ?
            """
            params = [qvec, top_k * 4, *scope_params, min_confidence, top_k]
            rows = c.execute(sql, params).fetchall()
        else:
            # Fallback: recency.
            sql = f"""
                SELECT id, scope, kind, content, confidence, created_at, created_by
                FROM memory
                WHERE {scope_sql}
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                  AND confidence > ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            rows = c.execute(sql, [*scope_params, min_confidence, top_k]).fetchall()
        # Touch last_seen so retrieved rows decay slower.
        if rows:
            ids = [r["id"] for r in rows]
            c.execute(
                "UPDATE memory SET last_seen = CURRENT_TIMESTAMP "
                "WHERE id IN ({})".format(",".join("?" * len(ids))),
                ids,
            )
    return [_row(r) for r in rows]


def pinned_facts(principal: Principal, limit: int = 8) -> list[MemoryRow]:
    """Always-on facts: kind='fact', confidence=1.0, scope in system|family."""
    with connect() as c:
        rows = c.execute(
            """SELECT id, scope, kind, content, confidence, created_at, created_by
               FROM memory
               WHERE kind = 'fact' AND confidence >= 0.99
                 AND scope IN ('system', 'family')
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


def upcoming_events(principal: Principal, days: int = 7) -> list[dict]:
    visible = visible_scopes(principal)
    scope_sql, scope_params = _scope_clause(visible)
    with connect() as c:
        sql = f"""
            SELECT title, starts_at, ends_at, location
            FROM events
            WHERE {scope_sql}
              AND starts_at BETWEEN CURRENT_TIMESTAMP
                                AND datetime('now', ?)
            ORDER BY starts_at
        """
        rows = c.execute(sql, [*scope_params, f"+{int(days)} days"]).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Context block (rendered into the system prompt)
# ---------------------------------------------------------------------------


def retrieve_context(
    task: str,
    principal: Principal,
    *,
    top_k: int = 12,
    max_chars: int = 6000,
) -> str:
    """Compose a markdown block of pinned facts + retrieved memories + upcoming
    events. Capped at ~max_chars so it doesn't dominate the prompt."""
    pinned = pinned_facts(principal)
    retrieved = search(query=task, principal=principal, top_k=top_k)
    events = upcoming_events(principal)

    seen_ids = {p.id for p in pinned}
    extra = [r for r in retrieved if r.id not in seen_ids]

    lines: list[str] = ["## Memory context"]

    if pinned:
        lines.append("\n**Pinned facts**")
        for m in pinned:
            lines.append(f"- ({m.scope}) {m.content}")

    if extra:
        lines.append("\n**Relevant memories**")
        for m in extra:
            lines.append(f"- ({m.scope}/{m.kind}, conf={m.confidence:.2f}) {m.content}")

    if events:
        lines.append("\n**Upcoming (next 7 days)**")
        for e in events:
            when = e["starts_at"]
            loc = f" @ {e['location']}" if e.get("location") else ""
            lines.append(f"- {when}: {e['title']}{loc}")

    if len(lines) == 1:
        return ""

    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n…(truncated)"
    return block


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


def find_contradictions(content: str, principal: Principal) -> list[MemoryRow]:
    """Return existing memories whose embedding is highly similar to `content`
    but whose textual content differs. Caller decides how to surface them."""
    visible = visible_scopes(principal)
    scope_sql, scope_params = _scope_clause(visible)
    with connect() as c:
        if not has_vec(c):
            return []
        qvec = serialize(embed(content, input_type="query"))
        sql = f"""
            SELECT m.id, m.scope, m.kind, m.content, m.confidence,
                   m.created_at, m.created_by, v.distance
            FROM memory_vec v JOIN memory m ON m.id = v.rowid
            WHERE v.embedding MATCH ? AND k = 8
              AND {scope_sql}
              AND (m.expires_at IS NULL OR m.expires_at > CURRENT_TIMESTAMP)
              AND m.confidence > 0.3
            ORDER BY v.distance
        """
        rows = c.execute(sql, [qvec, *scope_params]).fetchall()
    out: list[MemoryRow] = []
    for r in rows:
        sim = 1.0 - float(r["distance"])  # cosine sim
        if sim >= CONTRADICTION_SIM_THRESHOLD and r["content"].strip() != content.strip():
            out.append(_row(r))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(r) -> MemoryRow:
    return MemoryRow(
        id=r["id"],
        scope=r["scope"],
        kind=r["kind"],
        content=r["content"],
        confidence=r["confidence"],
        created_at=str(r["created_at"]),
        created_by=r["created_by"],
    )


def to_jsonable(rows: list[MemoryRow]) -> str:
    return json.dumps([r.__dict__ for r in rows], default=str, indent=2)

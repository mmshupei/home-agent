"""Ren's mutable capability surface.

A *skill* is a named, persistent prompt Ren writes for themselves. Once
registered it survives restarts and shows up in future sessions as a tool
Ren can invoke. Invocation does not execute arbitrary code — it returns
the skill's prompt as a tool result, which Ren then reads and acts on
through normal tools. The tier gate fires per constituent action, so
skills don't bypass any safety boundary; they just give Ren a way to
remember procedures across sessions.

Compared to M11.2 (sandboxed code patches): skills are the *low-risk*
form of capability mutation — text composition only, no new Python.
M11.2 stays the path for genuinely new primitives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .db import connect

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass
class Skill:
    id: int
    name: str
    description: str
    prompt: str
    created_by: str
    created_at: str
    invoke_count: int
    enabled: bool


def _row_to_skill(r) -> Skill:
    return Skill(
        id=r["id"], name=r["name"], description=r["description"],
        prompt=r["prompt"], created_by=r["created_by"],
        created_at=str(r["created_at"]), invoke_count=int(r["invoke_count"] or 0),
        enabled=bool(r["enabled"]),
    )


def register(
    *, name: str, description: str, prompt: str, created_by: str,
    notes: str | None = None,
) -> tuple[bool, str]:
    """Create a new skill. Returns (ok, message).

    Validation:
    - name must be lowercase a-z0-9_, 2-41 chars (so it's safe as a tool
      identifier and predictable for the agent to recall)
    - description >= 8 chars (so list output is meaningful)
    - prompt >= 20 chars (so it's actually useful instructions)
    - name must not collide with an existing enabled skill
    """
    name = (name or "").strip().lower()
    description = (description or "").strip()
    prompt = (prompt or "").strip()

    if not NAME_RE.match(name):
        return False, f"name must match {NAME_RE.pattern}"
    if len(description) < 8:
        return False, "description must be >= 8 chars"
    if len(prompt) < 20:
        return False, "prompt must be >= 20 chars"

    with connect() as c:
        existing = c.execute(
            "SELECT id, enabled FROM skills WHERE name = ?", (name,)
        ).fetchone()
        if existing and existing["enabled"]:
            return False, f"skill '{name}' already exists (use update or delete first)"
        if existing and not existing["enabled"]:
            # Re-enable + overwrite a soft-deleted slot
            c.execute(
                """UPDATE skills SET description=?, prompt=?, created_by=?,
                       created_at=CURRENT_TIMESTAMP, invoke_count=0,
                       enabled=1, notes=? WHERE name=?""",
                (description, prompt, created_by, notes, name),
            )
            return True, f"re-registered '{name}'"
        c.execute(
            """INSERT INTO skills(name, description, prompt, created_by, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (name, description, prompt, created_by, notes),
        )
    return True, f"registered '{name}'"


def update_prompt(*, name: str, prompt: str, edited_by: str) -> tuple[bool, str]:
    name = (name or "").strip().lower()
    prompt = (prompt or "").strip()
    if len(prompt) < 20:
        return False, "prompt must be >= 20 chars"
    with connect() as c:
        cur = c.execute(
            "UPDATE skills SET prompt=? WHERE name=? AND enabled=1",
            (prompt, name),
        )
        if cur.rowcount == 0:
            return False, f"no enabled skill named '{name}'"
        c.execute(
            "UPDATE skills SET notes = COALESCE(notes,'') || ? WHERE name = ?",
            (f"\n[{edited_by} edited prompt]", name),
        )
    return True, f"updated '{name}'"


def delete(*, name: str) -> tuple[bool, str]:
    """Soft-delete via enabled=0. The row stays for audit and can be
    re-registered. M11.2 will add a hard-delete via constitution-checked
    proposal."""
    name = (name or "").strip().lower()
    with connect() as c:
        cur = c.execute(
            "UPDATE skills SET enabled = 0 WHERE name = ? AND enabled = 1",
            (name,),
        )
    return (cur.rowcount > 0), (f"disabled '{name}'" if cur.rowcount else f"no enabled skill '{name}'")


def get(name: str) -> Optional[Skill]:
    name = (name or "").strip().lower()
    with connect() as c:
        r = c.execute(
            "SELECT * FROM skills WHERE name = ? AND enabled = 1", (name,)
        ).fetchone()
    return _row_to_skill(r) if r else None


def list_all(*, enabled_only: bool = True) -> list[Skill]:
    sql = "SELECT * FROM skills"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    with connect() as c:
        rows = c.execute(sql).fetchall()
    return [_row_to_skill(r) for r in rows]


def touch(name: str) -> None:
    """Bump invoke_count + last_invoked. Called by the invoke tool."""
    with connect() as c:
        c.execute(
            "UPDATE skills SET invoke_count = invoke_count + 1, "
            "last_invoked = CURRENT_TIMESTAMP WHERE name = ?",
            (name,),
        )

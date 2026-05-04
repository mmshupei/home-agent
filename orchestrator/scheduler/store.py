"""CRUD + due-detection over the ``scheduled_actions`` table.

Pure Python — no asyncio, no SDK, no Telegram. Safe to call from anywhere
that has a sqlite connection.

Naming and shape mirror ``orchestrator/skills.py``:
- name is unique, lowercase a-z0-9_, validated against NAME_RE
- soft-delete via enabled=0
- re-registering a previously-disabled name re-enables + overwrites it
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from ..db import connect

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
VALID_TARGET_KINDS = ("skill", "prompt")


@dataclass
class ScheduledAction:
    id: int
    name: str
    cron_expr: Optional[str]
    run_once_at: Optional[datetime]    # always tz-aware UTC
    target_kind: str
    target: str
    owner_user: str
    timezone: str
    last_fired_at: Optional[datetime]  # always tz-aware UTC
    last_status: Optional[str]
    last_error: Optional[str]
    last_cost_usd: Optional[float]
    enabled: bool
    created_by: str
    notes: Optional[str]


def _parse_ts(value) -> Optional[datetime]:
    """SQLite timestamps come back as naive strings (or None). Promote to
    tz-aware UTC. ``CURRENT_TIMESTAMP`` writes are already UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    # SQLite default format: 'YYYY-MM-DD HH:MM:SS'. Also accept ISO-8601 with 'T'.
    s = s.replace("T", " ")
    # Strip a trailing Z if present.
    if s.endswith("Z"):
        s = s[:-1]
    # Try a few formats; bail to fromisoformat last.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _row_to_action(r) -> ScheduledAction:
    return ScheduledAction(
        id=r["id"],
        name=r["name"],
        cron_expr=r["cron_expr"],
        run_once_at=_parse_ts(r["run_once_at"]),
        target_kind=r["target_kind"],
        target=r["target"],
        owner_user=r["owner_user"],
        timezone=r["timezone"],
        last_fired_at=_parse_ts(r["last_fired_at"]),
        last_status=r["last_status"],
        last_error=r["last_error"],
        last_cost_usd=r["last_cost_usd"],
        enabled=bool(r["enabled"]),
        created_by=r["created_by"],
        notes=r["notes"],
    )


def _validate_cron(expr: str) -> tuple[bool, str]:
    try:
        croniter(expr, datetime.now(timezone.utc))
        return True, ""
    except Exception as e:
        return False, f"invalid cron expression: {e}"


def register(
    *,
    name: str,
    target_kind: str,
    target: str,
    owner_user: str,
    created_by: str,
    cron_expr: Optional[str] = None,
    run_once_at: Optional[datetime] = None,
    timezone_name: str = "America/Los_Angeles",
    notes: Optional[str] = None,
) -> tuple[bool, str]:
    """Insert (or re-enable + overwrite) a scheduled action.

    Exactly one of ``cron_expr`` / ``run_once_at`` must be provided.
    """
    name = (name or "").strip().lower()
    target_kind = (target_kind or "").strip()
    target = (target or "").strip()

    if not NAME_RE.match(name):
        return False, f"name must match {NAME_RE.pattern}"
    if target_kind not in VALID_TARGET_KINDS:
        return False, f"target_kind must be one of {VALID_TARGET_KINDS}"
    if len(target) < 2:
        return False, "target must be non-empty"
    if (cron_expr is None) == (run_once_at is None):
        return False, "exactly one of cron_expr or run_once_at is required"
    if cron_expr is not None:
        ok, msg = _validate_cron(cron_expr)
        if not ok:
            return False, msg
    if run_once_at is not None:
        # Normalize to tz-aware UTC. If naive, assume UTC.
        if run_once_at.tzinfo is None:
            run_once_at = run_once_at.replace(tzinfo=timezone.utc)
        run_once_at = run_once_at.astimezone(timezone.utc)
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return False, f"unknown timezone: {timezone_name!r}"

    with connect() as c:
        # Verify owner exists.
        owner = c.execute("SELECT id FROM users WHERE id = ?", (owner_user,)).fetchone()
        if not owner:
            return False, f"unknown owner_user: {owner_user!r}"

        existing = c.execute(
            "SELECT id, enabled FROM scheduled_actions WHERE name = ?", (name,)
        ).fetchone()
        run_once_str = run_once_at.strftime("%Y-%m-%d %H:%M:%S") if run_once_at else None

        if existing and existing["enabled"]:
            return False, (
                f"scheduled action {name!r} already exists "
                f"(use schedule__cancel first, or pick a new name)"
            )
        if existing and not existing["enabled"]:
            c.execute(
                """UPDATE scheduled_actions SET
                       cron_expr=?, run_once_at=?, target_kind=?, target=?,
                       owner_user=?, timezone=?, last_fired_at=NULL,
                       last_status=NULL, last_error=NULL, last_cost_usd=NULL,
                       enabled=1, created_at=CURRENT_TIMESTAMP, created_by=?,
                       notes=?
                   WHERE name=?""",
                (cron_expr, run_once_str, target_kind, target,
                 owner_user, timezone_name, created_by, notes, name),
            )
            return True, f"re-registered {name!r}"
        c.execute(
            """INSERT INTO scheduled_actions(
                   name, cron_expr, run_once_at, target_kind, target,
                   owner_user, timezone, created_by, notes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, cron_expr, run_once_str, target_kind, target,
             owner_user, timezone_name, created_by, notes),
        )
    return True, f"registered {name!r}"


def cancel(*, name: str) -> tuple[bool, str]:
    """Soft-delete via enabled=0. The row remains for audit."""
    name = (name or "").strip().lower()
    with connect() as c:
        cur = c.execute(
            "UPDATE scheduled_actions SET enabled = 0 "
            "WHERE name = ? AND enabled = 1",
            (name,),
        )
    if cur.rowcount:
        return True, f"cancelled {name!r}"
    return False, f"no enabled action named {name!r}"


def get(name: str) -> Optional[ScheduledAction]:
    name = (name or "").strip().lower()
    with connect() as c:
        r = c.execute(
            "SELECT * FROM scheduled_actions WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_action(r) if r else None


def list_all(
    *, owner_user: Optional[str] = None, enabled_only: bool = True,
) -> list[ScheduledAction]:
    sql = "SELECT * FROM scheduled_actions"
    where = []
    params: list = []
    if enabled_only:
        where.append("enabled = 1")
    if owner_user is not None:
        where.append("owner_user = ?")
        params.append(owner_user)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_action(r) for r in rows]


def mark_fired(
    *,
    name: str,
    fired_at: datetime,
    status: str,
    error: Optional[str] = None,
    cost_usd: Optional[float] = None,
    expected_prev_fire: Optional[datetime] = None,
) -> bool:
    """Atomically record that an action just fired. Returns False if a
    concurrent worker already marked the same period (advisory lock pattern).

    ``expected_prev_fire``: if provided, the row is updated only when its
    current ``last_fired_at`` is NULL or strictly less than this timestamp.
    This blocks two heartbeats from firing the same period twice.
    """
    name = (name or "").strip().lower()
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    stamp = fired_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if expected_prev_fire is not None:
        if expected_prev_fire.tzinfo is None:
            expected_prev_fire = expected_prev_fire.replace(tzinfo=timezone.utc)
        prev_str = expected_prev_fire.astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        sql = (
            "UPDATE scheduled_actions SET last_fired_at=?, last_status=?, "
            "last_error=?, last_cost_usd=? "
            "WHERE name=? AND (last_fired_at IS NULL OR last_fired_at < ?)"
        )
        params = (stamp, status, error, cost_usd, name, prev_str)
    else:
        sql = (
            "UPDATE scheduled_actions SET last_fired_at=?, last_status=?, "
            "last_error=?, last_cost_usd=? WHERE name=?"
        )
        params = (stamp, status, error, cost_usd, name)

    with connect() as c:
        cur = c.execute(sql, params)
    return cur.rowcount > 0


def previous_fire_utc(action: ScheduledAction, now_utc: datetime) -> Optional[datetime]:
    """For a cron action, the most recent scheduled tick at or before now.
    For a run-once action, the run_once_at if past, else None.
    Returns None for invalid configurations.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    if action.run_once_at is not None:
        return action.run_once_at if action.run_once_at <= now_utc else None
    if not action.cron_expr:
        return None
    try:
        tz = ZoneInfo(action.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now_local = now_utc.astimezone(tz)
    it = croniter(action.cron_expr, now_local)
    prev_local = it.get_prev(datetime)
    return prev_local.astimezone(timezone.utc)


def due_actions(now_utc: datetime) -> list[tuple[ScheduledAction, datetime]]:
    """Return enabled actions whose previous tick is <= now AND that haven't
    been fired since that tick. Each result is (action, expected_prev_fire_utc)
    so the runner can pass that to mark_fired for the advisory lock.

    Pure function: no DB writes.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    out: list[tuple[ScheduledAction, datetime]] = []
    for action in list_all(enabled_only=True):
        prev = previous_fire_utc(action, now_utc)
        if prev is None:
            continue
        if action.last_fired_at is None or action.last_fired_at < prev:
            out.append((action, prev))
    return out

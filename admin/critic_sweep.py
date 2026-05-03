"""Hourly catch-up critic pass.

Finds finished sessions that have zero critic_findings rows (inline critic
was dropped because the CLI's event loop closed early), and runs the critic
synchronously. Bounded to recent sessions so the sweep stays cheap.
"""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from orchestrator import critic
from orchestrator.db import connect, ensure_schema


SESSIONS_TO_REVIEW = 20  # max per sweep


async def main() -> int:
    load_dotenv()
    ensure_schema()

    with connect() as c:
        rows = c.execute(
            """SELECT s.id, s.task
               FROM sessions s
               LEFT JOIN critic_findings cf ON cf.session_id = s.id
               WHERE s.ended_at IS NOT NULL
                 AND s.started_at > datetime('now', '-7 days')
               GROUP BY s.id
               HAVING COUNT(cf.id) = 0
               ORDER BY s.started_at DESC
               LIMIT ?""",
            (SESSIONS_TO_REVIEW,),
        ).fetchall()

    if not rows:
        print("(no unreviewed sessions)")
        return 0

    print(f"reviewing {len(rows)} sessions...")
    total_findings = 0
    for r in rows:
        try:
            findings = await critic.review(r["id"])
            total_findings += len(findings)
            print(f"  {r['id'][:12]} -> {len(findings)} findings  task={r['task'][:60]!r}")
        except Exception as e:
            print(f"  {r['id'][:12]} -> ERROR: {type(e).__name__}: {e}")
    print(f"\ntotal new findings: {total_findings}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

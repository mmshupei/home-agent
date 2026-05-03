"""Session log: SQLite row + per-invocation jsonl on disk.

Mirrors Val Agent's transcript shape (one JSON object per line, with
`type` discriminator) so existing benchmarking infrastructure carries over.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .auth import Principal
from .db import connect

DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"


def runs_dir() -> Path:
    return Path(os.environ.get("AGENT_RUNS_DIR", DEFAULT_RUNS_DIR))


@dataclass
class Session:
    id: str
    principal: Principal
    profile: str
    task: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    final_message: str = ""
    _seq: int = 0
    _jsonl_path: Path | None = None

    @classmethod
    def new(cls, *, task: str, profile: str, principal: Principal) -> "Session":
        sess = cls(id=uuid.uuid4().hex, principal=principal, profile=profile, task=task)
        # SQLite row
        with connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id, principal, profile, task) VALUES (?, ?, ?, ?)",
                (sess.id, principal.user_id, profile, task),
            )
        # jsonl
        day = datetime.now().strftime("%Y-%m-%d")
        d = runs_dir() / day
        d.mkdir(parents=True, exist_ok=True)
        sess._jsonl_path = d / f"{sess.id}.jsonl"
        sess._write(
            {
                "type": "session_start",
                "session_id": sess.id,
                "principal": {
                    "user_id": principal.user_id,
                    "name": principal.name,
                    "role": principal.role,
                    "token_label": principal.token_label,
                },
                "profile": profile,
                "task": task,
                "ts": sess.started_at,
            }
        )
        return sess

    def _write(self, obj: dict[str, Any]) -> None:
        if not self._jsonl_path:
            return
        with self._jsonl_path.open("a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def record(self, msg: Any) -> None:
        """Persist an SDK message verbatim to the jsonl. Best-effort serialization."""
        self._write({"type": "sdk_message", "ts": time.time(), "payload": _safe(msg)})

    def log_intent(self, tool_input: dict, tier: int) -> int:
        """Called by the gate before a decision is made. Returns the seq number
        so log_decision can correlate."""
        self._seq += 1
        self._write(
            {
                "type": "tool_intent",
                "seq": self._seq,
                "tool_name": tool_input.get("tool_name"),
                "tier": tier,
                "input": tool_input.get("tool_input"),
                "ts": time.time(),
            }
        )
        return self._seq

    def log_decision(
        self,
        seq: int,
        tool_name: str,
        tier: int,
        tool_input: dict,
        decision: str,
        decided_by: str,
    ) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO tool_calls
                   (session_id, seq, tool_name, tier, input_json, decision, decided_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.id,
                    seq,
                    tool_name,
                    tier,
                    json.dumps(tool_input, default=str),
                    decision,
                    decided_by,
                ),
            )
        self._write(
            {
                "type": "tool_decision",
                "seq": seq,
                "decision": decision,
                "decided_by": decided_by,
                "ts": time.time(),
            }
        )

    def log_tool_result(self, seq: int, result: Any, duration_ms: int) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE tool_calls SET result_json = ?, duration_ms = ? "
                "WHERE session_id = ? AND seq = ?",
                (json.dumps(_safe(result), default=str), duration_ms, self.id, seq),
            )

    def finalize(
        self,
        *,
        final_message: str = "",
        token_count: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.final_message = final_message
        self.ended_at = time.time()
        with connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, "
                "final_msg = ?, token_count = ?, cost_usd = ? WHERE id = ?",
                (final_message, token_count, cost_usd, self.id),
            )
        self._write(
            {
                "type": "session_end",
                "session_id": self.id,
                "final_message": final_message,
                "token_count": token_count,
                "cost_usd": cost_usd,
                "ts": self.ended_at,
            }
        )


def _safe(obj: Any) -> Any:
    """Best-effort conversion of SDK objects (dataclasses, blocks) to JSON-able dicts."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        d = {"__class__": obj.__class__.__name__}
        for k, v in vars(obj).items():
            if k.startswith("_"):
                continue
            d[k] = _safe(v)
        return d
    return repr(obj)

"""Post-hoc Sonnet review of a finished session.

Per design §2: pre-action critics double latency. The critic runs AFTER the
session ends, reads the persisted jsonl + audit rows, and writes structured
findings. High-severity findings get surfaced at the next session start as
cautions in the system prompt.

Scheduling: schedule_critic() spawns the review as a fire-and-forget task so
the user-facing response isn't delayed. M7 will move heavy distillation to the
nightly launchd job; M6 keeps it inline-async.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .db import connect
from .session import Session, runs_dir

CRITIC_MODEL = os.environ.get("AGENT_CRITIC_MODEL", "claude-sonnet-4-6")
MAX_TRANSCRIPT_CHARS = 40_000  # bound the critic's input cost


CRITIC_SYSTEM = """You are the post-hoc reviewer for a household agent. You read a session
transcript (the agent's tool calls, results, and final message) and identify drift,
not correctness of routine actions. You are looking for:

- redundancy: tool calls that repeat without new information
- context_ignored: pinned facts or recent memory that the agent failed to use
- fabrication: claims in the final message not supported by tool results or memory
- scope_leak: any reference to memory outside the speaker's visible scopes
- tool_misuse: wrong tool for the task, or tool called with bad inputs
- promise_made: agent committed to a future action ("I'll remind you tomorrow")
  that needs follow-through

Be conservative. If the session was clean, return an empty findings array.
Severity: 'info' for noticed-but-fine, 'warn' for should-fix patterns,
'error' for things that affected the user's outcome.

Return ONLY a JSON object of the form:
{"findings": [{"severity": "info|warn|error", "category": "...", "detail": "..."}]}
No prose outside the JSON."""


@dataclass
class Finding:
    severity: str
    category: str
    detail: str


def _read_transcript(session_id: str) -> str | None:
    """Locate today's jsonl for this session and return its contents (capped)."""
    base = runs_dir()
    if not base.exists():
        return None
    # Search recent days first.
    for day_dir in sorted(base.iterdir(), reverse=True):
        f = day_dir / f"{session_id}.jsonl"
        if f.exists():
            text = f.read_text(encoding="utf-8")
            return text[:MAX_TRANSCRIPT_CHARS]
    return None


def _audit_summary(session_id: str) -> str:
    with connect() as c:
        rows = c.execute(
            "SELECT seq, tool_name, tier, decision, decided_by, "
            "  substr(input_json, 1, 200) AS input_preview "
            "FROM tool_calls WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    if not rows:
        return "(no tool calls)"
    return "\n".join(
        f"{r['seq']:3} L{r['tier']} {r['decision']:18} by={r['decided_by']:14} "
        f"{r['tool_name']:42} input={r['input_preview']}"
        for r in rows
    )


def _save_findings(session_id: str, findings: list[Finding]) -> int:
    with connect() as c:
        for f in findings:
            c.execute(
                "INSERT INTO critic_findings(session_id, severity, category, detail) "
                "VALUES (?, ?, ?, ?)",
                (session_id, f.severity, f.category, f.detail),
            )
    return len(findings)


def _parse_findings(text: str) -> list[Finding]:
    # Strip code fences if the model wrapped the JSON.
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Salvage: find the first {...} block
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(s[start : end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []
    out: list[Finding] = []
    for item in obj.get("findings", []):
        sev = str(item.get("severity", "info")).lower()
        if sev not in ("info", "warn", "error"):
            sev = "info"
        out.append(
            Finding(
                severity=sev,
                category=str(item.get("category", "uncategorized"))[:64],
                detail=str(item.get("detail", ""))[:1000],
            )
        )
    return out


async def review(session_id: str, *, principal_role: str = "admin") -> list[Finding]:
    """Run the critic pass against a finished session. Returns findings (and
    persists them). Best-effort: returns [] on any failure rather than crashing."""
    transcript = _read_transcript(session_id) or "(transcript unavailable)"
    audit = _audit_summary(session_id)
    user_payload = (
        f"# Session {session_id}\n\n"
        f"## Audit (tool calls)\n{audit}\n\n"
        f"## Transcript (jsonl, may be truncated)\n{transcript}\n"
    )

    use_sub = os.environ.get("AGENT_USE_SUBSCRIPTION", "1") != "0"
    sdk_env = {"ANTHROPIC_API_KEY": ""} if use_sub else {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")
    }

    options = ClaudeAgentOptions(
        model=CRITIC_MODEL,
        system_prompt=CRITIC_SYSTEM,
        permission_mode="default",
        env=sdk_env,
    )

    text = ""
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_payload)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                elif isinstance(msg, ResultMessage):
                    if getattr(msg, "result", None):
                        text = msg.result
    except Exception as e:
        # Critic failures are non-fatal — log a single info finding and move on.
        text = json.dumps({
            "findings": [{
                "severity": "info",
                "category": "critic_error",
                "detail": f"critic pass failed: {type(e).__name__}: {e}",
            }]
        })

    findings = _parse_findings(text)
    if findings:
        _save_findings(session_id, findings)
    return findings


def schedule(session: Session) -> None:
    """Fire-and-forget critic pass. Does not block the response path.

    The caller's event loop owns the task; if the loop closes before the task
    completes (e.g. CLI exits immediately after run()), the critic is skipped.
    For background contexts (HTTP server, REPL) the task runs to completion.
    """
    try:
        loop_obj = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop_obj.create_task(_run_critic(session.id))


async def _run_critic(session_id: str) -> None:
    try:
        await review(session_id)
    except Exception:
        # Never propagate from the background task.
        pass


# ---------------------------------------------------------------------------
# Surfacing
# ---------------------------------------------------------------------------


def unseen_high_severity(limit: int = 3) -> list[dict]:
    """Most recent unseen warn/error findings, with their session info."""
    with connect() as c:
        rows = c.execute(
            """SELECT cf.id, cf.session_id, cf.severity, cf.category, cf.detail,
                      cf.created_at, s.task, s.principal
               FROM critic_findings cf
               JOIN sessions s ON s.id = cf.session_id
               WHERE cf.surfaced_at IS NULL AND cf.dismissed_at IS NULL
                 AND cf.severity IN ('warn','error')
               ORDER BY cf.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_surfaced(finding_ids: list[int]) -> None:
    if not finding_ids:
        return
    with connect() as c:
        c.execute(
            "UPDATE critic_findings SET surfaced_at = CURRENT_TIMESTAMP "
            "WHERE id IN ({})".format(",".join("?" * len(finding_ids))),
            finding_ids,
        )


def render_cautions_block(limit: int = 3) -> str:
    """Markdown block of recent unseen cautions to inject into the system prompt.
    Calling this is the surfacing event — matched ids are marked surfaced."""
    rows = unseen_high_severity(limit)
    if not rows:
        return ""
    mark_surfaced([r["id"] for r in rows])
    lines = ["## Recent cautions from the reviewer"]
    for r in rows:
        lines.append(
            f"- [{r['severity']}/{r['category']}] {r['detail']}  "
            f"(from session about {r['task'][:60]!r})"
        )
    lines.append(
        "Take these into account this turn. Address them if relevant; "
        "otherwise note them silently."
    )
    return "\n".join(lines)

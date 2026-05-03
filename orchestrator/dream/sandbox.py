"""M11.1 — filesystem sandbox.

A fresh sandbox dir per cycle, holding:
- A point-in-time copy of the production SQLite DB (via VACUUM INTO)
- A clone of the production repo at current HEAD
- A scratch dir for per-cycle artifacts before they're emitted to the queue

The sandbox is destroyed at end of cycle; nothing inside survives unless it
was explicitly copied to the dream-queue/ artifact directory.

Stage A (this file) gives process-level + filesystem-rooted isolation.
Stage B (M11.2) wraps the same shape in a Docker/OrbStack container with
a network-restricted bridge and read-only root.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.db import connect, db_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX_BASE = Path.home() / "agents-dream"
QUEUE_BASE = REPO_ROOT / "dream-queue"


@dataclass(frozen=True)
class Sandbox:
    kind: str           # 'filesystem' (M11.1) | 'container' (M11.2)
    cycle_id: str
    root: Path
    repo: Path
    db: Path
    artifacts: Path

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def spawn_filesystem_sandbox(cycle_id: str) -> Sandbox:
    """Create a fresh sandbox dir for this cycle. Idempotency: if the dir
    already exists, it is removed first (a stale half-built sandbox shouldn't
    block a new cycle)."""
    root = SANDBOX_BASE / cycle_id
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=False)

    # 1. Local clone of the repo at current HEAD. --local uses hardlinks
    #    where possible so the clone is fast and cheap on disk.
    repo_dest = root / "repo"
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(REPO_ROOT), str(repo_dest)],
        check=True,
        capture_output=True,
    )

    # 2. Consistent point-in-time copy of the production DB.
    db_dest = root / "data" / "agent.db"
    db_dest.parent.mkdir()
    with connect() as conn:
        # VACUUM INTO does not lock writers for long and produces a clean
        # standalone DB file with no WAL/SHM dependencies.
        conn.execute("VACUUM INTO ?", (str(db_dest),))

    # 3. Per-cycle artifact scratch (proposals, transcripts, reflection).
    artifacts = root / "artifacts"
    artifacts.mkdir()
    (artifacts / "proposals").mkdir()
    (artifacts / "rejections").mkdir()
    (artifacts / "transcripts").mkdir()

    return Sandbox(
        kind="filesystem",
        cycle_id=cycle_id,
        root=root,
        repo=repo_dest,
        db=db_dest,
        artifacts=artifacts,
    )


def emit_to_queue(sandbox: Sandbox) -> Path:
    """Copy the sandbox's artifacts directory to the production review queue.
    Returns the destination path. Cycle artifacts in the queue survive sandbox
    destruction; that's how proposals reach human review."""
    from datetime import datetime

    day = datetime.now().strftime("%Y-%m-%d")
    dest = QUEUE_BASE / day / sandbox.cycle_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sandbox.artifacts, dest)
    return dest

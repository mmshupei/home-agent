"""SQLite connection + schema bootstrap.

M3 wires sqlite-vec for vector similarity. SQLCipher + Keychain at-rest
encryption stays deferred (the [encrypted] extra is pinned but unused).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agent.db"


def db_path() -> Path:
    return Path(os.environ.get("AGENT_DB_PATH", DEFAULT_DB_PATH))


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load of sqlite-vec. False if extension or library missing."""
    try:
        conn.enable_load_extension(True)
    except AttributeError:
        return False
    try:
        import sqlite_vec  # noqa: WPS433 (intentional local import)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except AttributeError:
            pass


def connect(*, load_vec: bool = True) -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    if load_vec:
        _try_load_vec(conn)
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'adult', 'child')),
    imessage_handle TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used   TIMESTAMP,
    revoked_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    principal   TEXT NOT NULL,
    profile     TEXT NOT NULL,
    task        TEXT NOT NULL,
    started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP,
    final_msg   TEXT,
    token_count INTEGER,
    cost_usd    REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_principal ON sessions(principal, started_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    tool_name   TEXT NOT NULL,
    tier        INTEGER NOT NULL,
    input_json  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    decided_by  TEXT,
    result_json TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, seq);

-- M3: memory + events
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,           -- 'system' | 'family' | 'user:<user_id>'
    kind        TEXT NOT NULL CHECK (kind IN ('fact','preference','event','lesson')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by  TEXT NOT NULL,
    expires_at  TIMESTAMP,
    confidence  REAL DEFAULT 1.0,
    last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope);
CREATE INDEX IF NOT EXISTS idx_memory_kind  ON memory(scope, kind);

CREATE TABLE IF NOT EXISTS memory_archive (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP,
    created_by  TEXT NOT NULL,
    expires_at  TIMESTAMP,
    confidence  REAL,
    last_seen   TIMESTAMP,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    title       TEXT NOT NULL,
    starts_at   TIMESTAMP NOT NULL,
    ends_at     TIMESTAMP,
    location    TEXT,
    notes       TEXT,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(starts_at);
"""

# Vector table is a virtual table; only created if sqlite-vec loaded. Embedding
# dim matches voyage-3 (1024). Adjust here if switching providers.
EMBEDDING_DIM = 1024
VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    embedding float[{EMBEDDING_DIM}] distance_metric=cosine
);
"""


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.executescript(SCHEMA)
        # vec0 needs sqlite-vec loaded; connect(load_vec=True) handles that.
        try:
            conn.executescript(VEC_SCHEMA)
        except sqlite3.OperationalError:
            # extension wasn't loaded — memory still works, just without vec ANN
            pass
    finally:
        if own:
            conn.close()


def has_vec(conn: sqlite3.Connection) -> bool:
    """True if memory_vec virtual table exists in this connection."""
    try:
        conn.execute("SELECT 1 FROM memory_vec LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False

"""SQLite connection + schema bootstrap.

M1 uses plain sqlite3. SQLCipher + Keychain key derivation lands in M3 alongside
real memory storage, gated on the `encrypted` extra.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agent.db"


def db_path() -> Path:
    return Path(os.environ.get("AGENT_DB_PATH", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
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
"""


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        if own:
            conn.close()

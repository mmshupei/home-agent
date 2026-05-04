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
    role        TEXT NOT NULL CHECK (role IN ('admin', 'adult', 'child', 'device')),
    imessage_handle TEXT,
    telegram_user_id INTEGER UNIQUE,
    telegram_username TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One-shot link tokens for binding a Telegram chat to a user_id.
-- 5-minute TTL; consumed on first /start <token> call.
CREATE TABLE IF NOT EXISTS telegram_link_tokens (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMP
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
    -- M9 added 'schema', 'observation', 'question_pending' to the kind set.
    kind        TEXT NOT NULL CHECK (
        kind IN ('fact','preference','event','lesson',
                 'schema','observation','question_pending')
    ),
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

-- M6: post-hoc critic findings
CREATE TABLE IF NOT EXISTS critic_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    severity    TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
    category    TEXT NOT NULL,           -- e.g. 'redundancy','scope_leak','fabrication','tool_misuse','context_ignored'
    detail      TEXT NOT NULL,
    surfaced_at TIMESTAMP,                -- null until shown to a user
    dismissed_at TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_critic_unseen
    ON critic_findings(severity, surfaced_at) WHERE surfaced_at IS NULL;

-- M9: episodes — temporally bounded experiences (one per loop.run, one per
-- Reachy interaction in M10). Immutable after ended_at is set.
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,           -- 'cli'|'http'|'imessage'|'telegram'|'reachy'|'launchd'
    principal       TEXT,
    participants    TEXT,                    -- JSON array of user_ids
    started_at      TIMESTAMP NOT NULL,
    ended_at        TIMESTAMP,
    transcript      TEXT,
    audio_path      TEXT,
    affect          TEXT,                    -- JSON; source-specific shape
    summary         TEXT,
    embedding       BLOB,
    consolidated_at TIMESTAMP,
    consolidation_notes TEXT,
    session_id      TEXT REFERENCES sessions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_unconsolidated
    ON episodes(consolidated_at) WHERE consolidated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(started_at);
CREATE INDEX IF NOT EXISTS idx_episodes_source ON episodes(source);

-- M9: schema-kind memories link to their supporting evidence.
CREATE TABLE IF NOT EXISTS schema_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_id   INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    episode_id  INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    memory_id   INTEGER REFERENCES memory(id) ON DELETE SET NULL,
    weight      REAL DEFAULT 1.0,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_evidence_unique
    ON schema_evidence(schema_id, IFNULL(episode_id, -1), IFNULL(memory_id, -1));

-- M9: memory revisions — every change to a memory row's mutable fields,
-- so any night-cycle pass is fully revertible. revised_by uses these
-- conventions: 'night_cycle' | 'critic' | 'user:<id>' | 'memory_tool'.
CREATE TABLE IF NOT EXISTS memory_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    revised_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revised_by      TEXT NOT NULL,
    field           TEXT NOT NULL,           -- 'content'|'confidence'|'scope'|'archived'
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT,
    cycle_run_at    TIMESTAMP                -- night_cycles.ran_at, for revert-cycle
);
CREATE INDEX IF NOT EXISTS idx_revisions_cycle ON memory_revisions(cycle_run_at);
CREATE INDEX IF NOT EXISTS idx_revisions_memory ON memory_revisions(memory_id);

-- M9: per-cycle audit row.
CREATE TABLE IF NOT EXISTS night_cycles (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_s              INTEGER,
    episodes_replayed       INTEGER,
    commitments_extracted   INTEGER,
    schemas_proposed        INTEGER,
    observations_added      INTEGER,
    contradictions_found    INTEGER,
    questions_queued        INTEGER,
    archived_count          INTEGER,
    cost_usd                REAL,
    notes                   TEXT,
    raw_output              TEXT,
    status                  TEXT NOT NULL DEFAULT 'completed'
);
CREATE INDEX IF NOT EXISTS idx_night_cycles_ran ON night_cycles(ran_at);

-- Conversation threads. A thread is a multi-turn arc on a single surface
-- (Telegram, iMessage, etc.) for one principal. Ren can introspect the
-- current thread (status: when it started, turn count, time since last
-- activity) and decide to keep waiting in it OR compact it (write a
-- summary memory, mark closed) so the next turn starts fresh.
--
-- Constraint: at most ONE open thread per (surface, principal) at a time.
-- Closing one is the precondition for the next inbound creating a new one.
CREATE TABLE IF NOT EXISTS conversation_threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    surface         TEXT NOT NULL,           -- 'telegram'|'imessage'|'cli'|...
    principal       TEXT NOT NULL,
    started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at       TIMESTAMP,               -- null = still open
    closed_reason   TEXT,                    -- 'compacted'|'idle'|'manual'
    summary         TEXT,                    -- written on compact
    turn_count      INTEGER DEFAULT 0,
    last_turn_text  TEXT                     -- short tail of most recent user turn
);
-- Partial-unique on open rows: only one open thread per (surface, principal).
CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_one_open
    ON conversation_threads(surface, principal) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_threads_recent
    ON conversation_threads(surface, principal, last_active DESC);

-- Ren's mutable capability surface. A skill is a named, persistent prompt
-- Ren writes for themselves so they can recall a procedure later instead of
-- re-deriving it. Invocation = the agent reads the prompt from the tool
-- result and executes through normal tools (gate fires per constituent).
CREATE TABLE IF NOT EXISTS skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,    -- short identifier, e.g. 'evening_brief'
    description     TEXT NOT NULL,           -- one-line summary for skill__list
    prompt          TEXT NOT NULL,           -- the actual instructions Ren follows on invoke
    created_by      TEXT NOT NULL,           -- principal.user_id who created it (or 'ren')
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_invoked    TIMESTAMP,
    invoke_count    INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,       -- soft-delete via enabled=0
    notes           TEXT                     -- optional history / why-it-exists
);
CREATE INDEX IF NOT EXISTS idx_skills_enabled ON skills(enabled, name);

-- M9-adjacent: cross-process L3 approval queue. Agent process INSERTs
-- a pending row; Telegram bot UPDATEs to approved/denied via inline button
-- callback. Gate polls the row for state change. See orchestrator/approvals.py.
CREATE TABLE IF NOT EXISTS approval_requests (
    id              TEXT PRIMARY KEY,        -- random url-safe token
    user_id         TEXT NOT NULL,           -- principal asked
    tier            INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    summary         TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    requested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at      TIMESTAMP NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','approved','denied','expired')),
    decided_at      TIMESTAMP,
    decided_via     TEXT                     -- 'telegram'|'cli'|'pushover'|'timeout'
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending
    ON approval_requests(state, expires_at) WHERE state = 'pending';

-- M11: dreaming
CREATE TABLE IF NOT EXISTS dream_cycles (
    id                  TEXT PRIMARY KEY,
    started_at          TIMESTAMP NOT NULL,
    ended_at            TIMESTAMP,
    sandbox_kind        TEXT NOT NULL,
    proposals_emitted   INTEGER DEFAULT 0,
    rejections_logged   INTEGER DEFAULT 0,
    cost_usd            REAL,
    reflection_path     TEXT,
    status              TEXT NOT NULL CHECK (status IN ('running','completed','failed','aborted'))
);

CREATE TABLE IF NOT EXISTS proposals (
    id                  TEXT PRIMARY KEY,
    cycle_id            TEXT NOT NULL REFERENCES dream_cycles(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    title               TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    artifact_dir        TEXT NOT NULL,
    constraints_passed  BOOLEAN NOT NULL,
    tests_passed        BOOLEAN NOT NULL DEFAULT 0,
    state               TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','deferred','expired')),
    decided_at          TIMESTAMP,
    decided_by          TEXT,
    decision_reason     TEXT,
    applied_commit      TEXT,
    reverted_at         TIMESTAMP,
    revert_commit       TEXT,
    payload_json        TEXT                          -- structured per-kind detail
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state);
CREATE INDEX IF NOT EXISTS idx_proposals_cycle ON proposals(cycle_id);

CREATE TABLE IF NOT EXISTS constitution_rejections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            TEXT,
    declared_kind       TEXT,
    reason              TEXT NOT NULL,
    patch_summary       TEXT,
    layer               TEXT NOT NULL,                -- 'pre_implementer' | 'post_patch' | 'pre_apply'
    occurred_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Scheduler: pulse-driven scheduled actions. A 15-min heartbeat task in
-- triggers/telegram_bot.py scans this table; rows whose previous cron tick
-- (or run_once_at) is past AND whose last_fired_at < that previous tick are
-- handed off to a separate ClaudeSDKClient subagent (orchestrator/scheduler/
-- runner.py). Subagents post a short notification message to owner_user via
-- Telegram out-of-band; they do NOT submit into the user's resident.
--
-- Naming/shape mirrors `skills` for consistency: name UNIQUE, soft-delete via
-- enabled=0, created_by recorded honestly, rows survive disable.
CREATE TABLE IF NOT EXISTS scheduled_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    cron_expr       TEXT,                        -- 5-field cron, evaluated in `timezone`
    run_once_at     TIMESTAMP,                   -- mutually exclusive with cron_expr
    target_kind     TEXT NOT NULL CHECK (target_kind IN ('skill','prompt')),
    target          TEXT NOT NULL,               -- skill name OR free-form prompt text
    owner_user      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    timezone        TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    last_fired_at   TIMESTAMP,                   -- NULL = never fired
    last_status     TEXT,                        -- 'ok' | 'error' | NULL
    last_error      TEXT,
    last_cost_usd   REAL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by      TEXT NOT NULL,
    notes           TEXT,
    CHECK ((cron_expr IS NOT NULL) <> (run_once_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_actions_enabled
    ON scheduled_actions(enabled, name);
"""

# Vector table is a virtual table; only created if sqlite-vec loaded. Embedding
# dim matches voyage-3 (1024). Adjust here if switching providers.
EMBEDDING_DIM = 1024
VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    embedding float[{EMBEDDING_DIM}] distance_metric=cosine
);
"""


_MIGRATIONS = [
    # (column_check_table, column, alter_sql) — additive only, idempotent.
    ("users", "telegram_user_id", "ALTER TABLE users ADD COLUMN telegram_user_id INTEGER"),
    ("users", "telegram_username", "ALTER TABLE users ADD COLUMN telegram_username TEXT"),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, alter_sql in _MIGRATIONS:
        try:
            cols = _columns(conn, table)
        except sqlite3.OperationalError:
            continue  # table missing entirely; SCHEMA below will create it
        if column not in cols:
            conn.execute(alter_sql)
    _maybe_widen_memory_kind_check(conn)
    _maybe_widen_users_role_check(conn)
    _maybe_fix_dangling_users_fks(conn)


def _maybe_fix_dangling_users_fks(conn: sqlite3.Connection) -> None:
    """Tables created BEFORE a parent rename-rebuild may still have FKs pointing
    to <parent>_legacy. SQLite detects this lazily — the table works until
    something triggers FK enforcement. Scan every user table's SQL for the
    legacy suffix and rebuild it from the canonical CREATE statements below.

    Add new entries to FK_REBUILDS when a new table that REFERENCES users(id)
    or memory(id) is added.
    """
    legacy_suffixes = ("_legacy",)
    fk_rebuilds: dict[str, str] = {
        "telegram_link_tokens": """
            CREATE TABLE telegram_link_tokens (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                consumed_at TIMESTAMP
            )
        """,
        "schema_evidence": """
            CREATE TABLE schema_evidence (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_id   INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
                episode_id  INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
                memory_id   INTEGER REFERENCES memory(id) ON DELETE SET NULL,
                weight      REAL DEFAULT 1.0,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "memory_revisions": """
            CREATE TABLE memory_revisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id       INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
                revised_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revised_by      TEXT NOT NULL,
                field           TEXT NOT NULL,
                old_value       TEXT,
                new_value       TEXT,
                reason          TEXT,
                cycle_run_at    TIMESTAMP
            )
        """,
        "memory_archive": """
            CREATE TABLE memory_archive (
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
            )
        """,
    }

    for tbl, create_sql in fk_rebuilds.items():
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if not row:
            continue
        sql_text = row["sql"] or ""
        if not any(suf in sql_text for suf in legacy_suffixes):
            continue
        # The table has a stale FK reference; rebuild it. Discover its actual
        # column list (some columns added via ALTER) and copy into the canonical
        # shape, intersecting columns to avoid INSERT-with-mismatched-cols.
        cur_cols = [
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        ]
        # Parse new column list from create_sql by re-creating into a temp
        # table and reading PRAGMA. Cheaper: just trust that fk_rebuilds entries
        # match the actual column set.
        # (We don't have new columns added via ALTER on these tables.)
        col_list = ", ".join(cur_cols)
        conn.executescript(
            f"""
            PRAGMA foreign_keys = OFF;
            BEGIN;
            ALTER TABLE {tbl} RENAME TO {tbl}_dangling;
            {create_sql};
            INSERT INTO {tbl}({col_list})
                SELECT {col_list} FROM {tbl}_dangling;
            DROP TABLE {tbl}_dangling;
            COMMIT;
            PRAGMA foreign_keys = ON;
            """
        )


def _maybe_widen_users_role_check(conn: sqlite3.Connection) -> None:
    """M10: add 'device' to the users.role CHECK constraint. Same pattern as
    the memory.kind widening — rename, recreate, copy. Idempotent."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row or "device" in (row["sql"] or ""):
        return

    # Also rebuild dependent tokens table so its FK targets the new users
    # table (SQLite ties FK references to the named table at FK-resolution
    # time; renaming users would leave tokens.user_id REFERENCES users_legacy).
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        BEGIN;
        ALTER TABLE users RENAME TO users_legacy;
        CREATE TABLE users (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('admin','adult','child','device')),
            imessage_handle TEXT,
            telegram_user_id INTEGER UNIQUE,
            telegram_username TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users
            (id, name, role, imessage_handle, telegram_user_id,
             telegram_username, created_at)
        SELECT id, name, role, imessage_handle, telegram_user_id,
               telegram_username, created_at
        FROM users_legacy;
        ALTER TABLE tokens RENAME TO tokens_legacy;
        CREATE TABLE tokens (
            token_hash  TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used   TIMESTAMP,
            revoked_at  TIMESTAMP
        );
        INSERT INTO tokens
            (token_hash, user_id, label, created_at, last_used, revoked_at)
        SELECT token_hash, user_id, label, created_at, last_used, revoked_at
        FROM tokens_legacy;
        CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
        DROP TABLE tokens_legacy;
        ALTER TABLE telegram_link_tokens RENAME TO telegram_link_tokens_legacy;
        CREATE TABLE telegram_link_tokens (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consumed_at TIMESTAMP
        );
        INSERT INTO telegram_link_tokens(token, user_id, created_at, consumed_at)
        SELECT token, user_id, created_at, consumed_at FROM telegram_link_tokens_legacy;
        DROP TABLE telegram_link_tokens_legacy;
        DROP TABLE users_legacy;
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )


def _maybe_widen_memory_kind_check(conn: sqlite3.Connection) -> None:
    """M9: relax the memory.kind CHECK constraint to include schema /
    observation / question_pending. SQLite can't alter a CHECK in place,
    so we rename, recreate, copy, drop. Idempotent — only runs if the
    existing CHECK lacks 'schema'."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if not row or "schema" in (row["sql"] or ""):
        return  # table missing or already widened

    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        BEGIN;
        ALTER TABLE memory RENAME TO memory_legacy;
        CREATE TABLE memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (
                kind IN ('fact','preference','event','lesson',
                         'schema','observation','question_pending')
            ),
            content     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by  TEXT NOT NULL,
            expires_at  TIMESTAMP,
            confidence  REAL DEFAULT 1.0,
            last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO memory(id, scope, kind, content, created_at, created_by,
                           expires_at, confidence, last_seen)
        SELECT id, scope, kind, content, created_at, created_by,
               expires_at, confidence, last_seen FROM memory_legacy;
        DROP TABLE memory_legacy;
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
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

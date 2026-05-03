# Family Agent — Design & Implementation Handoff

**Status:** Design complete, ready to build
**Host:** macOS (Apple Silicon), single Mac as the household agent host
**Primary users:** Shupei (admin), Yunyan (adult), Yi'an (child, deferred)
**Implementer:** Claude Code

---

## 1. Goals & Non-Goals

### Goals

A personal/family agent running on a household Mac, accessible from iPhones over Tailscale, with:

- Home automation (via Home Assistant if present, AppleScript otherwise)
- Financial analysis (read-only by default; trades are L3 and stay manual for now)
- Chores (Reminders, Calendar, Messages, Mail via AppleScript)
- Browser support (Playwright MCP, computer-use only when vision is required)
- Per-person identity, scoped memory, audit trail
- Push-based approval flow for sensitive actions

### Non-Goals (intentionally)

- No custom iOS app. Shortcuts + iMessage covers the surface area.
- No SaaS dependency for the core loop. Anthropic API is the only required external service.
- No long-running daemon. Spawn-per-task is the execution model.
- No multi-host orchestration. One Mac, one agent.
- No auto-trading. Finance is observational; orders stay human-issued.

---

## 2. Architectural Principles

These three drive the design. Re-read them when in doubt.

**Spawn-per-task, not daemon.** Every invocation is a fresh process with bounded scope. Easier to reason about, trivial to kill, no in-memory state corruption. Warm context lives in SQLite.

**Profiles, not per-tool config.** A tool's tier is fixed (locking the door is always L3). The *profile* decides how that tier is enforced — unattended runs deny L2+, interactive runs prompt the terminal, mobile triggers push to the phone. This collapses a 2D matrix to one configurable axis.

**Critic post-hoc, not pre-action.** Pre-action critics double latency and fight the main loop. Post-hoc Sonnet review of the transcript catches drift without slowing the user-facing path. Tag findings, surface high-severity at next invocation.

---

## 3. Repository Layout

```
~/agents/
├── pyproject.toml                # uv-managed; Python 3.12+
├── .env                          # ANTHROPIC_API_KEY, HA_TOKEN, PUSHOVER_*, AUTH_HMAC_KEY
├── .gitignore                    # .env, runs/, *.db, *.db-journal
├── README.md
│
├── config/
│   ├── profiles.toml             # unattended / interactive / home / mobile
│   ├── tiers.toml                # tool-name regex → L1/L2/L3
│   ├── mcp.json                  # MCP server registry
│   ├── roles.toml                # admin / adult / child capability matrix
│   └── prompts/
│       ├── base.md               # always-included system prompt
│       └── domains/
│           ├── home.md
│           ├── finance.md
│           ├── chores.md
│           └── browse.md
│
├── orchestrator/
│   ├── __init__.py
│   ├── loop.py                   # run() entrypoint
│   ├── gating.py                 # PreToolUse hook + tier classifier
│   ├── critic.py                 # post-hoc Sonnet reviewer
│   ├── session.py                # SQLite session log + transcript
│   ├── memory.py                 # scoped memory store + retrieval
│   └── auth.py                   # token verification + principal resolution
│
├── tools/
│   ├── applescript/              # reminders, calendar, messages, mail shims
│   │   ├── reminders.py
│   │   ├── calendar.py
│   │   └── messages.py
│   ├── finance/                  # AAPL lots, RSU vesting, tax notes
│   │   ├── lots.py
│   │   └── analysis.py
│   └── sdk_mcp/                  # @tool-decorated curated routines
│       ├── home.py               # good_night, leaving_home, ...
│       └── household.py          # add_family_event, set_preference, ...
│
├── triggers/
│   ├── http.py                   # FastAPI on 127.0.0.1:8765
│   ├── imessage_relay.py         # watches chat.db, routes inbound
│   ├── launchd/
│   │   ├── morning_brief.plist
│   │   ├── memory_prune.plist
│   │   └── imessage_relay.plist
│   └── menubar.lua               # Hammerspoon hotkey
│
├── admin/
│   ├── cli.py                    # token issue/revoke, memory inspect, audit
│   └── seed.py                   # initial DB schema + seed family
│
├── data/
│   ├── agent.db                  # SQLite; SQLCipher-encrypted at rest
│   └── runs/                     # per-invocation jsonl logs
│
└── bin/
    └── agent                     # CLI entrypoint (chmod +x)
```

---

## 4. Dependencies

```toml
# pyproject.toml essentials
[project]
requires-python = ">=3.12"
dependencies = [
    "claude-agent-sdk>=0.1",
    "anthropic>=0.40",
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "pydantic>=2.6",
    "sqlcipher3-binary>=0.5",
    "sqlite-vec>=0.1",
    "tomli-w>=1.0",
    "httpx>=0.27",
    "python-dotenv>=1.0",
    "rich>=13.7",
]
```

Verify current Claude Agent SDK version and API at https://docs.claude.com/en/api/agent-sdk before pinning. Use `claude-opus-4-7` as the primary model for the orchestrator and `claude-sonnet-4-6` for the critic and memory distillation passes.

External services: Tailscale (free), Pushover (~$5 one-time), Home Assistant (optional, local).

---

## 5. Auth & Identity

### Token format

```
agent_<user_id>_<random_24_bytes_b64>
```

Verified by HMAC-SHA256 against `AUTH_HMAC_KEY` (32 bytes, in `.env`, generated once with `secrets.token_bytes(32)`).

### Schema

```sql
CREATE TABLE users (
    id          TEXT PRIMARY KEY,        -- 'shupei', 'yunyan', 'yian'
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,           -- 'admin' | 'adult' | 'child'
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tokens (
    token_hash  TEXT PRIMARY KEY,        -- HMAC of token
    user_id     TEXT NOT NULL REFERENCES users(id),
    label       TEXT,                    -- 'shupei-iphone', 'yunyan-shortcuts'
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used   TIMESTAMP,
    revoked_at  TIMESTAMP
);
```

Tokens are issued by `admin/cli.py issue-token <user_id> <label>`, displayed once, never stored in plaintext. The hash is what lives in the DB.

### Roles

```toml
# config/roles.toml
[admin]
default_profile = "interactive"
allowed_profiles = ["interactive", "home", "mobile", "unattended"]
max_tier = 3
can_promote_memory = true
can_read_all_scopes = true

[adult]
default_profile = "mobile"
allowed_profiles = ["interactive", "home", "mobile"]
max_tier = 3
can_promote_memory = true
can_read_all_scopes = false   # only family + own

[child]
default_profile = "mobile"
allowed_profiles = ["mobile"]
max_tier = 1                  # read-only by default
can_promote_memory = false
can_read_all_scopes = false
```

`max_tier` is enforced by the gate as a hard ceiling regardless of profile policy.

### Verification

```python
# orchestrator/auth.py
import hmac, hashlib, os
from dataclasses import dataclass

KEY = os.environ["AUTH_HMAC_KEY"].encode()

@dataclass
class Principal:
    user_id: str
    role: str
    token_label: str

def hash_token(token: str) -> str:
    return hmac.new(KEY, token.encode(), hashlib.sha256).hexdigest()

def verify(token: str, db) -> Principal | None:
    row = db.execute(
        "SELECT t.user_id, t.label, u.role FROM tokens t "
        "JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
    if not row:
        return None
    db.execute("UPDATE tokens SET last_used = CURRENT_TIMESTAMP WHERE token_hash = ?",
               (hash_token(token),))
    return Principal(user_id=row[0], token_label=row[1], role=row[2])
```

### Tailscale as second factor

The FastAPI server binds `127.0.0.1` only. Tailscale ACLs restrict the agent port to family devices on the tailnet. A leaked token without tailnet access is useless.

---

## 6. The Orchestrator Loop

```python
# orchestrator/loop.py
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from .gating import build_gate_hook
from .session import Session
from .memory import retrieve_context
from .critic import schedule_critic
from .auth import Principal

async def run(task: str, principal: Principal, profile: str) -> str:
    session = Session.new(task=task, profile=profile, principal=principal)
    cfg = load_profile(profile, principal.role)

    # Pull scoped memory into the system prompt
    context_block = retrieve_context(task, principal, top_k=12)

    options = ClaudeAgentOptions(
        system_prompt=compose_prompt(cfg.domains, context_block, principal),
        mcp_servers=cfg.mcp_servers,
        allowed_tools=cfg.allowed_tools,
        model="claude-opus-4-7",
        hooks={
            "PreToolUse":  [build_gate_hook(session, cfg, principal)],
            "PostToolUse": [session.log_tool_result],
            "Stop":        [session.finalize],
        },
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(task)
        async for msg in client.receive_response():
            session.record(msg)

    schedule_critic(session)
    return session.final_message
```

`compose_prompt` injects:
- `base.md`
- domain prompts (`home.md`, etc.) selected by `cfg.domains`
- the principal's identity: `"You are speaking with {principal.name}. Respect their scope."`
- the retrieved memory block, capped at ~2k tokens
- the current date/time in Pacific time

---

## 7. Gating

### Tier rules

```python
# orchestrator/gating.py
TIER_RULES = [
    # L1 — read-only, ambient
    (r"^(filesystem__read|fetch|search|.*__get_|.*__list_|.*__search_)", 1),
    (r"^playwright__(navigate|screenshot|extract|read_)",                 1),
    (r"^ha__(get_state|list_)",                                           1),
    (r"^memory__read",                                                    1),

    # L2 — local side-effects, reversible
    (r"^applescript__reminders__(add|complete|update)",                   2),
    (r"^applescript__calendar__add",                                      2),
    (r"^ha__call_service__(light|switch|scene|media_player)",             2),
    (r"^playwright__(click|fill|select)",                                 2),
    (r"^filesystem__write",                                               2),
    (r"^memory__write",                                                   2),

    # L3 — external, irreversible, or physical
    (r"^ha__call_service__(lock|cover|alarm|garage)",                     3),
    (r"^applescript__messages__send",                                     3),
    (r"^applescript__mail__send",                                         3),
    (r"^finance__(place_order|transfer|sell)",                            3),
    (r"^playwright__(submit|confirm|purchase|pay)",                       3),
    (r"^memory__delete",                                                  3),
]

def classify(tool: str) -> int:
    for pattern, tier in TIER_RULES:
        if re.match(pattern, tool):
            return tier
    return 2   # unknown → conservative
```

### Hook

```python
def build_gate_hook(session, cfg, principal):
    async def gate(input, tool_use_id, ctx):
        tier = classify(input["tool_name"])
        session.log_intent(input, tier, principal)

        # Hard ceiling from role
        max_tier = ROLE_MAX_TIER[principal.role]
        if tier > max_tier:
            return deny(f"Tier {tier} exceeds {principal.role} ceiling of {max_tier}")

        action = cfg.policy[tier]   # 'allow' | 'prompt_cli' | 'prompt_push' | 'deny'

        match action:
            case "allow":
                return {}
            case "deny":
                return deny(f"L{tier} blocked under {cfg.name}")
            case "prompt_cli":
                ok = await prompt_terminal(input, tier)
            case "prompt_push":
                ok = await prompt_pushover(input, tier, principal)

        return {} if ok else deny("User denied")

    return gate

def deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
```

### Profiles

```toml
# config/profiles.toml
[unattended]
description = "launchd-scheduled jobs"
policy = { 1 = "allow", 2 = "deny",       3 = "deny" }
domains = ["chores", "finance"]

[interactive]
description = "Terminal CLI"
policy = { 1 = "allow", 2 = "prompt_cli", 3 = "prompt_cli" }
domains = ["home", "finance", "chores", "browse"]

[home]
description = "Hammerspoon hotkey at desk"
policy = { 1 = "allow", 2 = "allow",      3 = "prompt_cli" }
domains = ["home", "chores", "browse"]

[mobile]
description = "iOS Shortcuts via Tailscale"
policy = { 1 = "allow", 2 = "prompt_push", 3 = "prompt_push" }
domains = ["home", "chores", "browse"]
```

---

## 8. Memory

### Schema

```sql
CREATE TABLE memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,           -- 'user:shupei' | 'family' | 'system'
    kind        TEXT NOT NULL,           -- 'fact' | 'preference' | 'event' | 'lesson'
    content     TEXT NOT NULL,
    embedding   BLOB,                    -- sqlite-vec
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by  TEXT NOT NULL,           -- principal.user_id
    expires_at  TIMESTAMP,
    confidence  REAL DEFAULT 1.0,
    last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_memory_scope ON memory(scope);
CREATE INDEX idx_memory_kind  ON memory(scope, kind);

-- Structured events table — precise time queries
CREATE TABLE events (
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
CREATE INDEX idx_events_time ON events(starts_at);
```

### Scope visibility

A query from principal P sees:
- `scope = 'system'`
- `scope = 'family'`
- `scope = 'user:' || P.user_id`

Admin role additionally sees all `user:*` scopes (for debugging; logged as a privileged read).

### Retrieval

```python
# orchestrator/memory.py
def retrieve_context(task: str, principal: Principal, top_k: int = 12) -> str:
    visible = visible_scopes(principal)
    embedding = embed(task)   # use voyage-3 or anthropic-embed equivalent

    rows = db.execute("""
        SELECT content, kind, scope
        FROM memory
        WHERE scope IN ({})
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
          AND confidence > 0.3
        ORDER BY vec_distance_cosine(embedding, ?) ASC
        LIMIT ?
    """.format(",".join("?" * len(visible))), (*visible, embedding, top_k)).fetchall()

    pinned = db.execute(
        "SELECT content FROM memory WHERE kind = 'fact' AND confidence = 1.0 "
        "AND scope IN ('system', 'family') ORDER BY created_at DESC LIMIT 8"
    ).fetchall()

    upcoming = db.execute(
        "SELECT title, starts_at FROM events "
        "WHERE scope IN ({}) AND starts_at BETWEEN datetime('now') AND datetime('now', '+7 days') "
        "ORDER BY starts_at".format(",".join("?" * len(visible))),
        visible
    ).fetchall()

    return format_context_block(pinned, rows, upcoming)
```

### Writes

Two paths:

1. **Explicit**: `memory__write` tool, called by the agent when the user says "remember that...". Confidence 1.0. Writes to user scope by default; promotion to family/system requires `can_promote_memory` and is a separate L2 action.

2. **Distilled**: critic's `Stop` hook runs Sonnet over the transcript with a structured-extraction prompt:

   ```
   Extract durable facts from this session. For each:
   - content: one sentence, third-person
   - kind: fact | preference | event | lesson
   - scope: user:{id} | family | system
   - confidence: 0.5–0.8
   - expires_at: ISO datetime or null
   Return JSON array. Skip ephemeral context (today's weather, etc.).
   ```

   Distilled memories enter at confidence ≤0.8 to distinguish them from explicit user statements.

### Forgetting

Nightly cron (`memory_prune.plist`):

```sql
-- Hard expiry
DELETE FROM memory WHERE expires_at < CURRENT_TIMESTAMP;
DELETE FROM events WHERE ends_at < datetime('now', '-30 days');

-- Confidence decay (5%/month untouched)
UPDATE memory
SET confidence = confidence * 0.95
WHERE last_seen < datetime('now', '-30 days')
  AND kind != 'fact';

-- Archive low-confidence
INSERT INTO memory_archive SELECT * FROM memory WHERE confidence < 0.3;
DELETE FROM memory WHERE confidence < 0.3;
```

Explicit forgetting: `memory__forget` tool, soft-deletes by setting `confidence = 0`, audit-logged. "Forget that conversation" runs over the most recent session's writes.

### Contradictions

When a new fact contradicts an existing one (semantic similarity > 0.9 but content differs), the agent does *not* silently overwrite. It emits a `contradiction_pending` record and surfaces at next interaction: "I had it that Yi'an's school is X — has that changed?"

---

## 9. Sessions & Audit

```sql
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,        -- uuid
    principal   TEXT NOT NULL,
    profile     TEXT NOT NULL,
    task        TEXT NOT NULL,
    started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP,
    final_msg   TEXT,
    token_count INTEGER,
    cost_usd    REAL
);

CREATE TABLE tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    tool_name   TEXT NOT NULL,
    tier        INTEGER NOT NULL,
    input_json  TEXT NOT NULL,
    decision    TEXT NOT NULL,           -- 'allow' | 'deny' | 'allow_after_prompt'
    decided_by  TEXT,                    -- 'auto' | 'cli' | 'push'
    result_json TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE critic_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    severity    TEXT NOT NULL,           -- 'info' | 'warn' | 'error'
    category    TEXT NOT NULL,
    detail      TEXT NOT NULL,
    surfaced_at TIMESTAMP                -- null until shown to user
);
```

Per-invocation jsonl in `data/runs/{date}/{session_id}.jsonl` mirrors Val Agent's format for benchmarking compatibility.

Retention: 90 days hot, archived to compressed `runs/archive/` after that. Critic findings retained 1 year.

---

## 10. Triggers

### FastAPI HTTP

```python
# triggers/http.py
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from orchestrator import auth, loop

app = FastAPI()

class RunRequest(BaseModel):
    task: str
    profile_hint: str | None = None

@app.post("/run")
async def run(req: RunRequest, x_agent_token: str = Header(...)):
    principal = auth.verify(x_agent_token, db())
    if not principal:
        raise HTTPException(401, "invalid token")

    profile = resolve_profile(principal, req.profile_hint)
    response = await loop.run(req.task, principal=principal, profile=profile)
    return {"response": response}

@app.get("/health")
async def health():
    return {"ok": True}
```

Run via launchd, bound to `127.0.0.1:8765`. Tailscale serves it to the tailnet.

### iOS Shortcut spec

Each family member installs a Shortcut named "Ask Agent":

1. Ask for input (text)
2. Get Contents of URL: `POST http://mac.tailnet.ts.net:8765/run`
3. Headers: `X-Agent-Token: agent_<their_token>`, `Content-Type: application/json`
4. Body: `{"task": <input>, "profile_hint": "mobile"}`
5. Show Result (response.response)

Token lives in a Note inside the Shortcut, not exposed elsewhere. Add to Action Button or Back Tap.

A second Shortcut "Tell Family" writes directly to family-scope memory via `POST /memory/family` — bypasses the agent loop for cheap inputs like "cleaners coming Thursday."

### iMessage relay

```python
# triggers/imessage_relay.py — runs under launchd, requires Full Disk Access
# Watches ~/Library/Messages/chat.db for new messages from known family numbers,
# routes them through the orchestrator, replies via AppleScript.
```

Sketch:
- Poll `chat.db` every 3 seconds for new rows since last seen `ROWID`
- Match sender phone/Apple ID against `users.imessage_handle` column (add to schema)
- Resolve to principal, run `loop.run()`, reply via AppleScript
- Maintain conversation thread context: pass last N exchanges from same handle as prior turns

This is the killer feature for non-CLI family members. Conversational, persistent, no app to install.

### launchd jobs

```xml
<!-- triggers/launchd/morning_brief.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>com.shupei.agent.morning_brief</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/shupei/agents/bin/agent</string>
        <string>--profile</string><string>unattended</string>
        <string>--principal</string><string>shupei</string>
        <string>--task</string>
        <string>Morning brief: calendar today, AAPL movement overnight, Hormuz Watch deltas, family events this week.</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>/Users/shupei/agents/data/runs/launchd.log</string>
    <key>StandardErrorPath</key><string>/Users/shupei/agents/data/runs/launchd.err</string>
</dict>
</plist>
```

Add `<key>EnvironmentVariables</key>` block to inject `.env` since launchd doesn't read it.

The Mac must be awake at 7am — add `caffeinate -s` wrapper or `pmset` schedule.

---

## 11. Push Approval (Pushover)

```python
async def prompt_pushover(tool_input, tier, principal) -> bool:
    # Route to the principal's own device, not a global channel
    user_key = PUSHOVER_KEYS[principal.user_id]

    receipt = await pushover.send(
        user=user_key,
        message=f"L{tier}: {tool_input['tool_name']}\n{summarize(tool_input)}",
        title=f"Agent — approve?",
        priority=2 if tier == 3 else 1,
        retry=30, expire=300,
        # Pushover supports inline reply on iOS via "reply" priority 2 ack
    )
    return await receipt.wait(timeout=60, default=False)
```

Priority 2 (emergency) for L3 — bypasses Do Not Disturb, requires explicit ack.

`summarize(tool_input)` is a 1–2 line human description, not the raw JSON. The agent generates this as part of the tool call (add a `purpose` field to the input schema for L2/L3 tools, mandatory).

---

## 12. Custom MCP Tools (Curated Routines)

```python
# tools/sdk_mcp/home.py
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool(
    "good_night",
    "Bedtime routine: lock front door, lights off, thermostat to 68°F.",
    {}
)
async def good_night(args):
    await ha.call("lock.front_door", "lock")
    await ha.call("light.all", "turn_off")
    await ha.call("climate.main", "set_temperature", temperature=68)
    return {"content": [{"type": "text", "text": "Locked, dark, 68°F. Sleep well."}]}

@tool(
    "leaving_home",
    "Departure routine: lights off, thermostat to away mode, lock front door.",
    {}
)
async def leaving_home(args):
    await ha.call("light.all", "turn_off")
    await ha.call("climate.main", "set_preset_mode", preset_mode="away")
    await ha.call("lock.front_door", "lock")
    return {"content": [{"type": "text", "text": "House secured, climate to away."}]}

home_server = create_sdk_mcp_server("home", "1.0", tools=[good_night, leaving_home])
```

Curated routines compose multiple raw HA calls under one tool. The gate prompts once for the routine, not per-action. Raw `ha__call_service` stays available for novel requests but isn't the primary surface.

Same pattern for finance: `aapl_lot_status` (read-only L1, returns the cost-basis ledger), `vest_calendar` (L1, upcoming RSU vests), no `place_order` for now.

---

## 13. Encryption at Rest

`data/agent.db` uses SQLCipher. Key derived from a passphrase stored in macOS Keychain:

```python
import sqlcipher3 as sqlite3, keyring

def open_db():
    key = keyring.get_password("agent", "db_key")
    conn = sqlite3.connect("data/agent.db")
    conn.execute(f"PRAGMA key = '{key}'")
    return conn
```

Initial setup (`admin/seed.py`) generates the key and stores it via `keyring.set_password`. Backups must include the keychain entry or the DB is unrecoverable — document this in the README.

`runs/*.jsonl` are not encrypted (volume too high, content less sensitive). They live in the same encrypted volume the user's home folder is on (FileVault assumed).

---

## 14. Build Sequence

Each milestone has a Definition of Done. Don't move forward until DoD is met.

### M1 — Spine (≈1.5 days)

- `pyproject.toml`, `.env.example`, repo layout
- `admin/seed.py` creates DB, schema, initial admin user, first token
- `orchestrator/auth.py` verify + Principal
- `orchestrator/loop.py` minimal: SDK client, system prompt, no memory yet
- `triggers/http.py` with `/run` and `/health`
- CLI `bin/agent --task "..."`
- Filesystem MCP + Playwright MCP wired

**DoD:** From terminal, `bin/agent --task "summarize the README in this repo"` works. From another machine on the tailnet, `curl -H "X-Agent-Token: ..." -d '{"task":"..."}' http://mac.tailnet.ts.net:8765/run` works. Auth rejects bad tokens.

### M2 — Gating (≈1 day)

- `orchestrator/gating.py` with TIER_RULES and hook
- `config/profiles.toml`, `config/roles.toml`
- `prompt_terminal` (rich-based prompt with diff-style preview)
- Pushover account, `prompt_pushover` stub (logs to console, returns True)

**DoD:** Running with `--profile unattended` denies any L2 tool call. Running `--profile interactive` prompts via terminal and respects the answer. Audit log records every decision.

### M3 — Memory (≈2 days)

- Schema migration: `memory`, `events`, `memory_archive`
- `orchestrator/memory.py` retrieve_context with scope filtering
- Embedding via Voyage or Anthropic embed API; sqlite-vec integration
- `memory__write`, `memory__read`, `memory__forget` tools
- Distillation pass in critic on `Stop`
- `admin/cli.py memory inspect|search|prune`

**DoD:** "Remember that Yi'an's pediatrician is Dr. Chen" writes to user scope. Next session, asking "who's Yi'an's pediatrician?" pulls it from context. "Forget that" works. Family scope is visible to other adult users; user scope is not.

### M4 — Real tools (≈2 days)

- AppleScript shims (Reminders, Calendar, Messages)
- Home Assistant MCP integration if HA is running, else skip
- Finance read-only tools (read existing AAPL lots ledger from CSV/JSON)
- Curated MCP routines: `good_night`, `leaving_home`, `morning_brief`

**DoD:** "Add 'pick up Yi'an at 4pm' to Reminders" works end-to-end. "What's on my calendar tomorrow?" returns real events. If HA: "turn off the bedroom lights" works.

### M5 — Push & remote (≈1 day)

- Pushover real implementation with reply receipts
- `prompt_push` polling with 60s timeout
- iOS Shortcut JSON spec written into README
- Yunyan's token issued + her Shortcut tested

**DoD:** Yunyan invokes the agent from her phone over Tailscale. An L2 action produces a Pushover prompt on her phone with an Approve/Deny button; the gate respects her answer.

### M6 — Critic & lessons (≈1 day)

- `orchestrator/critic.py` schedule + Sonnet review
- `critic_findings` written; high-severity surfaced at next session start
- `admin/cli.py critic recent|surface|dismiss`

**DoD:** A session with a notable issue (e.g., redundant tool calls, ignored context) produces a finding. Next session loads the recent findings into the system prompt as cautions.

### M7 — Schedules (≈1 day)

- `morning_brief.plist`, `memory_prune.plist`
- Mac sleep handling (`caffeinate` or `pmset`)
- Output of unattended runs delivered via iMessage to admin

**DoD:** 7am morning brief lands on Shupei's phone via iMessage every weekday. Memory prune runs nightly without errors.

### M8 — iMessage relay (≈1.5 days)

- `triggers/imessage_relay.py` watching chat.db
- launchd plist with Full Disk Access
- Per-handle conversation context (last N turns)
- Family member numbers added to users table

**DoD:** Yunyan texts the agent from Messages, gets a reply in the same thread. Conversation context persists across messages within a 30-minute window.

### Deferred

- Yi'an's child role and access — only after a few months of adult use
- Encrypted backup to external drive
- Web UI for audit log inspection (CLI is sufficient for now)
- Cross-device session continuity (iPad, Watch)
- Vector DB upgrade to LanceDB if SQLite-vec hits scaling limits

---

## 15. Validation & Testing

For each milestone, write three kinds of tests:

1. **Unit:** auth.verify, classify, scope visibility, token hashing
2. **Integration:** end-to-end `loop.run()` against a recorded transcript fixture, asserting tool sequence and gate decisions
3. **Adversarial:** the L2 tool that tries to escalate, the unattended job that asks for L3, the cross-scope memory read

Adversarial cases worth including from day one:

- Token from one user passed with `principal_hint=other_user` — must reject
- L3 tool called under unattended profile — must deny without prompting
- Memory write to family scope by child role — must deny
- Tool name not matching any tier rule — must default to L2 (conservative)
- Pushover timeout — must default to deny
- Long-running tool (>5min) — must time out gracefully
- Critic finding the agent fabricated a memory write — must surface at next session

---

## 16. Open Questions / Decisions Deferred

These don't block M1–M5; revisit during M6+:

- **Shared context for collaborative tasks.** If Shupei and Yunyan both invoke the agent on the same topic ("plan Yi'an's birthday"), should there be an explicit shared session? Or is family-scope memory enough? Default to memory-only until a real use case appears.
- **Cost ceiling.** Per-user daily token budget enforced at the gate. Implement once usage data exists; pre-optimizing is wasteful.
- **Voice via Siri Shortcuts.** Trivially works through the Shortcut, but TTS of the agent's reply requires more work. Possibly use macOS `say` and AirPlay back, or stick with text replies via Messages.
- **Conflict between explicit and distilled memory.** If the critic distills "user prefers concise replies" but the user explicitly wrote "remember I want detailed explanations," explicit wins by confidence. Verify this resolves correctly.
- **Web search inside the agent.** Anthropic's web_search tool vs Brave/Tavily MCP. Default to Anthropic's for simplicity; switch if quality issues emerge.

---

## 17. Notes for the Implementer

- **Don't over-engineer the gate.** Forty lines of policy is harder to get wrong than a thousand lines of cleverness. Resist abstractions.
- **Preserve transcript format.** `runs/*.jsonl` should match Val Agent's existing schema so benchmarking infrastructure carries over.
- **Verify Claude Agent SDK details.** This doc assumes the SDK exposes `ClaudeSDKClient`, `ClaudeAgentOptions`, hook events `PreToolUse`/`PostToolUse`/`Stop`, and `create_sdk_mcp_server`. Confirm against current docs at https://docs.claude.com/en/api/agent-sdk before implementing.
- **macOS specifics.** Full Disk Access for the iMessage watcher must be granted manually in System Settings → Privacy & Security. Document this in the README; it cannot be automated.
- **Treat the README as a runbook.** Setup steps, token issuance, backup procedure, recovery from lost keychain entry. Write it as you build, not at the end.
- **No premature web UI.** CLI + Shortcuts + iMessage covers everything. Adding a UI is a six-week distraction; the working system is the goal.
- **Ship M1 to M5 before adding any new capability.** The temptation to add features mid-build is strong. Resist. The shape of the system reveals itself only when real use begins.

---

*End of handoff.*

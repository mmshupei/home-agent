# Family Agent

Per-person agent on a household Mac. iPhone access over Tailscale. Spawn-per-task,
profile-driven gating, scoped memory.

See [`family_agent_design.md`](family_agent_design.md) for the full design.

## Status

| Milestone | Description | Status |
|-----------|-------------|--------|
| M1 | Spine: SDK loop, auth, HTTP, CLI | in progress |
| M2 | Tier gating + profiles | pending |
| M3 | Memory (sqlite-vec) | pending |
| M4 | Real tools (AppleScript, HA) | pending |
| M5 | Push & remote (Pushover, iOS Shortcut) | pending |
| M6 | Critic & lessons | pending |
| M7 | launchd schedules | pending |
| M8 | iMessage relay | pending |

## Setup (M1)

```bash
# 1. Install uv if needed: https://docs.astral.sh/uv/
uv sync

# 2. Configure secrets
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))" >> /tmp/key && \
  sed -i '' "s|^AUTH_HMAC_KEY=.*|AUTH_HMAC_KEY=$(cat /tmp/key)|" .env && rm /tmp/key
# Edit .env to add ANTHROPIC_API_KEY

# 3. Initialize DB and issue first admin token (PRINTED ONCE — copy it)
uv run python -m admin.seed

# 4. Run from terminal
uv run agent --task "summarize the README in this repo"

# 5. Run the HTTP server (in another terminal)
uv run uvicorn triggers.http:app --host 127.0.0.1 --port 8765

# 6. Smoke test from the same machine
curl -sS -H "X-Agent-Token: <token>" -H "Content-Type: application/json" \
     -d '{"task":"hello"}' http://127.0.0.1:8765/run
```

## Token issuance

```bash
uv run agent token issue --user yunyan --label yunyan-iphone
uv run agent token list
uv run agent token revoke <hash-prefix>
```

## Notes

- **Encryption at rest** (SQLCipher + Keychain) is staged for M3, when real
  family memory starts living in the DB. M1 uses plain SQLite.
- **Tailscale** is the network boundary. The HTTP server binds `127.0.0.1` only;
  Tailscale's `serve` exposes it to the tailnet.
- **Backup**: `data/agent.db` plus the `AUTH_HMAC_KEY` from `.env`. Once SQLCipher
  lands, backups must include the Keychain entry too.
- **Recovering from a lost HMAC key**: all tokens become invalid. Re-seed and
  re-issue.

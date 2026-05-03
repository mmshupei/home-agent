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

## launchd schedules (M7)

Three jobs ship in `triggers/launchd/`:

| Plist | Schedule | Purpose |
|---|---|---|
| `com.shupei.agent.morning_brief.plist` | 7:00 daily | Run morning_brief, deliver via iMessage |
| `com.shupei.agent.memory_prune.plist`  | 3:30 daily | Decay confidences, archive low-confidence, prune expired |
| `com.shupei.agent.critic_sweep.plist`  | hourly     | Review any sessions whose inline critic was dropped |

Install / uninstall / status:

```bash
uv run python -m admin.launchd install
uv run python -m admin.launchd status
uv run python -m admin.launchd uninstall
```

The install step copies templates into `~/Library/LaunchAgents/`, rewriting the
hardcoded `/Users/smo` path to your actual home, and bootstraps each one into
your gui domain.

**Mac sleep / wake.** launchd does fire at the scheduled time only if the Mac is
awake or `pmset` has scheduled a wake. Two options:
1. **Always-on Mac** (recommended for the agent host): System Settings →
   Energy → "Prevent automatic sleeping when display is off."
2. **Scheduled wake**:
   ```bash
   sudo pmset repeat wakeorpoweron MTWRF 06:55:00
   ```
   This wakes the Mac at 06:55 weekdays so the 07:00 brief runs reliably.

`caffeinate -s` inside the brief itself isn't needed unless the brief is long-
running; the spawn-per-task model means each invocation is short.

## iOS Shortcut — "Ask Agent" (M5)

Each family member installs this Shortcut on their iPhone (Action Button or Back Tap):

1. **Ask for input** — type: Text, prompt: "Ask the agent…"
2. **Get Contents of URL** —
   - URL: `http://<your-mac-tailnet-name>.ts.net:8765/run`
   - Method: `POST`
   - Headers:
     - `X-Agent-Token`: `agent_<their_token>` (paste their personal token)
     - `Content-Type`: `application/json`
   - Request Body (JSON):
     - `task`: (Provided Input from step 1)
     - `profile_hint`: `mobile`
3. **Get Dictionary Value** — key: `response`
4. **Show Result** — (Dictionary Value from step 3)

Token storage: paste it inside the Shortcut itself (Comment block above the URL action).
Don't share Shortcuts containing tokens.

For approvals (L2/L3 actions) the agent will push to Pushover instead of replying
inline. Approve on the iPhone; the Shortcut returns the result once the action
completes.

A second optional Shortcut "Tell Family" can `POST /memory/family` directly for
quick fact-saves like "cleaners coming Thursday" — bypasses the agent loop.

### Pushover (per-family-member)

Get a [Pushover](https://pushover.net) account ($5 one-time). Each person installs
the Pushover iOS app and gets a User Key (visible on the dashboard). Then in
`.env`:

```
PUSHOVER_APP_TOKEN=<from your Pushover application>
PUSHOVER_USER_KEY_SHUPEI=<Shupei's user key>
PUSHOVER_USER_KEY_YUNYAN=<Yunyan's user key>
```

When unset for a given user, the gate **denies** L2/L3 mobile-profile calls
rather than silently allowing them.

## Notes

- **Encryption at rest** (SQLCipher + Keychain) is staged for M3, when real
  family memory starts living in the DB. M1 uses plain SQLite.
- **Tailscale** is the network boundary. The HTTP server binds `127.0.0.1` only;
  Tailscale's `serve` exposes it to the tailnet.
- **Backup**: `data/agent.db` plus the `AUTH_HMAC_KEY` from `.env`. Once SQLCipher
  lands, backups must include the Keychain entry too.
- **Recovering from a lost HMAC key**: all tokens become invalid. Re-seed and
  re-issue.

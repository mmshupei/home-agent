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

## Telegram bot (M8b — recommended chat surface)

Family members chat with the agent via Telegram. Each Telegram user is a
distinct chat with the bot — no same-Apple-ID collision (the iMessage gotcha).

### One-time setup

**1. Create the bot.** On your iPhone or in Telegram desktop:
- Open Telegram, search for `@BotFather`, tap Start
- Send `/newbot` → give it a display name (e.g. "Shupei Family Agent") and a
  username ending in `bot` (e.g. `shupei_family_agent_bot`)
- BotFather replies with an HTTP API token — copy it

**2. Drop the token in `.env`:**
```
TELEGRAM_BOT_TOKEN=<paste here>
```
(Optional: also set `TELEGRAM_BOT_USERNAME=shupei_family_agent_bot` to skip the
auto-detection round-trip.)

**3. Start the bot:**
```bash
uv run python -m triggers.telegram_bot
# or as a launchd daemon:
uv run python -m admin.launchd install
```

**4. Link each family member.** Run for each user:
```bash
uv run agent user link-telegram --user shupei
```
The CLI prints a `https://t.me/<botname>?start=<token>` URL. Tap it on the
iPhone where Telegram is installed; the bot binds the Telegram account to that
agent user_id. Token expires in 5 minutes; re-issue if needed.

### Behavior

- Inbound messages route through `loop.run()` under the **mobile profile**, so
  L2/L3 actions go through Pushover approval — same gate as the iOS Shortcut path
- "Typing…" indicator while the agent works
- Last 4 turns within 30 minutes carried as thread context
- Replies clipped to 4000 chars (Telegram's limit is 4096)
- Unknown Telegram users get a polite "ask the operator for a link" reply,
  never silently accepted
- No Full Disk Access required — Telegram delivers via HTTPS

## iMessage relay (M8)

Family members text the Mac like any other contact; the agent receives via
`~/Library/Messages/chat.db`, processes through the orchestrator (mobile
profile, with thread context), and replies through Messages.app.

### One-time setup

1. **Register each family member's handle**
   ```bash
   uv run agent user add --id yunyan --name Yunyan --role adult \
        --imessage "+15551234567"
   ```
   Phone numbers can be E.164 (+1...) or just the last 10 digits — the
   relay normalizes both.

2. **Grant Full Disk Access** to the binary that will run the relay.
   System Settings → Privacy & Security → Full Disk Access → toggle on:
   - `/opt/homebrew/bin/uv`     (the runtime)
   - `/usr/sbin/sshd-keygen-wrapper` (only if you'll relay from launchd)

   Without this, the relay errors immediately (`authorization denied` on
   chat.db).

3. **(Optional) Grant Automation permission** for Messages.app: System
   Settings → Privacy & Security → Automation → uv → check Messages. The
   first send will prompt for it interactively — accept.

### Run

Foreground (for testing):
```bash
uv run python -m triggers.imessage_relay --dry-run        # log inbound, no reply
uv run python -m triggers.imessage_relay                  # live
```

As a launchd daemon (auto-restart, runs at login):
```bash
uv run python -m admin.launchd install   # picks up the imessage_relay.plist too
```

### Behavior

- Only known handles are processed (silent ignore for unknowns)
- Last 4 exchanges in the past 30 minutes are passed as thread context
- Replies are clipped to 1500 chars
- L2/L3 actions trigger the mobile-profile Pushover prompt — no inline allow
- State (`data/imessage_relay.state`) records the last processed ROWID; on
  first run with no state, jumps to current MAX(ROWID) so the backlog isn't
  reprocessed

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

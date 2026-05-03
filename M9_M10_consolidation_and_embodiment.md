# Family Agent — M9 & M10 Addendum: Consolidation and Embodiment

**Status:** Design, deferred until M1–M8 are running and producing real data
**Prerequisites:** Working memory system (M3), real session history (≥2 weeks of M5+), Reachy Mini (for M10)
**Companion to:** `family_agent_design.md`

---

## 0. Why This Exists

The base design (M1–M8) treats memory as an accumulating store: write, retrieve, occasionally prune. That works, but it has a known failure mode — quality degrades monotonically. Without active consolidation, you get:

- Stale facts retrieved alongside current ones, no way to tell which is which
- The same pattern re-derived from raw episodes on every query (wasted tokens, inconsistent answers)
- Contradictions that quietly coexist because no process looks for them
- Memory whose structure reflects arrival order rather than actual relationships
- Episodic data (long transcripts, ambient interactions) that's either dropped on the floor or pollutes retrieval

The fix isn't a bigger model or better embeddings. It's a separate process that runs while no one is looking — *consolidation*, sometimes loosely called "dreaming" — that does what the per-session critic can't: think across sessions, across days, across people.

Reachy Mini sharpens this. Once the agent is embodied — running storytelling sessions with Yi'an, ambient interactions throughout the day — the volume of episodic data jumps an order of magnitude and demands real consolidation rather than retention. M10 is where the agent acquires a body; M9 is what makes that body's experiences become part of the agent rather than just logs.

This addendum specifies both, with M9 designed to stand alone (useful even without Reachy) and M10 layering on top.

---

## 1. What Consolidation Actually Does

Strip the metaphor. Three concrete functions, each addressing a real failure mode:

**Replay → abstraction.** Specific episodes ("Yunyan picked up Yi'an Tuesday at 4pm"; "Yunyan picked up Yi'an Thursday at 4pm"; "Shupei picked up Yi'an Wednesday at 4:15") become general schemas ("Tuesday/Thursday are Yunyan's days, ~4pm; Wednesday is Shupei's, ~4:15"). The episodes don't go away — they're evidence. The schema is what gets retrieved first.

**Active pruning.** Memories that didn't get touched, didn't match, didn't matter — get demoted. Not deleted by default; demoted to lower confidence and eventually archived. This is the inverse of accumulation, and it's the half most "memory systems" skip.

**Reorganization.** Re-embed memories whose context changed (a new schema makes its supporting episodes mean something more specific). Surface dormant memories that got buried. Notice unexpected proximities — without acting on them, just flagging them for human review.

The key word above is "didn't act." Consolidation *proposes*. The household *confirms*. Without that asymmetry, the agent's understanding silently drifts and trust collapses.

---

## 2. The Episodic Stream

Before consolidation matters, you need something to consolidate. The base design has two write paths into memory: explicit (`memory__write`) and distilled (per-session critic). Both produce *memory items*: short, factual, retrieval-ready. That's correct for tool-driven sessions where the goal is task completion.

Embodiment changes the data shape. Reachy interactions, ambient conversation pickup, scheduled check-ins — these produce *episodes*: temporally bounded experiences with structure of their own. Treating them as memory items destroys the structure. Treating them as transient destroys the value.

The right primitive is a separate `episodes` table that's high-volume, time-indexed, and *fed into* memory by consolidation rather than queried directly at inference time.

```sql
CREATE TABLE episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,           -- 'reachy' | 'imessage' | 'cli' | 'launchd' | 'http'
    principal       TEXT,                    -- main human participant (nullable for ambient)
    participants    TEXT,                    -- JSON array of user_ids present
    started_at      TIMESTAMP NOT NULL,
    ended_at        TIMESTAMP,
    transcript      TEXT,                    -- full text of the interaction
    audio_path      TEXT,                    -- nullable; path to wav if archived
    affect          TEXT,                    -- JSON: duration_s, turn_count, laughs, interruptions, ...
    summary         TEXT,                    -- 1-2 sentences, written at episode close
    embedding       BLOB,                    -- of the summary, for night-cycle clustering
    consolidated_at TIMESTAMP,               -- null until night cycle processes
    consolidation_notes TEXT                 -- what the night cycle extracted/decided
);
CREATE INDEX idx_episodes_unconsolidated ON episodes(consolidated_at) WHERE consolidated_at IS NULL;
CREATE INDEX idx_episodes_time ON episodes(started_at);
CREATE INDEX idx_episodes_source ON episodes(source);
```

A few things worth highlighting:

- **Episodes are immutable.** Once written, they don't change. Errors get corrected by *new* episodes that supersede; the historical record stays intact. This is the foundation of being able to revert consolidation decisions.
- **Affect is JSON, not a fixed schema.** Different sources record different things. Reachy gets `{turn_count, total_duration_s, laughs, interruptions, dominant_emotion}`; iMessage gets `{message_count, response_latency_avg_s, emoji_density}`; CLI gets nearly nothing. Don't over-design.
- **Audio is optional.** Storing wav files for every Reachy interaction is wasteful and creates real privacy concerns. Default: transcript only, drop audio after Whisper transcribes. Enable audio retention only for specific cases (debugging, explicit consent for a session).
- **Summary is written at episode *close*, not in the night cycle.** This is the same idea as the per-session critic — distill while the context is hot. The night cycle reads summaries, not raw transcripts, except when it needs to.

### Episode lifecycle

```
1. Source (Reachy, iMessage, CLI) opens an episode → INSERT
2. Interaction happens, transcript accumulates
3. Source detects end (silence timeout, explicit goodbye, session close) → UPDATE
4. Episode-close hook: write summary, embed, set ended_at
5. Episode sits with consolidated_at = NULL
6. Night cycle processes → updates consolidated_at, may write to memory
```

For the agent loop itself (the existing M1 path), every `loop.run()` is also an episode. Source is `cli`/`http`/`imessage`, transcript is the user-agent exchange, summary is the final message plus a one-line gist. This means the night cycle has visibility into both ambient (Reachy) and intentional (agent) interactions, which is what makes cross-cutting patterns visible.

---

## 3. Memory Kinds, Extended

The base design defined four kinds: `fact`, `preference`, `event`, `lesson`. Consolidation needs three more:

```
schema           — an abstracted pattern derived from multiple episodes/facts
                   ("Tuesday/Thursday are Yunyan's pickup days")
question_pending — a contradiction or gap the night cycle queued for confirmation
                   ("Two memories disagree on Yi'an's school start time")
observation      — a longitudinal pattern flagged but not yet promoted to schema
                   ("Yi'an has asked for space stories 3 nights in a row, vs. usual ocean")
```

`schema` is the consolidation output. It links to its supporting evidence:

```sql
CREATE TABLE schema_evidence (
    schema_id       INTEGER NOT NULL REFERENCES memory(id),
    episode_id      INTEGER REFERENCES episodes(id),
    memory_id       INTEGER REFERENCES memory(id),  -- if evidence is another memory item
    weight          REAL DEFAULT 1.0,
    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (schema_id, episode_id, memory_id)
);
```

The point of evidence linkage: when retrieval surfaces a schema, the agent can — if needed — drill into what it was built from. And when consolidation revises a schema (or finds it unsupported), the lineage is intact.

`question_pending` is the surface for trust preservation. The night cycle never silently overrides; contradictions become questions, and the next morning interaction surfaces them.

`observation` is the cautious tier between "noticed nothing" and "promoted to schema." Three weeks of "Yi'an seems quieter at dinner" doesn't become a schema, but it shouldn't vanish either. Observations have low confidence, low retrieval weight, and a longer life — they're material for the night cycle to keep watching, not knowledge to act on.

---

## 4. The Night Cycle

### Schedule

launchd, 3:00am Pacific, every night.

```xml
<!-- triggers/launchd/night_cycle.plist -->
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
```

3am because: the household is asleep; episode writes from the previous day are complete; any morning interaction (likely Shupei first up at 6:30) will see the night cycle's output fresh. The Mac must be awake — `pmset` schedule wakes for it, then sleeps after.

### Structure

The cycle runs as a single agentic session, but with a constrained tool surface (no external tools, no L3 actions, only memory and database reads/writes) and a structured prompt that walks through phases. Model: Sonnet with extended thinking enabled, ~10-20K reasoning tokens budget. Cost ballpark: 30-80¢ per night.

```python
# orchestrator/night_cycle.py
async def run_night_cycle():
    cycle = NightCycle.start()

    inputs = {
        "unconsolidated_episodes": fetch_episodes(consolidated=False, limit=50),
        "recent_memory":           fetch_memory(days=30, scopes=["family", "system"]),
        "pending_questions":       fetch_memory_kind("question_pending"),
        "recent_observations":     fetch_memory_kind("observation", days=60),
        "yesterday_session_log":   fetch_sessions(days=1),
        "last_cycle_notes":        fetch_last_cycle_notes(),
    }

    options = ClaudeAgentOptions(
        system_prompt=NIGHT_CYCLE_PROMPT,
        mcp_servers=[memory_server, db_query_server],   # internal only
        allowed_tools=[
            "memory__read", "memory__write", "memory__update",
            "memory__archive", "memory__query_kind",
            "db__select",
            "schema__add_evidence",
        ],
        model="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 16000},
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(format_inputs(inputs))
        async for msg in client.receive_response():
            cycle.record(msg)

    cycle.finalize()
```

### The four phases

The prompt walks the model through four phases. Don't try to run them as separate sessions — the value comes from holding all of it in working memory at once, which is exactly what extended thinking is for.

```
NIGHT_CYCLE_PROMPT = """
You are running the household agent's nightly consolidation cycle.
The household is asleep. Take your time. Be thoughtful, conservative,
and explicit about what you're proposing versus deciding.

Inputs:
- Yesterday's episodes (Reachy interactions, agent sessions, messages)
- The last 30 days of family/system memory
- Pending questions queued by previous cycles
- Recent observations (longitudinal patterns under watch)
- The last cycle's notes (what you wrote about the previous day)

Phases — do them in order, output structured results:

PHASE 1 — REPLAY
Read yesterday's episodes. For each, ask:
- Did this interaction succeed for the human? (signal: did they come back, did
  they correct, did they thank, did they trail off?)
- Did the agent or Reachy make any commitments ("I'll remind you", "let's try
  this tomorrow") that need follow-through?
- Were there moments of correction or confusion that suggest the agent's
  understanding is wrong?

Output: replay_notes (prose) + commitments (structured list with due dates)

PHASE 2 — ABSTRACT
Look across recent memory and yesterday's episodes for patterns:
- Repeated behaviors → schema candidates ("X usually happens on Y")
- Stable preferences → preference promotion
- Recurring topics → observation candidates

Rules:
- A schema requires ≥3 supporting episodes/facts.
- An observation requires ≥2 occurrences of something not yet patterned.
- If unsure, prefer observation over schema. Easier to upgrade later than
  to retract a confidently-wrong schema.

Output: schema_proposals + observation_proposals (each with evidence list)

PHASE 3 — RECONCILE
Look for contradictions, redundancy, and decay:
- Contradiction: two memories disagree on a fact. Don't pick a winner —
  queue a question_pending for the morning.
- Redundancy: multiple memories say nearly the same thing. Keep the most
  specific; archive the others.
- Decay: memories with confidence below 0.3 and last_seen > 60 days ago
  go to archive.

Rules:
- Never silently overwrite a fact. Contradictions become questions.
- Never delete; only archive (recoverable).
- A user-explicit memory (confidence 1.0) is never archived without an
  explicit pending question first.

Output: contradictions + redundancies_resolved + archive_actions

PHASE 4 — REORGANIZE
- For each new schema, link supporting episodes via schema_evidence.
- For each schema with new evidence, recompute its confidence based on
  evidence count and recency.
- For observations that crossed the schema threshold during this cycle,
  promote them.
- Look at the cluster topology of recent family-scope memory: are there
  unexpected proximities worth flagging? (Don't draw conclusions; flag.)

Output: reorganization_actions + topology_notes

FINAL — REFLECTION
Write 3-5 sentences of prose: what changed in your understanding of this
household today? What surprised you? What are you watching for tomorrow?

This goes into night_cycles.notes and serves as memory for future cycles.
Be honest, including about uncertainty. Avoid grandiosity.
"""
```

### Tool budget per phase

The model has a real budget and can't do everything. Defaults:

- Phase 1 (replay): ≤20 episodes. If more, prioritize by source diversity (Reachy + at least one agent session per principal).
- Phase 2 (abstract): up to 5 schema proposals, up to 10 observation proposals.
- Phase 3 (reconcile): all contradictions found, but ≤3 questions queued (avoid morning-interaction overload).
- Phase 4 (reorganize): bounded by what phase 2 produced.

### Outputs

```sql
CREATE TABLE night_cycles (
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
    notes                   TEXT,         -- the reflection
    raw_output              TEXT          -- the full structured response, for debugging
);
```

One row per night. Read the `notes` column over time and you have a longitudinal record of how the agent's understanding of the household evolved. This is also the first thing to read when something feels wrong.

---

## 5. Trust Preservation

The architectural failure mode of any consolidation system is that it silently changes what the agent knows. The household stops being able to predict what the agent will do, and trust collapses. Three rules prevent this:

### Rule 1: Episodes are immutable. Memory is mutable but versioned.

Raw episodes never change after `ended_at` is set. A correction comes as a *new* episode, not an edit.

Memory items can change, but every change is logged:

```sql
CREATE TABLE memory_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memory(id),
    revised_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revised_by      TEXT NOT NULL,            -- 'night_cycle' | 'user:shupei' | 'critic'
    field           TEXT NOT NULL,            -- 'content' | 'confidence' | 'scope' | 'archived'
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT
);
```

Reverting any single change is a SQL operation. Reverting a whole night cycle is "find all revisions where revised_by = 'night_cycle' and revised_at = X, restore old_values." The CLI exposes this:

```
just memory revert-cycle 2026-05-03
```

### Rule 2: Propose, don't decide.

The night cycle never silently:
- Overrides a user-explicit fact
- Resolves a contradiction
- Deletes anything

When in doubt, it queues a `question_pending`. The morning interaction surfaces these (one at a time, at most 3 per day, prioritized by importance × staleness):

> "Two memories say different things about Yi'an's school start — one says 8:30, one says 9:00. Which is current?"

The user answers; the answer becomes a high-confidence memory revision; the contradiction is closed. The audit log shows the whole loop.

### Rule 3: Schemas explain themselves.

Any schema retrieved into context comes with its evidence list available. If the agent says "Yunyan usually picks up Yi'an on Tuesdays," the user can ask "why do you think that?" and the agent can produce the evidence. Schemas without legible evidence are bugs, not features.

In the system prompt:

> When using a schema in your response, be prepared to cite its evidence
> if asked. Schemas are inferences, not facts; treat them with appropriate
> hedging when stakes are high.

---

## 6. Morning Surface

The night cycle queued things; the morning needs to surface them. Two mechanisms:

### Auto-prepend

The `compose_prompt` function (from M1) gets a new section. After the regular memory context block, prepend any `question_pending` items in family scope, plus commitments due today:

```
<night_cycle_surface>
Pending questions (please surface naturally if appropriate):
1. Two memories disagree on Yi'an's school start time. Which is current — 8:30 or 9:00?
   (last seen: 2026-04-29; staleness suggests asking soon)

Commitments due today:
- "Remind Shupei about the Hugues Plessis meeting" (made by Reachy, 2026-04-30)
</night_cycle_surface>
```

The agent is instructed to weave these in *naturally*, not interrogate. If the user says "good morning, what's my day look like?", the response can include "before I get to today, quick — was it 8:30 or 9:00 for Yi'an's school start? I had two different memories." If the user fires off a quick question and leaves, don't surface; queue for next interaction.

### Morning brief integration

The launchd-scheduled morning brief (M7) reads `question_pending` and includes the top one in the brief. This means even passive consumption of the brief surfaces the question.

### Limits

Hard cap: 3 pending questions surfaced per day total, across all interaction surfaces. Prevents the agent from feeling like a homework assignment. Questions that aren't answered for 14 days get auto-archived with a `question_unanswered` lesson written: "Asked about X on Y; user did not engage. Don't re-ask without new evidence."

---

## 7. M10 — Reachy Embodiment

Reachy Mini joins as another source for episodes, another surface for interaction, and — distinctively — a body the agent can sometimes inhabit. The integration is structurally simple if M9 is in place; the value is that Reachy now feeds the same memory and consolidation pipeline as everything else.

### What stays the same

- Reachy uses the same auth (it has its own token, principal=`reachy_yian` or similar)
- It writes to the same `episodes` table
- Its interactions go through the same gate (curated tier rules for Reachy-specific tools)
- Its experiences feed the same night cycle

### What's new

**A device principal.** Reachy isn't quite a user — it's an agent-in-a-body that interacts on behalf of users (mostly Yi'an). The cleanest model: device principals with a specific role.

```toml
# config/roles.toml — addition
[device]
default_profile = "embedded"
allowed_profiles = ["embedded"]
max_tier = 2                    # no L3 from Reachy
can_promote_memory = false      # only via consolidation
can_read_all_scopes = false     # family + system + the user it's serving
```

Reachy's token is provisioned per session (or per day) by the agent itself, scoped to the user it's serving. When Yi'an starts a story session, the agent issues a short-lived token to Reachy bound to `user:yian` + `family`.

**An `embedded` profile.** Reachy's profile differs from `mobile`:

```toml
[embedded]
description = "Reachy in-home interaction"
policy = { 1 = "allow", 2 = "allow", 3 = "deny" }
domains = ["storytelling", "child_interaction"]
# L2 allowed without prompt because Reachy interactions are real-time;
# L3 simply not allowed at all.
```

The reasoning: a child is talking to a robot, you cannot have approval prompts in the loop. The mitigation is the hard L3 ceiling — Reachy can never send a message, place an order, lock a door. Worst case for an L2 misfire is a Reminder gets created or a light gets turned on.

**Reachy-specific tools (curated MCP).**

```python
# tools/sdk_mcp/reachy.py
@tool("tell_story", "Tell Yi'an a story on a given theme.", {"theme": str, "length_minutes": int})
async def tell_story(args): ...

@tool("ask_about_day", "Ask Yi'an open-ended questions about his day.", {})
async def ask_about_day(args): ...

@tool("set_quiet_mode", "Stop speaking, listen passively.", {})
async def set_quiet_mode(args): ...
```

Curated routines, not raw motor primitives. Reachy's lower-level controls (head movement, audio I/O, wake word) live below this layer in the existing Reachy app codebase; the agent only sees high-level capabilities.

**Episode-writing hooks.** Reachy's existing app gets two additions:

1. On wake (interaction starts): `POST /episode/start` to the FastAPI server, get an episode_id back
2. On sleep (silence timeout or explicit goodbye): `POST /episode/end` with transcript + affect

The episode-end hook does the same summarize-and-embed as agent sessions. The night cycle picks them up the same way.

### What Reachy uniquely provides

Three signals not available from text-only interactions:

- **Affect signal.** Whisper transcripts plus simple acoustic features (speaking rate, pause duration, laughter detection — the last one is the noisiest, treat it as a hint not a fact). Stored in `episodes.affect`.
- **Routine vs novelty.** Reachy interactions cluster naturally (bedtime stories, morning chats, weekend play). The night cycle can notice when something is unusual: "Yi'an asked for space stories three nights in a row, vs. usual ocean." This becomes an `observation`, watched, possibly promoted.
- **Temporal coherence.** Reachy's episodes have natural beginnings and ends; agent sessions often don't. This makes Reachy data unusually clean for longitudinal analysis.

Don't over-engineer affect detection. Duration, turn count, and presence/absence of laughter cover 90% of useful signal. Trying to do real emotion recognition opens a door of false confidence and ethical questions for a 5-year-old's interactions; stay shallow on purpose.

### Privacy posture for child interactions

This is the most sensitive data the agent handles. Defaults must be conservative:

- **No audio retention by default.** Whisper transcribes locally (whisper.cpp on the Mac, called from Reachy via the FastAPI server), audio is dropped. Enable retention only for explicit debugging windows.
- **Transcripts stay on the Mac.** Never sent to any third party except Anthropic for the agent loop itself. The night cycle is the only consumer of full transcripts; everyone else sees summaries.
- **Family-scope by default, never user:yian-only.** Yi'an's interactions with Reachy are visible to his parents. This is a deliberate choice — a 5-year-old's relationship with an embodied agent should not be private from his parents. Revisit when he's older.
- **No personality persistence Yi'an doesn't know about.** Reachy's storytelling style adapts to what works (longer stories on quiet nights, shorter on tired ones), but anything that looks like "memory of Yi'an" (knowing his favorite topics, calling back to past stories) should be transparently surfaced when asked. "Reachy, why did you tell me about whales again?" → "You loved the whale story two weeks ago, so I thought you might like another one."

### Build sequence inside M10

1. **Token provisioning**: agent can issue scoped tokens to device principals; Reachy can authenticate
2. **Episode endpoints**: `/episode/start` and `/episode/end` on the FastAPI server
3. **Reachy app integration**: existing app calls those endpoints around each interaction
4. **Curated tools**: `tell_story`, `ask_about_day`, etc. as SDK MCP server
5. **Embedded profile + tier rules**: gate adapted for real-time use
6. **End-to-end test**: Yi'an interacts with Reachy, episode lands in DB, night cycle processes it, morning brief mentions a relevant observation

---

## 8. Schema Migration

The base design (M3) needs the following added. Cheap to add now if desired; otherwise, add at M9.

```sql
-- New table
CREATE TABLE episodes (...);                    -- §2

-- New tables for consolidation
CREATE TABLE schema_evidence (...);             -- §3
CREATE TABLE memory_revisions (...);            -- §5
CREATE TABLE night_cycles (...);                -- §4

-- New memory kinds (no schema change, just new values)
-- 'schema', 'observation', 'question_pending'

-- New device principals (additions to existing users table)
INSERT INTO users (id, name, role) VALUES ('reachy_yian', 'Reachy (Yi''an)', 'device');
```

Recommendation in the original handoff: add the empty `episodes` and `schema_evidence` tables in M3 even if nothing populates them yet. Reserves the namespace, costs nothing, prevents painful migrations later.

---

## 9. Cost & Compute Budget

**Night cycle.** Sonnet with 16K thinking budget, ~50 episodes input, structured JSON output. Realistic cost per night: $0.30–$0.80. Yearly: ~$200, comparable to a streaming subscription. If cost drifts higher, reduce thinking budget first; the structured walk-through is doing most of the work.

**Reachy interactions.** Per the existing Reachy app (Whisper STT + Claude + TTS), unchanged. Adding episode-writing is microseconds.

**Embedding load.** With Reachy producing 5-15 episodes/day on top of agent sessions, you're embedding maybe 30 things daily. bge-m3 in the FastAPI process handles this without breathing hard.

**Storage.** Episodes with transcripts are the big growth driver. A typical Reachy storytelling session is 5-15 minutes, ~2-5KB transcript. Even at 20 episodes/day across all sources, that's ~30MB/year. Negligible. Audio retention would change the math by 1000x — another reason to keep it off by default.

---

## 10. Definition of Done

### M9

- Night cycle runs nightly at 3am via launchd, completes within 5 minutes
- Episodes table populated by all existing sources (CLI, HTTP, iMessage)
- Schema, observation, and question_pending memory kinds working end-to-end
- Morning interaction surfaces pending questions (capped, prioritized)
- Memory revisions logged; `just memory revert-cycle YYYY-MM-DD` works
- Audit: 7 days of night_cycles.notes are readable, coherent, useful
- A real contradiction successfully reaches the user as a question and gets resolved

### M10

- Reachy authenticates with a device-principal token
- Reachy interactions write episodes with transcript + affect
- Curated Reachy tools (`tell_story`, `ask_about_day`) usable via embedded profile
- Embedded profile enforces hard L3 ceiling
- A Reachy-derived observation reaches the morning brief at least once during testing
- Privacy defaults verified: no audio retention, family-scope visibility, transparent recall

---

## 11. What Not to Build (Yet)

These are tempting and wrong for this phase:

- **Continuous learning** that updates the agent's behavior between sessions (e.g., RLAIF on session outcomes). Adds enormous complexity, requires real evaluation infrastructure, breaks reproducibility. Revisit after a year of stable consolidation.
- **Cross-household generalization.** The agent's experience is one household's. Don't try to build patterns that generalize across families — you don't have the data and you don't want the surveillance posture.
- **Affect-driven behavior changes.** Reachy noticing Yi'an seems quiet should *flag for the parents*, not change Reachy's behavior. The line between "responsive embodied agent" and "manipulative system" is thin and you stay on the right side by keeping affect read-only for the agent itself.
- **Dream visualization / personality sims.** Cute, useless, distracting. The night cycle's prose reflection is enough to make the system legible.
- **Multi-agent night cycles.** ("One Sonnet replays, one Sonnet abstracts, one Opus reconciles...") A single thinking-enabled session is more coherent and cheaper. Resist.

---

## 12. Open Questions

These need real-world data to resolve; flag them now, decide later:

- **How much affect signal is real?** Whisper-derived speaking rate and pause duration are reliable; laughter detection is noisy; emotion recognition is mostly theater. After 30 days of Reachy data, run a manual eval: when did affect cues correlate with anything an adult would also notice? Recalibrate.
- **Schema vs observation thresholds.** I picked 3 episodes for schema, 2 for observation. This is a guess. Tune after seeing real promotion rates — if schemas get retracted often, raise the threshold; if observations rot in-place forever, lower it.
- **Question fatigue.** 3 questions/day is a guess. If users start ignoring them, drop to 1; if they're answering quickly and asking for more, raise. Track the engagement rate explicitly.
- **The reflection prose.** Is it actually useful, or is it agent journaling theater? Re-evaluate after 30 days of cycles. If you've never opened the notes column, kill it.
- **Reachy's relationship to other family members.** Designed with Yi'an in mind, but Yunyan and Shupei will interact with it too. Should adult interactions feed the same episode stream, or stay in separate channels? Probably same stream with participant tagging; revisit after observing actual usage.

---

## 13. Sequencing Note

M9 is valuable on its own. Even without Reachy, the night cycle improves the base agent significantly: the family memory store stays clean, contradictions get surfaced, schemas reduce token waste. Build M9 when you have 2-3 weeks of M5+ data and have started feeling the failure modes of accumulating-only memory.

M10 is gated on Reachy actually being deployed and used. Don't build the integration if Reachy is sitting in a box. The right trigger: Reachy has been in active use for 2+ weeks via its own app, and you find yourself wishing it knew what the agent knew.

If both are built together (because Reachy is already deployed by the time you reach M9), the order should be: episodes table and lifecycle first, base night cycle second, Reachy integration third. Doing Reachy before the night cycle gives you a flood of episodes nothing consumes, which is worse than no episodes at all.

---

*End of M9/M10 addendum. To be read alongside `family_agent_design.md`.*

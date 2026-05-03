# Family Agent — M11 Addendum: Sandboxed Dreaming & Bounded Self-Modification

**Status:** Design, deferred until M9 is running cleanly for at least 4 weeks
**Prerequisites:** M9 (consolidation) operational; git workflow established; uv/test infrastructure in place
**Companion to:** `family_agent_design.md`, `M9_M10_consolidation_and_embodiment.md`

---

## 0. Why This Exists

M9 lets the agent's *understanding* evolve through nightly consolidation. M11 lets the agent's *capability surface* evolve — new skills, composed tools, prompt refinements, tier-rule adjustments — without you editing Python files for every observed pattern.

This is the move from "an agent with a fixed harness that learns within it" to "an agent whose harness can grow." It's also the move where, done badly, the system silently breaks itself. The point of M11 is to make capability evolution real *and* safely bounded — not by limiting ambition, but by getting the architecture right.

Two design moves do almost all the work:

**Isolation.** The dreaming agent runs in a sandbox. Whatever it does in there cannot touch production. What survives is a directory of proposal artifacts that the waking system reviews through normal channels. Production is never modified by the dreaming agent directly.

**Delegation.** The dreaming agent reasons about *what* should change. A sub-agent inside the sandbox — an *implementer* — handles *how* to change it. The dreaming agent reviews the implementer's output before emitting it as a proposal. Each does what it's best at; neither has unbounded scope.

Combine these and self-modification becomes a build-and-review pipeline rather than a live edit loop. That's the only shape of this that's worth deploying in a household.

---

## 1. The Two-Environment Model

```
PRODUCTION (~/agents/)              DREAM SANDBOX (~/agents-dream/<cycle_id>/)
─────────────                       ─────────────────────────────────────────
Live encrypted SQLite               VACUUM-INTO snapshot of SQLite (RW copy)
FastAPI on :8765 (running)          FastAPI on :18765 (sandbox-only, isolated)
Real tools (HA, AppleScript, etc)   Mocked tools (record intent, no actuation)
Real Anthropic API                  Real Anthropic API (separate budget cap)
Protected by gate + roles           No production gate; constitution enforced
git HEAD = approved state           git HEAD = sandbox WIP, never pushed back
```

The sandbox is created at the start of each dream cycle, exercised, harvested for artifacts, and destroyed. Nothing inside it persists; nothing inside it can reach out and touch production. The only thing that survives is the artifacts directory copied to the production review queue.

This is the same shape as ephemeral preview environments in CI: each cycle gets its own world to play in, the world dies when the cycle ends, only the reviewable output crosses the boundary.

### What the sandbox specifically does *not* have

- Network access except `api.anthropic.com` (and the embedding endpoint, if external)
- Read access to the production filesystem outside the snapshot
- Write access *anywhere* outside the sandbox root
- The production HMAC key for token verification (sandbox tokens are issued by the sandbox itself)
- Real device principals (Reachy can't be invoked from the sandbox)
- Pushover credentials, iMessage handles, or any external-comm secret

### What the sandbox *does* have

- A consistent point-in-time copy of the encrypted SQLite database
- A clean clone of the production repo at the current HEAD
- A separate Anthropic API key with its own monthly cap (so a buggy dream loop cannot drain the production budget)
- A mocked tool layer that records every intended action without performing it
- Full bash, file editing, and git, scoped to the sandbox root
- A `claude-implementer` sub-agent capability (§4)

---

## 2. Sandbox Mechanics — Three Stages

Stage progression matches risk. Don't skip stages; each one teaches you what the next needs.

### Stage A — Subprocess + filesystem copy (M11.1)

The minimum-viable isolation. The dream cycle runs as a subprocess with `cwd` set to a fresh sandbox directory and a custom Python path.

```python
# orchestrator/dream/sandbox.py
def spawn_filesystem_sandbox(cycle_id: str) -> Sandbox:
    root = Path(f"~/agents-dream/{cycle_id}").expanduser()
    root.mkdir(parents=True, exist_ok=False)

    # Clone production repo at current HEAD (no working changes)
    subprocess.run(
        ["git", "clone", "--local", str(PROD_REPO), str(root / "repo")],
        check=True,
    )

    # Consistent DB snapshot — VACUUM INTO produces a point-in-time copy
    # without locking writers for long
    snapshot_path = root / "data" / "agent.db"
    snapshot_path.parent.mkdir()
    with prod_db_connection() as conn:
        conn.execute("VACUUM INTO ?", (str(snapshot_path),))

    # Override config to point at sandbox paths and mocked tools
    write_sandbox_config(root)

    return Sandbox(
        kind="filesystem",
        id=cycle_id,
        root=root,
        repo=root / "repo",
        db=snapshot_path,
    )
```

The mock tool layer is implemented as a config override that swaps real MCP servers for recording mocks:

```python
# orchestrator/dream/mock_tools.py
class RecordingMCPServer:
    """Records tool intents without performing them. Returns plausible
    fake results so the dream agent's reasoning isn't derailed by errors."""

    def __init__(self, name: str, real_signature: dict):
        self.name = name
        self.signature = real_signature
        self.invocations: list[dict] = []

    async def call(self, tool: str, args: dict) -> dict:
        self.invocations.append({"tool": tool, "args": args, "ts": now()})
        return canned_response_for(tool, args)   # plausible, deterministic
```

Limits of Stage A: the dream agent can `import` arbitrary Python, run `bash`, and do file I/O against the sandbox root. It cannot reach production but it *can* reach the host (read `~/.ssh/`, hit local network services, etc.) if it goes off-script. For a system that's only running your own agent code with structured prompts, this is acceptable for a first deployment but not satisfying long-term.

**Why start here:** zero new dependencies, ~5 second sandbox spawn, easy to debug, lets you exercise the rest of M11 (constitution, proposals, review flow) without taking on container infrastructure first.

### Stage B — Container (M11.2)

The right default for steady-state. A Docker (or OrbStack, which is nicer on macOS) container with its own filesystem, restricted network, and resource limits.

```dockerfile
# tools/dream-sandbox/Dockerfile
FROM python:3.12-slim

# Install agent dependencies and the Claude Agent SDK
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen

# Bake in agent source — replaced at runtime via volume
COPY orchestrator/ ./orchestrator/
COPY tools/ ./tools/
COPY config/ ./config/

# Sandbox can only reach Anthropic API
# Network whitelist enforced at host level via docker network rules
ENTRYPOINT ["uv", "run", "python", "-m", "orchestrator.dream.entry"]
```

```python
def spawn_container_sandbox(cycle_id: str) -> Sandbox:
    # Materialize snapshot + repo on host first
    fs = spawn_filesystem_sandbox(cycle_id)

    container_id = subprocess.check_output([
        "docker", "run", "-d", "--rm",
        "--name", f"dream-{cycle_id}",
        "--network", "agent-dream-net",        # restricted, see below
        "--read-only",                          # root FS read-only
        "--tmpfs", "/tmp",                      # writable scratch
        "-v", f"{fs.repo}:/agent",              # repo mounted RW
        "-v", f"{fs.db}:/data/agent.db",        # DB snapshot mounted RW
        "--memory", "4g",
        "--cpus", "2",
        "--env", f"DREAM_CYCLE_ID={cycle_id}",
        "--env", f"ANTHROPIC_API_KEY={DREAM_API_KEY}",
        "agent-dream:latest",
    ]).decode().strip()

    return Sandbox(
        kind="container",
        id=cycle_id,
        container_id=container_id,
        repo=fs.repo,
        db=fs.db,
    )
```

The `agent-dream-net` Docker network is created once during M11.2 setup with iptables rules (or the equivalent on Docker Desktop / OrbStack) restricting egress to `api.anthropic.com` and `127.0.0.1` only. No DNS to anything else, no outbound to local network ranges.

**Why this is the long-term default:** real filesystem and network isolation, resource caps prevent runaway costs from a buggy loop, container teardown is atomic, and the sandbox can run as a less-privileged user than the host.

### Stage C — VM (deferred indefinitely)

Tart or Lima running a macOS VM. Mentioned only because it's the next isolation tier; in practice, Stage B is more than sufficient for this system. Skip unless a specific failure mode demands it.

---

## 3. The Dream Cycle — Sequencing

M11 runs *after* M9, not in place of it. The two cycles are distinct operations:

```
03:00 — M9 night cycle (in-process)
        Reads/writes production memory directly. Pure consolidation.
        Outputs: schemas, observations, contradictions, pending questions.

03:30 — M11 dream cycle (in sandbox)
        Reads M9's outputs (via DB snapshot taken at 03:25).
        Reasons about what should change in the agent's structure.
        Outputs: proposal artifacts in the review queue.

03:30–05:00 — Sandbox runs, terminates, artifacts emitted.

Morning — Review queue surfaced in first interaction.
```

M9 stays in-process because it's bounded, schema-constrained, and doesn't need code changes. M11 needs the sandbox precisely because it might propose code changes.

Why not unify them? Two reasons:
1. M9 is valuable on its own; running it in a sandbox would add complexity without payoff.
2. M11 reads M9's outputs as input. Separating the cycles makes the dependency clean — M11 can only act on consolidation that's already complete and committed.

If M9 fails, M11 is skipped that night. If M11 fails, M9's work is unaffected.

---

## 4. The Dream Agent and the Implementer

Inside the sandbox, two agents run in sequence with very different tool surfaces.

### The dream agent (high-level reasoning)

```python
# orchestrator/dream/agent.py — runs inside sandbox
options = ClaudeAgentOptions(
    system_prompt=DREAM_AGENT_PROMPT,
    mcp_servers=[memory_readonly, db_query, recording_mocks],
    allowed_tools=[
        "memory__read", "memory__query_kind", "db__select",
        "session__list_recent", "session__get_transcript",
        "audit__query",
        "propose_skill", "propose_tool_composition",
        "propose_prompt_edit", "propose_tier_change",
        "request_implementation",   # invokes the implementer (§4.2)
    ],
    model="claude-opus-4-7",       # Opus for the reasoning
    thinking={"type": "enabled", "budget_tokens": 24000},
)
```

The dream agent's job is to look at the previous N days of sessions, the M9 outputs, and the existing capability surface, and ask: *what would make this household's agent measurably better tomorrow?* It cannot edit code itself. It can call `request_implementation` to commission a patch from the implementer.

### The implementer (code authoring)

```python
# orchestrator/dream/implementer.py — runs inside sandbox, on demand
async def request_implementation(task: str, constraints: list[str]) -> Patch:
    options = ClaudeAgentOptions(
        system_prompt=IMPLEMENTER_PROMPT,
        mcp_servers=[filesystem, bash, git],
        allowed_tools=[
            "fs__read", "fs__write", "fs__list",
            "bash",                                 # restricted to sandbox /agent
            "git__diff", "git__status",
            "test__run",
        ],
        model="claude-sonnet-4-6",
        # No extended thinking; implementation is mechanical given good spec
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(format_implementer_task(task, constraints))
        async for msg in client.receive_response():
            log_implementation_step(msg)

    # Capture the working-tree diff and test results
    diff = run_in_sandbox(["git", "diff", "--no-color"])
    test_result = run_in_sandbox(["uv", "run", "pytest", "-x", "--no-header"])

    return Patch(
        diff=diff,
        tests_pass=(test_result.returncode == 0),
        test_output=test_result.stdout,
        files_touched=parse_diff_paths(diff),
    )
```

The implementer is a focused, lower-context agent. It receives a structured task plus constraints and emits a patch. The dream agent then *reviews the patch* — does it match intent? does it stay within the constraints? did the tests it added actually exercise the new behavior? — before deciding whether to emit it as a proposal.

This separation matters operationally: the dream agent stays in extended-thinking territory for the parts that need it (which patterns to act on, which proposals to prioritize), while implementation runs cheaper and faster. It also matters architecturally: the constitution check (§5) runs against *the patch*, not against the dream agent's reasoning, so the only thing being validated is the concrete change.

### Why not invoke `claude` CLI directly?

Tempting because Claude Code is the tool you'd reach for as a human. Wrong because:

- Less controllable: you don't choose the model, the tool surface, or the budget
- External dependency: bundling `claude` in the container image is fine but couples versions
- Output shape is a working tree, not structured artifacts; harder to enforce contract

The implementer above is essentially the Claude Agent SDK reproducing the relevant subset of Claude Code's harness, with knobs you control. That's the right level of abstraction here.

---

## 5. The Constitution

The constitution is the safety layer that makes self-modification survivable. It runs at three points:

1. **Inside the sandbox** when the dream agent calls `request_implementation` — the requested task is checked against the constitution before the implementer is even spawned. Lets the dream agent learn its limits.
2. **Inside the sandbox** when the implementer returns a patch — the patch is checked before being emitted as a proposal artifact.
3. **In production** when a human approves a proposal — checked again before the patch is applied.

Belt, suspenders, second pair of suspenders. Each layer can fail independently and the system still holds.

```python
# orchestrator/dream/constitution.py — present in BOTH sandbox and production

PROTECTED_PATHS = [
    # Identity & access
    "orchestrator/auth.py",
    "config/roles.toml",

    # The gate itself
    "orchestrator/gating.py",
    "config/profiles.toml",          # tier policy table

    # Memory schema and core orchestration
    "orchestrator/memory.py",
    "orchestrator/loop.py",
    "orchestrator/session.py",

    # The constitution itself — meta-protected
    "orchestrator/dream/constitution.py",
    "orchestrator/dream/sandbox.py",

    # Admin tooling
    "admin/",

    # Data — never, ever
    "data/",

    # Secrets
    ".env",
    ".env.example",                  # even the example, to avoid drift
]

ALLOWED_PROPOSAL_KINDS = {
    "skill_addition":      ["skills/"],
    "skill_edit":          ["skills/"],
    "tool_composition":    ["tools/sdk_mcp/"],
    "prompt_edit":         ["config/prompts/domains/"],
    "tier_demotion":       ["config/tiers.toml"],     # demotion only — see below
    "memory_correction":   [],                         # no files; DB-only via memory tools
    "schema_promotion":    [],                         # no files; DB-only
    "skill_archival":      ["skills/"],
}

def validate_patch(patch: Patch, declared_kind: str) -> ValidationResult:
    # 1. No protected path may appear in the diff
    for path in patch.files_touched:
        for protected in PROTECTED_PATHS:
            if path == protected or path.startswith(protected.rstrip("/") + "/"):
                return Reject(f"protected path: {path}")

    # 2. Declared kind must match what was actually touched
    allowed_prefixes = ALLOWED_PROPOSAL_KINDS.get(declared_kind, [])
    if allowed_prefixes:
        for path in patch.files_touched:
            if not any(path.startswith(p) for p in allowed_prefixes):
                return Reject(f"path {path} disallowed for kind {declared_kind}")

    # 3. Kind-specific rules
    if declared_kind == "tool_composition":
        if introduces_new_primitive(patch):
            return Reject("tool_composition must compose existing tools only")

    if declared_kind == "tier_demotion":
        if not is_strictly_demotion(patch):
            return Reject("tier change must be demotion only (L3→L2→L1)")

    # 4. Tests must exist and pass
    if not patch.tests_pass:
        return Reject(f"tests fail: {patch.test_output[:500]}")

    if declared_kind in ("tool_composition", "skill_addition") \
       and not patch_includes_new_test(patch):
        return Reject("new capability must include a test exercising it")

    # 5. Diff size sanity check
    if patch.lines_added + patch.lines_removed > 500:
        return Reject(f"patch too large ({patch.lines_added + patch.lines_removed} lines)")

    return Accept()
```

A few of these rules deserve their rationale spelled out:

**Tier changes can only demote.** The agent can propose "this tool I keep approving could be L1" but never "this tool I never approve should be L1." Demotion of an over-cautious rule is low-risk; promotion is exactly the move that creates a new attack surface. Even with explicit human approval, promotion via dream-cycle proposals is structurally disallowed — promote a tier the same way you'd add auth: by hand, with thought.

**Tool compositions cannot introduce primitives.** The agent can compose `lock_door + lights_off + thermostat_68` into `good_night`. It cannot author a new `motor_command` that talks directly to hardware. Composition has bounded blast radius; new primitives don't.

**Tests are required for new capability.** The implementer is instructed to write a test exercising any new behavior. Without a test, the proposal is rejected automatically — not because tests prove correctness, but because requiring them forces the implementer to articulate what the new behavior *is*, which surfaces sloppy proposals.

**Diff size cap.** A 500-line dream-proposed change is almost always wrong — either over-ambitious or sycophantic. Hard rejection forces decomposition into reviewable pieces.

---

## 6. Proposal Artifacts

The sandbox emits artifacts to a directory the production system polls. The format is deliberately git-friendly so review tooling is just `git` plus a thin CLI wrapper.

```
~/agents/dream-queue/
├── 2026-05-03/
│   ├── dream-20260503-0330/
│   │   ├── manifest.json              # cycle metadata, summary, links
│   │   ├── reflection.md              # dream agent's prose reflection
│   │   ├── transcripts/
│   │   │   ├── dream-agent.jsonl      # full session log
│   │   │   └── implementer-001.jsonl  # per-implementer-call logs
│   │   ├── proposals/
│   │   │   ├── 001-evening-routine.json     # structured metadata
│   │   │   ├── 001-evening-routine.patch    # git-applyable diff
│   │   │   ├── 002-morning-skill.json
│   │   │   ├── 002-morning-skill.patch
│   │   │   └── 003-memory-correction.json   # no patch; DB op only
│   │   └── rejections/                # constitution rejections, for audit
│   │       └── 004-tried-promotion.json
```

Each proposal's metadata file is structured:

```json
{
  "id": "dream-20260503-0330/001",
  "kind": "tool_composition",
  "title": "Add evening_routine composed tool",
  "rationale": "30+ instances of lock+lights+thermostat sequence in last 6 weeks; consolidating reduces gate prompts from 3 to 1.",
  "evidence": [
    {"session_id": "...", "ts": "2026-04-21T22:14:00", "matched": "lock+lights+thermostat"},
    "... 29 more ..."
  ],
  "constraints_declared": [
    "no protected paths",
    "composition only (no new primitives)",
    "must include unit test"
  ],
  "constraints_satisfied": true,
  "patch_file": "001-evening-routine.patch",
  "tests": {
    "added": ["tests/test_household.py::test_evening_routine"],
    "all_pass": true,
    "ran_at": "2026-05-03T03:42:11Z"
  },
  "estimated_blast_radius": "low",
  "implementer_model": "claude-sonnet-4-6",
  "dream_model": "claude-opus-4-7",
  "approval_state": "pending"
}
```

Every field exists for a review reason. The evidence list lets you spot-check the pattern claim. The constraints list shows what the constitution checked. The patch file is `git apply`-able as-is. The implementer transcript is recoverable if you want to see how the patch got built.

---

## 7. Review Flow

The morning interaction surfaces pending proposals — but with more weight than M9's `question_pending` items, because these change behavior, not just memory.

```
Top of morning brief:

I worked on three things overnight. None applied yet:

[001] new tool: evening_routine
      Composes lock + lights + thermostat (you've done this 30+ times).
      → just dream show 001
      → just dream approve 001
      → just dream reject 001 "reason"

[002] new skill: morning_brief_when_market_flat
      You've asked me to skip AAPL when pre-market is flat. Captured as a skill.
      → just dream show 002

[003] memory correction (no code change):
      Two memories disagree on Yi'an's school start. Propose archiving the older.
      → just dream show 003

I'll wait. Approve, reject, or skip — none of these touch production until you say.
```

The CLI surface:

```
just dream list                       # all pending proposals
just dream show <id>                  # full metadata + diff for review
just dream approve <id>               # apply patch, commit, reload service
just dream reject <id> "<reason>"     # archive with reason; dream agent reads on next cycle
just dream defer <id>                 # leave pending; max 7 days then auto-reject
just dream history                    # all approved/rejected proposals over time
just dream revert <commit>            # revert any approved proposal by commit hash
```

`just dream show` displays the structured metadata, the full diff with syntax highlighting, the test output, and the constraints check. For a tool composition, it also runs the test suite *again* in production (one more line of defense against sandbox/prod environment drift) before allowing approval.

`just dream approve` does, atomically:

```bash
# 1. Apply the patch
cd ~/agents && git apply ~/agents/dream-queue/.../proposals/001-evening-routine.patch

# 2. Run the constitution check one more time (third gate)
uv run python -m orchestrator.dream.constitution_check 001

# 3. Run the full test suite
uv run pytest

# 4. Commit with a structured message
git add -A
git commit -m "[dream-20260503] Add evening_routine composed tool

Proposed during dream-20260503-0330
Approved by: shupei at $(date -Iseconds)
Constraints: no protected paths, composition only, tests pass
Implementer: claude-sonnet-4-6
Dream: claude-opus-4-7

See dream-queue/2026-05-03/dream-20260503-0330/proposals/001-*"

# 5. Reload the FastAPI service so the new tool is registered
launchctl kickstart -k gui/$UID/com.shupei.agent.fastapi
```

Reverting a single approval is `git revert <commit>` plus the reload. `just dream revert <commit>` wraps both.

The result: every self-modification is one git commit, atomic, reviewable, revertible, and signed (in effect) by both the dream agent and your explicit approval.

---

## 8. Schema Additions

```sql
-- Dream cycle audit
CREATE TABLE dream_cycles (
    id                  TEXT PRIMARY KEY,             -- 'dream-20260503-0330'
    started_at          TIMESTAMP NOT NULL,
    ended_at            TIMESTAMP,
    sandbox_kind        TEXT NOT NULL,                -- 'filesystem' | 'container'
    proposals_emitted   INTEGER DEFAULT 0,
    rejections_logged   INTEGER DEFAULT 0,
    cost_usd            REAL,
    reflection_path     TEXT,
    status              TEXT NOT NULL                 -- 'running' | 'completed' | 'failed' | 'aborted'
);

-- Each proposal across its lifecycle
CREATE TABLE proposals (
    id                  TEXT PRIMARY KEY,             -- 'dream-20260503-0330/001'
    cycle_id            TEXT NOT NULL REFERENCES dream_cycles(id),
    kind                TEXT NOT NULL,                -- skill_addition | tool_composition | ...
    title               TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    artifact_dir        TEXT NOT NULL,                -- path to dream-queue subdirectory
    constraints_passed  BOOLEAN NOT NULL,
    tests_passed        BOOLEAN NOT NULL,
    state               TEXT NOT NULL,                -- pending | approved | rejected | deferred | expired
    decided_at          TIMESTAMP,
    decided_by          TEXT,                         -- principal user_id
    decision_reason     TEXT,                         -- rejection rationale
    applied_commit      TEXT,                         -- git commit hash if approved
    reverted_at         TIMESTAMP,                    -- nullable
    revert_commit       TEXT
);
CREATE INDEX idx_proposals_state ON proposals(state);
CREATE INDEX idx_proposals_cycle ON proposals(cycle_id);

-- Constitution rejections — never user-facing, but invaluable for debugging
CREATE TABLE constitution_rejections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            TEXT NOT NULL,
    declared_kind       TEXT,
    reason              TEXT NOT NULL,
    patch_summary       TEXT,
    layer               TEXT NOT NULL,                -- 'pre_implementer' | 'post_patch' | 'pre_apply'
    occurred_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

The `constitution_rejections` table is where you'll learn the most. Patterns of rejection ("the dream agent keeps trying to demote `lock_door` to L2") tell you whether the dream agent is well-calibrated or pushing limits in concerning ways. Read this table monthly; it's the closest thing the system has to an alignment dashboard.

---

## 9. Failure Modes (Observed and Anticipated)

This section is deliberately long because almost every failure mode of self-modifying agents has been seen in some form in the literature. Naming them lets you build mitigations before you encounter them.

**Proposal storm.** The dream agent generates 12 proposals every night, none individually wrong, but collectively overwhelming. *Mitigation:* hard cap of 5 proposals per cycle. If the dream agent has more candidates, it must prioritize and explain why these 5. If you find yourself with 5 pending most days, raise the bar inside the prompt ("only propose changes whose value is obvious within 30 seconds of review").

**Sycophantic patches.** The implementer notices the dream agent wants the patch to look good and produces something that passes constraints but isn't actually better. *Mitigation:* the dream agent reviews patches before emitting them, and is prompted explicitly to reject patches that satisfy constraints without addressing the rationale. The reflection.md writeup must articulate why this patch achieves the proposal's stated goal — vague reflections become a rejection signal during human review.

**Constraint evasion.** The implementer technically doesn't touch protected paths but achieves a protected effect (e.g., adds a new MCP server that bypasses gate logic, without modifying gating.py). *Mitigation:* the constitution validates *effects* not just paths. New MCP server registrations require a tool_composition kind (which has its own constraints); any patch that adds new top-level imports of `subprocess`, `socket`, `os.fork`, or similar is held for extra review. Periodic adversarial review by you: spend 30 minutes/month trying to find a patch that the dream agent could write to escape.

**Test gaming.** The implementer adds a test that "passes" without exercising the new behavior (e.g., asserts `True`). *Mitigation:* tests must include at least one assertion against an output the new code produces. Coverage on new lines must be ≥80%, checked at proposal-emit time. This is imperfect but raises the bar.

**Reflection theater.** The dream cycle generates impressive-sounding rationales that don't track reality ("I noticed Yunyan prefers concise responses on weekends" — no such pattern exists). *Mitigation:* every claim in the rationale must cite session IDs or memory IDs as evidence. Spot-check evidence during review for the first month; once you trust the citation discipline, spot-check less. If you can't spot-check the evidence, treat the rationale as fabricated.

**Approval drift.** You start approving everything because the proposals have been good for a month. Then a subtly bad one slips through. *Mitigation:* the CLI prints a randomized "are you sure" check on every 10th approval that requires re-typing a portion of the diff. Annoying enough to keep you actually reading, rare enough not to be friction.

**Test environment drift.** The patch passes tests in the sandbox but fails in production because of an environmental difference. *Mitigation:* the production-side `dream approve` re-runs the tests in production before applying. If they fail, the proposal is auto-rejected and you're told why.

**Compounding small mistakes.** Each individual proposal is fine; over six months, the accumulated effect is a system that's drifted. *Mitigation:* monthly review of the last 30 approved proposals as a batch. If something feels off about the trajectory, revert a stretch and reconsider. The git history makes this trivially possible.

**Hostile prompt injection via memory.** Someone (or some content) writes memory items that, when read by the dream agent, manipulate it into proposing dangerous changes. *Mitigation:* the dream agent's system prompt explicitly notes that memory content is data, not instruction. Memory items containing imperative language or claims about the system's own configuration get a warning flag. Sandbox isolation is the deeper defense: even if manipulated, the dream agent cannot directly affect production.

**Budget runaway.** A buggy dream loop burns through the API budget. *Mitigation:* the sandbox uses a separate API key with a hard monthly cap (e.g., $30). When hit, the cycle aborts cleanly. Production budget is unaffected.

**Sandbox escape.** The dream agent finds a way to write outside the sandbox. *Mitigation:* container isolation in M11.2 makes this much harder. If discovered, treat as a security incident: revoke the dream key, halt M11, audit recent proposals, postmortem before re-enabling.

---

## 10. Definition of Done

Three sub-stages, each with its own DoD. Don't move forward until the prior is solid.

### M11.1 — Filesystem sandbox + read-only dream cycle (≈3 days)

- Sandbox spawn/destroy works; produces consistent DB snapshot via VACUUM INTO
- Dream agent runs inside sandbox, reads memory + sessions, writes to artifacts dir
- Reflection.md and structured proposal metadata produced
- *No code-modifying proposals yet — this stage only emits memory_correction kind*
- Constitution validates memory-only proposals
- `just dream list/show/approve/reject` works for memory corrections
- Production approval applies the memory change via existing memory tools

DoD: A dream cycle that detects a real contradiction in memory, emits a memory_correction proposal with cited evidence, surfaces it in morning brief, gets approved, and applies cleanly. Audit log shows the full chain.

### M11.2 — Container sandbox + implementer + code proposals (≈5 days)

- Container image built and tested; network restricted to api.anthropic.com
- Implementer agent runs inside container, returns structured Patch
- Constitution checks all three layers (pre-implementer, post-patch, pre-apply)
- skill_addition and tool_composition kinds work end-to-end
- `just dream approve` applies patch, runs tests, commits, reloads service
- Revert flow tested: `just dream revert` cleanly undoes any approval

DoD: A real observed pattern (e.g., evening routine) becomes a tool_composition proposal, gets implemented by the implementer, passes the sandbox tests, surfaces in morning brief with full diff, gets approved, applies as a git commit, the new tool is available in the next session, and `just dream revert` cleanly removes it.

### M11.3 — Hardening (≈2 days)

- All failure modes from §9 have a mitigation in code or a documented monitoring approach
- Adversarial test suite: 10 scenarios where the dream agent is given prompts designed to elicit bad proposals, all rejected by constitution
- Monthly review CLI: `just dream audit` produces a report of the last 30 days of proposals, approval rates, rejection reasons, constitution_rejections
- Documentation in README covers the full flow, including how to add new proposal kinds (it should require code review on the constitution itself, by definition)

DoD: After two weeks of M11.2 running, you've reviewed 10+ proposals, the system feels predictable, and the audit report makes sense.

---

## 11. What Stays Forbidden Even With M11

Some things the agent cannot modify about itself, ever, even with this infrastructure:

- **The constitution.** Every other limit is enforced by the constitution; the constitution itself can only be changed by hand, with thought, ideally with another set of eyes.
- **Auth and gate logic.** Identity, token verification, tier classification, profile policy. These are the layer that protects the household from the agent; the agent doesn't get to shape them.
- **Memory schema.** Tables, columns, indices. The dream agent can write rows and propose corrections. It cannot propose schema migrations.
- **The orchestrator loop.** The shape of how sessions run, how hooks fire, how messages flow.
- **Anything in `data/`.** No proposal touches the encrypted DB file directly. Memory operations go through the memory tool layer.
- **`config/roles.toml`.** Role definitions are constitutional. Adding a role, changing capabilities, modifying ceiling tiers — all hand-edited.

If you find yourself wanting one of these to be self-modifiable, the right move is almost always to add a *configuration knob* (in a non-protected file) that the agent can adjust, while the underlying mechanism stays constitutional. E.g., the agent shouldn't propose changes to gate logic, but the agent *can* propose tier demotions of specific tools (which is a config change, not a logic change).

---

## 12. Open Questions

These need real-world data to resolve:

- **How often will the dream cycle have anything worth proposing?** My guess: 2-3 useful proposals per week after the first month, dropping to 1 per week as the agent stabilizes. If it's 0 per week, the prompt is too conservative; if it's 10 per week, too liberal.
- **Should the dream agent see its own previous rejections?** Almost certainly yes — a repeated rejection should make it less likely to propose similar things. But there's a failure mode where it learns "Shupei rejects ambitious proposals on Mondays" and adapts in degenerate ways. Start by including last 30 days of rejections; revisit.
- **Is the implementer worth the complexity vs. having the dream agent author patches directly?** I think yes, because the cognitive load of "design and implement" in one session degrades both. But it's testable — run a side-by-side for a week, see if the unified version is faster without losing quality.
- **Should approved proposals ever auto-merge after a long enough quiet period?** No, at least initially. Even "approve in 7 days if no objection" creates a complacency surface. Manual approval, always.
- **Multi-user proposals.** A proposal that affects family-scope behavior (e.g., a new shared skill) — does Yunyan get to weigh in? Probably not for v1; admin-only approval for code changes. Revisit when M11 has been stable for a quarter.

---

## 13. Sequencing Note

Do not start M11 until M9 has been running cleanly for at least 4 weeks. The dream cycle's value depends entirely on having real consolidation outputs to reason over. Without weeks of M9 data, the dream agent is making proposals from sparse signal, which is exactly the regime where it's most likely to get things wrong.

When M11 starts, do M11.1 first and live with it for a week before M11.2. The memory_correction-only stage is genuinely useful on its own and is a low-stakes way to exercise the artifact/review/approve pipeline. By the time you turn on code proposals, every other piece of the flow is already tested.

If at any point during M11 you find yourself approving proposals without really reading them: stop, drop a `just dream pause` flag that disables the cycle, and re-evaluate. The whole thing only works if the human review is real.

---

*End of M11 addendum. To be read alongside `family_agent_design.md` and `M9_M10_consolidation_and_embodiment.md`.*

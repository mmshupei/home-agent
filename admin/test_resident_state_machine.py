"""Smoke test for orchestrator/resident.py state machine.

Doesn't spin a real ClaudeSDKClient subprocess. Instead, monkey-patches
the SDK class with a fake that lets us drive the timing of `query`,
`receive_response`, and `interrupt`. We verify:

1. A normal turn: submit() returns the full reply.
2. Debounce: two submits within DEBOUNCE_SEC merge; both futures get
   the same reply.
3. Steering: a submit during streaming triggers interrupt, the original
   future resolves to None, the steering submit's future resolves with
   the new reply.

Run with: .venv/bin/python admin/test_resident_state_machine.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import ResultMessage  # for type duck-typing


# ---------------------------------------------------------------------------
# Fake ClaudeSDKClient
# ---------------------------------------------------------------------------


class _FakeAssistantText:
    def __init__(self, text: str):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, text: str):
        # Match real SDK: msg.content is a list of blocks; each block may be
        # a TextBlock with .text. The resident checks isinstance(block,
        # TextBlock), so we must use the real TextBlock class here.
        from claude_agent_sdk import TextBlock
        self.content = [TextBlock(text=text)]


class _FakeResultMessage:
    """Subclass the real ResultMessage so isinstance checks pass.

    Real ResultMessage has many fields; we only set what the resident
    reads. The duration_* fields are required for the @dataclass init."""

    def __new__(cls, *, result: str, total_cost_usd=None, usage=None):
        from claude_agent_sdk import ResultMessage as RM
        # ResultMessage is a dataclass — instantiate with sensible defaults.
        return RM(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="fake",
            total_cost_usd=total_cost_usd,
            usage=usage or {"input_tokens": 0, "output_tokens": 0},
            result=result,
        )


class FakeClient:
    """Stands in for ClaudeSDKClient. Each `query()` call must be paired
    with `feed_response(text)` (called from the test) which sets up the
    next receive_response() iteration.

    `interrupt()` causes the in-flight receive_response generator to
    yield a ResultMessage with `result='(interrupted partial)'` and stop.
    """

    def __init__(self):
        # Queue of completion-text-or-interrupt for the next stream.
        self._next_response_event = asyncio.Event()
        self._next_response_text: str | None = None
        self._stream_active = False
        self._interrupted = False
        self.queries: list[str] = []
        self.interrupted_count = 0

    def feed_response(self, text: str) -> None:
        self._next_response_text = text
        self._next_response_event.set()

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)
        # Reset for the new stream
        self._next_response_event.clear()
        self._next_response_text = None
        self._interrupted = False
        self._stream_active = True

    async def interrupt(self) -> None:
        self.interrupted_count += 1
        self._interrupted = True
        # Free the receive_response loop with a synthetic ResultMessage.
        self._next_response_text = "(interrupted)"
        self._next_response_event.set()

    async def receive_response(self):
        # Wait until either feed_response or interrupt fires.
        await self._next_response_event.wait()
        text = self._next_response_text or ""
        if self._interrupted:
            yield _FakeResultMessage(result=text)
        else:
            yield _FakeAssistantMessage(text)
            yield _FakeResultMessage(result=text)
        self._stream_active = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Patch + run
# ---------------------------------------------------------------------------


async def main():
    # Stub out heavy bits so we don't hit the real DB / file system.
    from orchestrator import resident

    fake_clients: list[FakeClient] = []

    def _factory(options):
        c = FakeClient()
        fake_clients.append(c)
        return c

    # Monkey-patch the ClaudeSDKClient symbol used inside resident.py.
    resident.ClaudeSDKClient = _factory  # type: ignore[assignment]

    # Stub Session, episodes, critic, build_options, gate so we don't
    # need DB/filesystem.
    class _FakeSession:
        id = "fake-session"
        def record(self, msg): pass
        def log_intent(self, *a, **k): return 1
        def log_decision(self, *a, **k): pass
        def finalize(self, **k): pass

    class _SessionFactory:
        @classmethod
        def new(cls, *, task, profile, principal):
            return _FakeSession()

    resident.Session = _SessionFactory  # type: ignore[assignment]

    class _Episodes:
        @staticmethod
        def start(*a, **k): return "fake-episode"
        @staticmethod
        def close(*a, **k): pass
    resident.episodes = _Episodes  # type: ignore[assignment]

    class _Critic:
        @staticmethod
        def schedule(s): pass
    resident.critic = _Critic  # type: ignore[assignment]

    # build_options is real but constructs MCP servers + reads config.
    # Replace with a stub that returns a dummy options object — FakeClient
    # ignores it.
    resident.build_options = lambda **kwargs: object()
    resident.compose_static_system_prompt = lambda d, p: ""
    resident.compose_per_turn_header = lambda task, principal: ""
    resident.build_gate_hook = lambda *a, **k: (lambda *a, **k: None)
    resident.get_profile = lambda name: type("P", (), {"policy": {}, "domains": []})()

    # Build a Principal stub
    from orchestrator.auth import Principal
    p = Principal(user_id="test-user", name="Test", role="admin", token_label="test")

    # ---- Test 1: simple turn ----
    print("--- test 1: simple turn ---")
    agent = resident.ResidentAgent(principal=p, surface="test", profile="mobile")
    await agent.start()
    # Wait for client to be set up
    for _ in range(50):
        if fake_clients:
            break
        await asyncio.sleep(0.02)
    assert fake_clients, "client never initialized"

    async def feed_after(text: str, delay: float):
        await asyncio.sleep(delay)
        # Find current client and feed it.
        fake_clients[-1].feed_response(text)

    feeder = asyncio.create_task(feed_after("hello back", delay=0.4))
    reply = await agent.submit("hello")
    await feeder
    assert reply == "hello back", f"expected 'hello back', got {reply!r}"
    print(f"  ok: reply={reply!r}")
    await agent.stop()

    # ---- Test 2: debounce merges two quick messages ----
    print("--- test 2: debounce ---")
    fake_clients.clear()
    agent = resident.ResidentAgent(principal=p, surface="test", profile="mobile")
    await agent.start()
    for _ in range(50):
        if fake_clients:
            break
        await asyncio.sleep(0.02)

    # Two submits ~50ms apart should both end up in one query.
    async def submit_two():
        f1 = asyncio.create_task(agent.submit("part 1"))
        await asyncio.sleep(0.05)
        f2 = asyncio.create_task(agent.submit("part 2"))
        return f1, f2

    f1, f2 = await submit_two()
    # Give debounce window a chance to expire (250ms) and feed reply.
    await asyncio.sleep(0.4)
    fake_clients[-1].feed_response("merged reply")

    r1, r2 = await asyncio.gather(f1, f2)
    assert r1 == "merged reply", f"f1 got {r1!r}"
    assert r2 == "merged reply", f"f2 got {r2!r}"
    # Only one query should have been issued.
    assert len(fake_clients[-1].queries) == 1, f"queries: {fake_clients[-1].queries}"
    # Merged text should contain both parts.
    q = fake_clients[-1].queries[0]
    assert "part 1" in q and "part 2" in q, f"merged query: {q!r}"
    print(f"  ok: both futures got merged reply, single query, queries={fake_clients[-1].queries}")
    await agent.stop()

    # ---- Test 3: steering interrupts in-flight stream ----
    print("--- test 3: steering ---")
    fake_clients.clear()
    agent = resident.ResidentAgent(principal=p, surface="test", profile="mobile")
    await agent.start()
    for _ in range(50):
        if fake_clients:
            break
        await asyncio.sleep(0.02)

    # Submit #1 starts the stream but never gets a feed_response —
    # so it sits with state="streaming".
    f1 = asyncio.create_task(agent.submit("first message"))
    # Wait until state is streaming (past debounce window).
    for _ in range(60):
        if agent.state == "streaming":
            break
        await asyncio.sleep(0.02)
    assert agent.state == "streaming", f"state never reached streaming, was {agent.state}"

    # Submit #2 should trigger interrupt + new turn.
    f2 = asyncio.create_task(agent.submit("steer to this"))
    # Give the interrupt + receive_response drain time, then feed the
    # NEW client's response. (After interrupt the same client gets a new
    # query, not a new client. Wait for the second query.)
    for _ in range(100):
        if len(fake_clients[-1].queries) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(fake_clients[-1].queries) >= 2, (
        f"second query not issued; queries={fake_clients[-1].queries} "
        f"interrupted_count={fake_clients[-1].interrupted_count}"
    )
    fake_clients[-1].feed_response("steered reply")

    r1, r2 = await asyncio.gather(f1, f2)
    assert r1 is None, f"f1 (interrupted) should be None, got {r1!r}"
    assert r2 == "steered reply", f"f2 got {r2!r}"
    assert fake_clients[-1].interrupted_count == 1, (
        f"expected 1 interrupt, got {fake_clients[-1].interrupted_count}"
    )
    # Second query should contain the steered text.
    assert "steer to this" in fake_clients[-1].queries[1]
    print(f"  ok: f1=None (interrupted), f2={r2!r}, interrupts={fake_clients[-1].interrupted_count}")
    await agent.stop()

    print("\nAll three smoke tests passed.")


if __name__ == "__main__":
    asyncio.run(main())

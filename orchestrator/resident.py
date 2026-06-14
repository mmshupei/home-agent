"""Resident agent — one long-lived ClaudeSDKClient per (surface, principal).

Replaces the one-shot loop.run() invocation model on Telegram so that
mid-stream steering works: when a new user message arrives while the
client is generating, we call client.interrupt() and re-query with the
new text. The model keeps the truncated assistant turn in conversation
history and reads the new user turn as a steer.

Design notes:

- The SDK's ClaudeSDKClient is pinned to its async context (see
  client.py: "Caveat: As of v0.0.20..."). The client is created and
  driven entirely inside the resident's background task; the only
  cross-task call is `interrupt()`, which the SDK supports for exactly
  this use case (sends a control-protocol message via the transport).

- Per-turn session+episode lifecycle. Each user submission opens its
  own Session and episode. The gate hook resolves the *current*
  session via a zero-arg callable (see gating.build_gate_hook), so we
  can swap sessions between turns without rebuilding hooks or MCP
  servers.

- Steering. submit() puts (text, future) into an inbox queue. If the
  resident task is currently streaming, submit() also calls
  client.interrupt() so the in-flight turn ends. The streaming loop
  notices state == "interrupted", finishes draining receive_response,
  closes the episode with an interrupted=True tag, and proceeds to
  the next inbox item. The interrupted submission's future resolves
  to None — the bridge skips sending a reply for None and lets the
  steering submission's own future deliver the actual reply.

- Debounce. When a submission is pulled from the inbox we wait up to
  DEBOUNCE_SEC for additional submissions and merge their text. Two
  thumb-typed messages in a row get treated as one steered intent
  rather than as an interrupt round-trip.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from . import critic, episodes
from .auth import Principal
from .gating import build_gate_hook, get_profile
from .loop import (
    DEFAULT_DOMAINS,
    build_options,
    compose_per_turn_header,
    compose_static_system_prompt,
)
from .prompts import prompt_telegram, prompt_terminal
from .session import Session

# Debounce window for merging multiple inbound messages from the same user
# into one turn. Long enough to catch a quick "...and also..." follow-up,
# short enough that single messages don't feel laggy.
DEBOUNCE_SEC = 0.25

# How long to wait for the background task to drain on stop().
STOP_TIMEOUT_SEC = 10.0


class _Submission:
    """A user message awaiting reply. The future resolves with the reply
    text once the turn ends; or with None if the submission was superseded
    by a later steering interrupt (the steering submission's future is
    what carries the actual reply for the merged intent)."""

    __slots__ = ("text", "future")

    def __init__(self, text: str, future: asyncio.Future):
        self.text = text
        self.future = future


class ResidentAgent:
    """Long-lived agent owning one ClaudeSDKClient for a (surface, principal).

    Lifecycle:
        agent = ResidentAgent(principal=p, surface="telegram", profile="mobile")
        await agent.start()
        reply = await agent.submit("hello")    # may be None on steer
        ...
        await agent.stop()

    Thread-safety: not thread-safe; designed for a single asyncio event
    loop. submit() is safe to call from any coroutine on that loop.
    """

    def __init__(
        self,
        *,
        principal: Principal,
        surface: str = "telegram",
        profile: str = "mobile",
        model: str = "claude-fable-5",
        domains: Optional[list[str]] = None,
    ):
        self.principal = principal
        self.surface = surface
        self.profile = profile
        self.model = model
        self.domains = domains or DEFAULT_DOMAINS

        self._inbox: asyncio.Queue[_Submission] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._client: Optional[ClaudeSDKClient] = None

        # Mutable holder so the gate hook (closed over `self`) can find
        # the current turn's session.
        self._current_session: Optional[Session] = None
        # State machine: idle | streaming | interrupted | stopping
        self._state: str = "idle"
        # Futures of submissions whose text was merged into the current
        # turn — they all resolve together when the turn ends.
        self._turn_futures: list[asyncio.Future] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Launch the background task that owns the ClaudeSDKClient.

        Returns once the task is created — the client itself connects
        asynchronously inside _run(). The first submit() will simply
        wait in the inbox until the client is ready."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name=f"resident:{self.surface}:{self.principal.user_id}",
        )

    async def stop(self) -> None:
        """Signal the background task to drain and exit, then await it."""
        if self._task is None:
            return
        self._stop_event.set()
        # Sentinel so a blocked inbox.get() unblocks.
        loop = asyncio.get_running_loop()
        await self._inbox.put(_Submission(text="", future=loop.create_future()))
        try:
            await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            print(
                f"[resident] {self.surface}:{self.principal.user_id} "
                f"did not stop in {STOP_TIMEOUT_SEC}s; cancelling"
            )
            self._task.cancel()
        finally:
            self._task = None

    async def submit(self, text: str) -> Optional[str]:
        """Queue a user message. Returns the reply text once ready, or
        None if this submission was superseded by a later steering
        message — in that case the caller should send no reply (the
        steering submission's own returned reply addresses the merged
        content)."""
        if self._task is None or self._task.done():
            raise RuntimeError(
                f"resident not running for {self.surface}:{self.principal.user_id}"
            )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._inbox.put(_Submission(text=text, future=fut))

        # If we're mid-stream, interrupt so this new message is treated
        # as a steer. Update state first so a flurry of concurrent
        # submits doesn't double-call interrupt().
        if self._state == "streaming":
            self._state = "interrupted"
            client = self._client
            if client is not None:
                try:
                    await client.interrupt()
                except Exception as e:
                    # Interrupt can race with stream end; not fatal.
                    print(f"[resident] interrupt error (non-fatal): {e!r}")

        return await fut

    # ------------------------------------------------------------------
    # Background task
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        profile_obj = get_profile(self.profile)

        # The session_provider closure lets us swap sessions per turn
        # without rebuilding the gate hook.
        gate = build_gate_hook(
            lambda: self._current_session,
            profile_obj,
            self.principal,
            prompt_cli=prompt_terminal,
            prompt_push=prompt_telegram,
        )

        options = build_options(
            principal=self.principal,
            system_prompt=compose_static_system_prompt(
                self.domains, self.principal
            ),
            gate=gate,
            model=self.model,
            source=self.surface,
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client
                print(
                    f"[resident] up · {self.surface}:{self.principal.user_id} "
                    f"profile={self.profile} model={self.model}"
                )
                while not self._stop_event.is_set():
                    sub = await self._inbox.get()
                    if self._stop_event.is_set():
                        # Drain remaining submissions so callers don't hang.
                        self._resolve_remaining_with_none(first=sub)
                        return

                    merged_text, merged_subs = await self._debounce_merge(sub)
                    self._turn_futures = [s.future for s in merged_subs]

                    try:
                        await self._handle_turn(client, merged_text)
                    except Exception as e:
                        # Turn-level error: never let the resident task die
                        # without resolving futures.
                        err = f"(agent error: {type(e).__name__}: {e})"
                        print(f"[resident] turn error: {e!r}")
                        for f in self._turn_futures:
                            if not f.done():
                                f.set_result(err)
                        self._turn_futures = []
                        self._current_session = None
                        self._state = "idle"
        except Exception as e:
            print(
                f"[resident] FATAL {self.surface}:{self.principal.user_id}: "
                f"{type(e).__name__}: {e}"
            )
            # Resolve any in-flight futures so callers don't hang.
            for f in self._turn_futures:
                if not f.done():
                    f.set_result(f"(agent fatal: {e})")
            self._turn_futures = []
            raise
        finally:
            self._client = None
            self._state = "idle"
            print(
                f"[resident] down · {self.surface}:{self.principal.user_id}"
            )

    def _resolve_remaining_with_none(self, first: _Submission) -> None:
        if not first.future.done():
            first.future.set_result(None)
        while not self._inbox.empty():
            try:
                s = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not s.future.done():
                s.future.set_result(None)

    async def _debounce_merge(
        self, first: _Submission
    ) -> tuple[str, list[_Submission]]:
        """After receiving the first submission, wait up to DEBOUNCE_SEC
        for further submissions and merge them. Two messages from the
        same user within the window become one turn."""
        subs = [first]
        deadline = time.monotonic() + DEBOUNCE_SEC
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                more = await asyncio.wait_for(
                    self._inbox.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                break
            subs.append(more)
        # Drain anything that arrived but didn't trigger debounce.
        while not self._inbox.empty():
            try:
                subs.append(self._inbox.get_nowait())
            except asyncio.QueueEmpty:
                break
        if len(subs) == 1:
            return first.text, subs
        merged = "\n\n".join(s.text for s in subs)
        return merged, subs

    async def _handle_turn(
        self, client: ClaudeSDKClient, user_text: str
    ) -> None:
        """Run one user→reply turn against the resident client."""
        # Per-turn session + episode. The gate hook reads
        # self._current_session via the session_provider closure.
        session = Session.new(
            task=user_text, profile=self.profile, principal=self.principal,
        )
        self._current_session = session
        episode_id = episodes.start(
            source=self.surface,
            principal=self.principal,
            session_id=session.id,
        )

        header = compose_per_turn_header(user_text, self.principal)
        full_input = (header + "\n\n" + user_text) if header else user_text

        self._state = "streaming"
        final_text = ""
        token_count: Optional[int] = None
        cost_usd: Optional[float] = None

        try:
            await client.query(full_input)
            async for msg in client.receive_response():
                session.record(msg)
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final_text += block.text
                elif isinstance(msg, ResultMessage):
                    cost_usd = getattr(msg, "total_cost_usd", None)
                    usage = getattr(msg, "usage", None) or {}
                    if isinstance(usage, dict):
                        token_count = (
                            (usage.get("input_tokens") or 0)
                            + (usage.get("output_tokens") or 0)
                        ) or None
                    if getattr(msg, "result", None):
                        final_text = msg.result
        finally:
            session.finalize(
                final_message=final_text,
                token_count=token_count,
                cost_usd=cost_usd,
            )
            try:
                interrupted = self._state == "interrupted"
                tag = " (interrupted)" if interrupted else ""
                transcript = f"USER: {user_text}\n\nAGENT{tag}: {final_text}"
                episodes.close(
                    episode_id,
                    transcript=transcript,
                    summary=(final_text[:200] if final_text else None),
                    affect={
                        "cost_usd": cost_usd,
                        "token_count": token_count,
                        "profile": self.profile,
                        "interrupted": interrupted,
                    },
                )
            except Exception as e:
                print(f"[resident] episode close error: {e!r}")
            critic.schedule(session)

        was_interrupted = self._state == "interrupted"
        self._state = "idle"
        self._current_session = None

        if was_interrupted:
            # Don't deliver the partial reply — the steering submission's
            # future will deliver the reply that addresses the merged
            # intent.
            for f in self._turn_futures:
                if not f.done():
                    f.set_result(None)
        else:
            reply = final_text or "(no response)"
            for f in self._turn_futures:
                if not f.done():
                    f.set_result(reply)
        self._turn_futures = []


# ---------------------------------------------------------------------------
# Per-process registry
# ---------------------------------------------------------------------------
# Bridges (Telegram, iMessage, ...) share a single registry. One resident per
# (surface, principal). Callers should `await get_resident(...)` to either
# fetch an existing instance or start a new one; `await stop_all()` on
# shutdown.

_REGISTRY: dict[tuple[str, str], ResidentAgent] = {}
_REGISTRY_LOCK: Optional[asyncio.Lock] = None


def _registry_lock() -> asyncio.Lock:
    global _REGISTRY_LOCK
    if _REGISTRY_LOCK is None:
        _REGISTRY_LOCK = asyncio.Lock()
    return _REGISTRY_LOCK


async def get_resident(
    *,
    principal: Principal,
    surface: str,
    profile: str = "mobile",
    model: str = "claude-fable-5",
    domains: Optional[list[str]] = None,
) -> ResidentAgent:
    """Get-or-create the resident for (surface, principal). Idempotent.
    A dead resident (task crashed) is replaced."""
    key = (surface, principal.user_id)
    async with _registry_lock():
        existing = _REGISTRY.get(key)
        if existing is not None and existing.alive:
            return existing
        # Replace any dead entry.
        agent = ResidentAgent(
            principal=principal,
            surface=surface,
            profile=profile,
            model=model,
            domains=domains,
        )
        await agent.start()
        _REGISTRY[key] = agent
        return agent


async def stop_all() -> None:
    """Stop every resident in the registry. Best-effort; logs failures."""
    async with _registry_lock():
        agents = list(_REGISTRY.values())
        _REGISTRY.clear()
    for a in agents:
        try:
            await a.stop()
        except Exception as e:
            print(
                f"[resident] stop error for "
                f"{a.surface}:{a.principal.user_id}: {e!r}"
            )

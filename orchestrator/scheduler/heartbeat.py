"""Heartbeat asyncio task.

Lives inside the long-running ``triggers/telegram_bot.py`` process. Every
HEARTBEAT_INTERVAL_SEC the gate scans ``scheduled_actions`` (pure Python,
no LLM cost). When something is due, it hands off to
``orchestrator.scheduler.runner.run_due_actions``.

Designed to be crash-resilient: every iteration is wrapped in try/except,
so a single bad row or a transient SDK failure can't kill the loop.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import store
from .runner import run_due_actions

# Tick cadence. 15 minutes is the locked default — balance between
# responsiveness ("21:30 daily" can fire up to ~15 min late) and idle cost
# (no LLM cost on idle ticks; only the gate scan runs).
HEARTBEAT_INTERVAL_SEC = 15 * 60
HEARTBEAT_JITTER_SEC = 30  # tiny ± to avoid lockstep across coincidental restarts


class Heartbeat:
    """Periodic gate-and-dispatch task.

    Construct with an ``on_notify`` callback that takes (owner_user_id, text)
    and pushes the text to that user via Telegram out-of-band. ``start()``
    spawns the asyncio task; ``stop()`` cancels it cleanly. Idempotent on
    both ends.
    """

    def __init__(
        self,
        *,
        on_notify: Callable[[str, str], Awaitable[None]],
        interval_sec: float = HEARTBEAT_INTERVAL_SEC,
        jitter_sec: float = HEARTBEAT_JITTER_SEC,
        model: str = "claude-sonnet-4-5",
    ) -> None:
        self._on_notify = on_notify
        self._interval = float(interval_sec)
        self._jitter = float(jitter_sec)
        self._model = model
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="scheduler-heartbeat")
        print(
            f"[heartbeat] started — interval={int(self._interval)}s "
            f"jitter=±{int(self._jitter)}s model={self._model}"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None

    async def fire_now(self, name: str | None = None) -> int:
        """Run any actions due *as if* the heartbeat just ticked. If ``name``
        is provided, only consider that single action (still subject to the
        usual due-detection — to truly bypass it, an admin tool would need
        to bump last_fired_at backward; for now schedule__fire_now uses the
        unconditional path below).

        Returns the number of actions handed to the runner.
        """
        return await self._tick_once(force_name=name)

    async def force_fire(self, name: str) -> bool:
        """Unconditional fire of one named action: bypass due-detection.
        Used by the schedule__fire_now tool surface for smoke testing.
        Returns True iff the named action exists and was enabled.
        """
        action = store.get(name)
        if action is None or not action.enabled:
            return False
        # Use 'now' as the expected_prev_fire so mark_fired's advisory lock
        # accepts our update (last_fired_at < now is essentially always true
        # for a manual fire-now). The runner's mark_fired call will handle
        # the lock atomically.
        now_utc = datetime.now(timezone.utc)
        await run_due_actions(
            due=[(action, now_utc)],
            on_notify=self._on_notify,
            model=self._model,
        )
        return True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _tick_once(self, *, force_name: str | None = None) -> int:
        now_utc = datetime.now(timezone.utc)
        try:
            due = store.due_actions(now_utc)
        except Exception as e:
            print(f"[heartbeat] due_actions scan error: {e!r}")
            return 0
        if force_name:
            due = [pair for pair in due if pair[0].name == force_name]
        if not due:
            return 0
        names = [a.name for a, _ in due]
        print(f"[heartbeat] {len(due)} action(s) due: {names}")
        try:
            await run_due_actions(
                due=due, on_notify=self._on_notify, model=self._model
            )
        except Exception as e:
            print(f"[heartbeat] dispatch error (non-fatal): {e!r}")
        return len(due)

    async def _wait_with_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        # First tick: small startup delay so we don't fire-storm during the
        # bot's initial post_init (greeting, drift check, etc.).
        await self._wait_with_stop(min(30.0, self._interval))
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception as e:
                print(f"[heartbeat] tick error (non-fatal): {e!r}")
            if self._stop.is_set():
                break
            sleep_for = self._interval + random.uniform(-self._jitter, self._jitter)
            await self._wait_with_stop(max(1.0, sleep_for))

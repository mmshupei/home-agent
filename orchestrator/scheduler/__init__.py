"""Pulse-driven scheduler.

A long-lived asyncio heartbeat task (started in triggers/telegram_bot.py)
ticks every HEARTBEAT_INTERVAL_SEC, scans ``scheduled_actions`` for rows
that are due, and hands each one to ``runner.run_due_actions`` which spawns
an isolated ClaudeSDKClient subagent. Subagents post a short notification
message to ``owner_user`` via Telegram out-of-band — they do NOT submit
into the user's resident agent, so the heartbeat never steers attention
from a live conversation.

Public surface:
- ``store``: CRUD + ``due_actions`` (pure-Python, no asyncio, no LLM).
- ``runner``: subagent invocation per due action.
- ``heartbeat``: the asyncio tick loop.
"""

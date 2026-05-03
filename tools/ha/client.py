"""Thin Home Assistant REST client.

Only call when HA_URL + HA_TOKEN are both set. is_configured() lets callers
skip wiring the tools when HA isn't present.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx


def base_url() -> Optional[str]:
    return os.environ.get("HA_URL") or None


def token() -> Optional[str]:
    return os.environ.get("HA_TOKEN") or None


def is_configured() -> bool:
    return bool(base_url() and token())


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
    }


async def get_states() -> list[dict]:
    """Snapshot of every entity. L1."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{base_url()}/api/states", headers=_headers())
        r.raise_for_status()
        return r.json()


async def get_state(entity_id: str) -> Optional[dict]:
    """Single entity's state + attributes. L1."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{base_url()}/api/states/{entity_id}", headers=_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def call_service(domain: str, service: str, **service_data: Any) -> dict:
    """Call a service. Tier depends on the domain (light → L2, lock → L3).
    The gate enforces this via the tool name; this client just speaks HTTP.
    """
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(
            f"{base_url()}/api/services/{domain}/{service}",
            headers=_headers(),
            json=service_data,
        )
        r.raise_for_status()
        return {"ok": True, "result": r.json()}

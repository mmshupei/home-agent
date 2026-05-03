"""SDK MCP wrappers for Home Assistant. Returns an empty tools list when HA
isn't configured, so the orchestrator can mount the server unconditionally.

Naming matches config/tiers.toml regexes:
  ha__get_state                       L1
  ha__list_states                     L1
  ha__call_service__light|switch|...  L2
  ha__call_service__lock|cover|...    L3
"""
from __future__ import annotations

import json

from claude_agent_sdk import tool

from tools.ha import client as ha


@tool(
    "ha__get_state",
    "Get the current state of a Home Assistant entity (e.g. 'light.kitchen', "
    "'climate.main', 'lock.front_door'). Returns state + attributes JSON.",
    {"entity_id": str},
)
async def ha_get_state(args):
    if not ha.is_configured():
        return {
            "is_error": True,
            "content": [{"type": "text", "text": "Home Assistant not configured (HA_URL/HA_TOKEN unset)"}],
        }
    s = await ha.get_state(args["entity_id"])
    if s is None:
        return {"is_error": True, "content": [{"type": "text", "text": f"unknown entity: {args['entity_id']}"}]}
    return {"content": [{"type": "text", "text": json.dumps(s, indent=2)}]}


@tool(
    "ha__list_states",
    "List all entities and their current states. Use sparingly — output can be large.",
    {},
)
async def ha_list_states(args):
    if not ha.is_configured():
        return {
            "is_error": True,
            "content": [{"type": "text", "text": "Home Assistant not configured"}],
        }
    rows = await ha.get_states()
    summary = [{"entity_id": r["entity_id"], "state": r["state"]} for r in rows]
    return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}]}


# We expose one generic call tool per coarse domain group so tier classification
# (L2 vs L3) routes correctly. See config/tiers.toml.

async def _service_call(args, domain_label: str):
    if not ha.is_configured():
        return {
            "is_error": True,
            "content": [{"type": "text", "text": "Home Assistant not configured"}],
        }
    domain = args["domain"]
    service = args["service"]
    payload = args.get("data") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    res = await ha.call_service(domain, service, **payload)
    return {"content": [{"type": "text", "text": json.dumps(res, default=str)}]}


@tool(
    "ha__call_service__light",
    "Call an HA service in the light/switch/scene/media_player family (L2 — "
    "reversible). domain is e.g. 'light', service is 'turn_on'/'turn_off'/'toggle', "
    "data is the service payload (entity_id, brightness_pct, etc.).",
    {"domain": str, "service": str, "data": dict},
)
async def ha_call_light(args):
    return await _service_call(args, "light")


@tool(
    "ha__call_service__lock",
    "Call an HA service in the lock/cover/alarm/garage family (L3 — physical, "
    "irreversible). Always confirm with the user before calling.",
    {"domain": str, "service": str, "data": dict},
)
async def ha_call_lock(args):
    return await _service_call(args, "lock")


def ha_tools():
    """Return the HA tool list. Always returns the schemas (so the agent knows
    they exist) — the tools themselves error out if HA isn't configured."""
    return [ha_get_state, ha_list_states, ha_call_light, ha_call_lock]

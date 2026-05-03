"""Curated household routines.

These bundle multiple raw HA / AppleScript calls under one approval prompt.
Per the design: raw service calls remain available for novel requests, but
routines are the primary surface.

Every routine that touches HA must check ha.is_configured() first and degrade
gracefully when HA isn't set up — at minimum reporting which steps would have
fired so the user knows what's missing.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from claude_agent_sdk import tool

from tools.applescript import calendar as cal_shim
from tools.finance import lots as ledger
from tools.ha import client as ha

PACIFIC = ZoneInfo("America/Los_Angeles")


@tool(
    "good_night",
    "Bedtime routine: lock front door, all lights off, thermostat to 68°F. "
    "L2 in tier policy because it bundles reversible-state HA calls; the door "
    "lock is the L3 part and gets surfaced in the prompt. Skips HA steps if "
    "HA isn't configured and reports what was skipped.",
    {},
)
async def good_night(args):
    if not ha.is_configured():
        return {
            "content": [
                {
                    "type": "text",
                    "text": "good_night skipped — Home Assistant not configured. "
                    "Would have: lock front door, lights off, climate to 68°F.",
                }
            ]
        }
    steps: list[str] = []
    try:
        await ha.call_service("light", "turn_off", entity_id="all")
        steps.append("lights off")
    except Exception as e:
        steps.append(f"lights off FAILED: {e}")
    try:
        await ha.call_service("climate", "set_temperature",
                              entity_id="climate.main", temperature=68)
        steps.append("climate → 68°F")
    except Exception as e:
        steps.append(f"climate FAILED: {e}")
    try:
        await ha.call_service("lock", "lock", entity_id="lock.front_door")
        steps.append("front door locked")
    except Exception as e:
        steps.append(f"front door lock FAILED: {e}")
    return {"content": [{"type": "text", "text": " · ".join(steps) + ". Sleep well."}]}


@tool(
    "leaving_home",
    "Departure routine: lights off, thermostat to away mode, lock front door.",
    {},
)
async def leaving_home(args):
    if not ha.is_configured():
        return {
            "content": [
                {
                    "type": "text",
                    "text": "leaving_home skipped — Home Assistant not configured.",
                }
            ]
        }
    steps: list[str] = []
    try:
        await ha.call_service("light", "turn_off", entity_id="all")
        steps.append("lights off")
    except Exception as e:
        steps.append(f"lights FAILED: {e}")
    try:
        await ha.call_service("climate", "set_preset_mode",
                              entity_id="climate.main", preset_mode="away")
        steps.append("climate → away")
    except Exception as e:
        steps.append(f"climate FAILED: {e}")
    try:
        await ha.call_service("lock", "lock", entity_id="lock.front_door")
        steps.append("door locked")
    except Exception as e:
        steps.append(f"door FAILED: {e}")
    return {"content": [{"type": "text", "text": " · ".join(steps)}]}


@tool(
    "morning_brief",
    "Compose the morning brief: today's calendar, upcoming family events (7 day), "
    "AAPL ledger snapshot, next vest. Pulls from local sources only. L1 — safe "
    "to run unattended.",
    {},
)
async def morning_brief(args):
    today = datetime.now(PACIFIC).strftime("%A, %Y-%m-%d")
    parts: list[str] = [f"# Morning brief — {today}\n"]

    # Calendar
    try:
        events = await cal_shim.list_events(days_ahead=2)
        today_iso = datetime.now(PACIFIC).strftime("%Y-%m-%d")
        # AppleScript date strings are locale-formatted; we just dump them.
        if events:
            parts.append("## Next 48h\n" + "\n".join(
                f"- {e['starts_at']} — {e['title']} [{e['calendar']}]"
                for e in events[:10]
            ))
        else:
            parts.append("## Next 48h\n_(nothing scheduled)_")
    except Exception as e:
        parts.append(f"## Next 48h\n_(calendar read failed: {e})_")

    # Finance snapshot
    try:
        s = ledger.lot_summary()
        if s["lot_count"]:
            parts.append(
                f"\n## AAPL ledger\n"
                f"- {s['lot_count']} lots, {s['total_shares']:.0f} shares total\n"
                f"- avg cost basis ${s['avg_cost_basis']:.2f}\n"
                f"- long-term: {s['long_term_lots']} lots / {s['long_term_shares']:.0f} sh"
            )
        else:
            parts.append("\n## AAPL ledger\n_(empty — populate data/finance.json)_")
        next_vests = ledger.upcoming_vests(days=90)
        if next_vests:
            v = next_vests[0]
            parts.append(
                f"- next vest: {v['shares']:.0f} sh on {v['vest_date']} "
                f"(in {v['days_until']} days)"
            )
    except Exception as e:
        parts.append(f"\n## Finance\n_(read failed: {e})_")

    # HA — only if configured. Adapts to whatever entities exist; flags
    # door/lock/cover that's unlocked/open and any unavailable entities.
    if ha.is_configured():
        try:
            states = await ha.get_states()
            ha_lines: list[str] = []

            # Anomaly surface: doors unlocked, covers open, alarm disarmed.
            for s in states:
                eid = s["entity_id"]
                st = s["state"]
                if eid.startswith("lock.") and st == "unlocked":
                    ha_lines.append(f"- ⚠️ {eid}: unlocked")
                elif eid.startswith("cover.") and st == "open":
                    ha_lines.append(f"- ⚠️ {eid}: open")
                elif eid.startswith("alarm_control_panel.") and st in ("disarmed", "pending"):
                    ha_lines.append(f"- ⚠️ {eid}: {st}")
                elif st == "unavailable" and eid.split(".")[0] in (
                    "lock", "climate", "alarm_control_panel", "cover", "binary_sensor"
                ):
                    ha_lines.append(f"- ⚠️ {eid}: unavailable")

            # Notable single-entity reads, only if present.
            for eid in ("climate.main", "weather.home", "weather.forecast_home"):
                hit = next((s for s in states if s["entity_id"] == eid), None)
                if hit:
                    attrs = hit.get("attributes", {})
                    if eid.startswith("climate."):
                        ha_lines.append(
                            f"- {eid}: {hit['state']} "
                            f"({attrs.get('current_temperature', '?')}°F)"
                        )
                    elif eid.startswith("weather."):
                        t = attrs.get("temperature")
                        ha_lines.append(f"- {eid}: {hit['state']}, {t}° if known")

            # Always show a domain count summary as the floor.
            from collections import Counter
            domains = Counter(s["entity_id"].split(".")[0] for s in states)
            top = ", ".join(f"{d}:{n}" for d, n in domains.most_common(6))
            ha_lines.append(f"- _{len(states)} entities · {top}_")

            parts.append("\n## Home\n" + "\n".join(ha_lines))
        except Exception as e:
            parts.append(f"\n## Home\n_(HA read failed: {e})_")

    return {"content": [{"type": "text", "text": "\n".join(parts)}]}


def routines():
    return [good_night, leaving_home, morning_brief]

"""Tier classification + PreToolUse gating hook.

Per design §2: a tool's tier is fixed (config/tiers.toml). The profile
(config/profiles.toml) decides whether each tier is allowed, prompted, or
denied. Roles add a hard ceiling (config/roles.toml).
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .auth import Principal

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

PROMPT_DECISION = Callable[[dict, int, Principal], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_toml(p: Path) -> dict:
    return tomllib.loads(p.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class TierRule:
    pattern: re.Pattern
    tier: int


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    policy: dict[int, str]  # tier -> "allow" | "prompt_cli" | "prompt_push" | "deny"
    domains: list[str]


@dataclass(frozen=True)
class Role:
    name: str
    default_profile: str
    allowed_profiles: set[str]
    max_tier: int
    can_promote_memory: bool
    can_read_all_scopes: bool


def load_tier_rules() -> list[TierRule]:
    raw = _load_toml(CONFIG_DIR / "tiers.toml")
    rules: list[TierRule] = []
    for r in raw.get("rule", []):
        rules.append(TierRule(pattern=re.compile(r["pattern"]), tier=int(r["tier"])))
    return rules


def load_profiles() -> dict[str, Profile]:
    raw = _load_toml(CONFIG_DIR / "profiles.toml")
    out: dict[str, Profile] = {}
    for name, body in raw.items():
        # toml emits sub-table policy as {1: "allow", ...} when keys are bare ints,
        # but we authored "policy.1 = ..." which yields {'1': '...'}. Normalize.
        policy_raw = body.get("policy", {})
        policy = {int(k): v for k, v in policy_raw.items()}
        out[name] = Profile(
            name=name,
            description=body.get("description", ""),
            policy=policy,
            domains=list(body.get("domains", [])),
        )
    return out


def load_roles() -> dict[str, Role]:
    raw = _load_toml(CONFIG_DIR / "roles.toml")
    out: dict[str, Role] = {}
    for name, body in raw.items():
        out[name] = Role(
            name=name,
            default_profile=body["default_profile"],
            allowed_profiles=set(body["allowed_profiles"]),
            max_tier=int(body["max_tier"]),
            can_promote_memory=bool(body.get("can_promote_memory", False)),
            can_read_all_scopes=bool(body.get("can_read_all_scopes", False)),
        )
    return out


# Load once at import time. Cheap, immutable.
_TIER_RULES = load_tier_rules()
_PROFILES = load_profiles()
_ROLES = load_roles()


_MCP_PREFIX = re.compile(r"^mcp__[^_]+(?:_[^_]+)*__")


def classify(tool_name: str) -> int:
    """Map a tool name to its tier. Unknown tools default to L2 (conservative).

    The SDK exposes MCP-server tools as `mcp__<server>__<tool>`. The
    config/tiers.toml patterns are written against the bare `<tool>` form,
    so we strip the prefix before matching.
    """
    canonical = _MCP_PREFIX.sub("", tool_name) if tool_name.startswith("mcp__") else tool_name
    for r in _TIER_RULES:
        if r.pattern.match(canonical):
            return r.tier
    return 2


def get_profile(name: str) -> Profile:
    if name not in _PROFILES:
        raise KeyError(f"unknown profile: {name}. Known: {sorted(_PROFILES)}")
    return _PROFILES[name]


def get_role(name: str) -> Role:
    if name not in _ROLES:
        raise KeyError(f"unknown role: {name}. Known: {sorted(_ROLES)}")
    return _ROLES[name]


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def build_gate_hook(
    session,
    profile: Profile,
    principal: Principal,
    *,
    prompt_cli: PROMPT_DECISION,
    prompt_push: PROMPT_DECISION,
):
    """Construct an async PreToolUse hook bound to this session/profile/principal.

    `prompt_cli` and `prompt_push` are injected so tests (and the unattended
    profile) don't have to touch IO.
    """
    role = get_role(principal.role)

    async def gate(input_data: dict, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        tier = classify(tool_name)

        # 1. Hard role ceiling
        if tier > role.max_tier:
            seq = session.log_intent(
                {"tool_name": tool_name, "tool_input": tool_input}, tier
            )
            session.log_decision(
                seq, tool_name, tier, tool_input, "deny", "role_ceiling"
            )
            return _deny(
                f"Tier {tier} exceeds {principal.role} ceiling of {role.max_tier}"
            )

        # 2. Profile policy
        action = profile.policy.get(tier, "deny")
        seq = session.log_intent(
            {"tool_name": tool_name, "tool_input": tool_input}, tier
        )

        decision: str
        decided_by: str
        approved: bool

        if action == "allow":
            decision, decided_by, approved = "allow", "auto", True
        elif action == "deny":
            decision, decided_by, approved = "deny", f"profile:{profile.name}", False
        elif action == "prompt_cli":
            approved = await prompt_cli(
                {"tool_name": tool_name, "tool_input": tool_input}, tier, principal
            )
            decision = "allow_after_prompt" if approved else "deny"
            decided_by = "cli"
        elif action == "prompt_push":
            approved = await prompt_push(
                {"tool_name": tool_name, "tool_input": tool_input}, tier, principal
            )
            decision = "allow_after_prompt" if approved else "deny"
            decided_by = "push"
        else:
            decision, decided_by, approved = "deny", "unknown_policy", False

        session.log_decision(seq, tool_name, tier, tool_input, decision, decided_by)

        if approved:
            return _allow()
        return _deny(
            f"L{tier} {action} under profile '{profile.name}': user denied or policy blocked"
        )

    return gate

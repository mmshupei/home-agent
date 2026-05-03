"""Token issuance + verification.

Token format:  agent_<user_id>_<24_url_safe_bytes>
Stored:        HMAC-SHA256(AUTH_HMAC_KEY, token) hex
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from sqlite3 import Connection

TOKEN_PREFIX = "agent_"


def _key() -> bytes:
    raw = os.environ.get("AUTH_HMAC_KEY")
    if not raw:
        raise RuntimeError(
            "AUTH_HMAC_KEY not set in environment. "
            "Generate with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    # Accept either hex or raw; hex is the recommended .env representation.
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode()


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    token_label: str | None


def hash_token(token: str) -> str:
    return hmac.new(_key(), token.encode(), hashlib.sha256).hexdigest()


def mint_token(user_id: str) -> str:
    return f"{TOKEN_PREFIX}{user_id}_{secrets.token_urlsafe(24)}"


def issue(conn: Connection, user_id: str, label: str | None = None) -> str:
    """Issue a new token for an existing user. Returns the plaintext token
    (printed once, never stored in plaintext)."""
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown user_id: {user_id}")

    token = mint_token(user_id)
    conn.execute(
        "INSERT INTO tokens(token_hash, user_id, label) VALUES (?, ?, ?)",
        (hash_token(token), user_id, label),
    )
    return token


def verify(token: str, conn: Connection) -> Principal | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    th = hash_token(token)
    row = conn.execute(
        """
        SELECT t.user_id, t.label, u.name, u.role
        FROM tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ? AND t.revoked_at IS NULL
        """,
        (th,),
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE tokens SET last_used = CURRENT_TIMESTAMP WHERE token_hash = ?", (th,)
    )
    return Principal(
        user_id=row["user_id"],
        name=row["name"],
        role=row["role"],
        token_label=row["label"],
    )


def revoke(conn: Connection, token_hash_prefix: str) -> int:
    """Revoke any tokens whose hash starts with the given prefix. Returns count."""
    cur = conn.execute(
        "UPDATE tokens SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE token_hash LIKE ? AND revoked_at IS NULL",
        (token_hash_prefix + "%",),
    )
    return cur.rowcount

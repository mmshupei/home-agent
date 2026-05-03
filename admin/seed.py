"""First-time setup: ensure schema, create the initial admin user, mint a token.

Idempotent for schema and user. Token issuance prints once and exits.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from orchestrator import auth
from orchestrator.db import connect, ensure_schema, db_path

INITIAL_ADMIN = {
    "id": "shupei",
    "name": "Shupei",
    "role": "admin",
}


def main() -> int:
    load_dotenv()
    ensure_schema()

    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE id = ?", (INITIAL_ADMIN["id"],)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users(id, name, role) VALUES (?, ?, ?)",
                (INITIAL_ADMIN["id"], INITIAL_ADMIN["name"], INITIAL_ADMIN["role"]),
            )
            print(f"created user: {INITIAL_ADMIN['id']} ({INITIAL_ADMIN['role']})")
        else:
            print(f"user already exists: {INITIAL_ADMIN['id']}")

        existing_token = conn.execute(
            "SELECT 1 FROM tokens WHERE user_id = ? AND revoked_at IS NULL",
            (INITIAL_ADMIN["id"],),
        ).fetchone()
        if existing_token:
            print(
                f"\nadmin already has an active token. To issue another:\n"
                f"  uv run python -m admin.cli token issue --user {INITIAL_ADMIN['id']} "
                f"--label <label>\n"
            )
            return 0

        token = auth.issue(conn, INITIAL_ADMIN["id"], label="seed")

    print(f"\ndatabase: {db_path()}")
    print("\n=== INITIAL ADMIN TOKEN (printed once, store it now) ===")
    print(token)
    print("=========================================================\n")
    print("Test it:")
    print("  uv run agent --task 'hello'")
    print("  curl -H 'X-Agent-Token: " + token + "' \\")
    print("       -H 'Content-Type: application/json' \\")
    print("       -d '{\"task\":\"hello\"}' http://127.0.0.1:8765/run")
    return 0


if __name__ == "__main__":
    sys.exit(main())

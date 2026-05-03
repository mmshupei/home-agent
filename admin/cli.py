"""CLI: agent runner + admin commands.

Usage:
  agent --task "..."                     # run as default admin (interactive profile)
  agent --task "..." --principal yunyan --profile mobile
  agent token issue --user yunyan --label yunyan-iphone
  agent token list
  agent token revoke <hash-prefix>
  agent user add --id yunyan --name Yunyan --role adult
  agent user list
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from orchestrator import auth, loop
from orchestrator.db import connect, ensure_schema

console = Console()


def _resolve_principal(user_id: str) -> auth.Principal:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"unknown user_id: {user_id}")
    return auth.Principal(
        user_id=row["id"], name=row["name"], role=row["role"], token_label="cli"
    )


def cmd_run(args: argparse.Namespace) -> int:
    principal = _resolve_principal(args.principal)
    result = asyncio.run(
        loop.run(
            task=args.task,
            principal=principal,
            profile=args.profile,
            model=args.model,
        )
    )
    console.print(result)
    return 0


def cmd_token_issue(args: argparse.Namespace) -> int:
    with connect() as conn:
        token = auth.issue(conn, args.user, args.label)
    console.print("\n[bold yellow]new token (shown once):[/bold yellow]")
    console.print(token)
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    with connect() as conn:
        rows = conn.execute(
            """SELECT substr(token_hash, 1, 12) AS prefix, user_id, label,
                      created_at, last_used, revoked_at
               FROM tokens ORDER BY created_at DESC"""
        ).fetchall()
    table = Table(title="tokens")
    for col in ("hash_prefix", "user_id", "label", "created_at", "last_used", "revoked_at"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["prefix"], r["user_id"], r["label"] or "",
            str(r["created_at"]), str(r["last_used"] or ""),
            str(r["revoked_at"] or "")
        )
    console.print(table)
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    with connect() as conn:
        n = auth.revoke(conn, args.prefix)
    console.print(f"revoked {n} token(s)")
    return 0


def cmd_user_add(args: argparse.Namespace) -> int:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users(id, name, role, imessage_handle) VALUES (?, ?, ?, ?)",
            (args.id, args.name, args.role, args.imessage),
        )
    console.print(f"created user: {args.id} ({args.role})")
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, role, imessage_handle, created_at FROM users ORDER BY id"
        ).fetchall()
    table = Table(title="users")
    for col in ("id", "name", "role", "imessage_handle", "created_at"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["id"], r["name"], r["role"], r["imessage_handle"] or "", str(r["created_at"]))
    console.print(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="Family agent CLI")
    sub = p.add_subparsers(dest="cmd")

    # default: `agent --task "..."` runs the loop
    p.add_argument("--task", help="task to run (omit when using a subcommand)")
    p.add_argument("--principal", default="shupei", help="user_id to run as (default: shupei)")
    p.add_argument("--profile", default="interactive", help="profile name (default: interactive)")
    p.add_argument("--model", default="claude-opus-4-7", help="model id")

    tok = sub.add_parser("token", help="manage tokens").add_subparsers(dest="tok_cmd")

    iss = tok.add_parser("issue")
    iss.add_argument("--user", required=True)
    iss.add_argument("--label")
    iss.set_defaults(func=cmd_token_issue)

    lst = tok.add_parser("list")
    lst.set_defaults(func=cmd_token_list)

    rev = tok.add_parser("revoke")
    rev.add_argument("prefix", help="hash prefix (12 chars from `token list`)")
    rev.set_defaults(func=cmd_token_revoke)

    usr = sub.add_parser("user", help="manage users").add_subparsers(dest="usr_cmd")

    ua = usr.add_parser("add")
    ua.add_argument("--id", required=True)
    ua.add_argument("--name", required=True)
    ua.add_argument("--role", required=True, choices=["admin", "adult", "child"])
    ua.add_argument("--imessage", help="iMessage handle (phone/email), optional")
    ua.set_defaults(func=cmd_user_add)

    ul = usr.add_parser("list")
    ul.set_defaults(func=cmd_user_list)

    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ensure_schema()
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args)
    if args.task:
        return cmd_run(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

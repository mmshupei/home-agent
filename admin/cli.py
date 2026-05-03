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
from orchestrator import memory as mem
from orchestrator.db import connect, ensure_schema, has_vec

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


def cmd_chat(args: argparse.Namespace) -> int:
    """Interactive REPL: type a message, get a reply, repeat. Each turn is a
    fresh agent invocation (spawn-per-task model), so memory is the bridge
    between turns. Empty line or Ctrl-D exits."""
    principal = _resolve_principal(args.principal)
    console.print(
        f"[dim]chat as {principal.name} ({principal.role}) profile={args.profile} "
        f"model={args.model}\nempty line or Ctrl-D to exit[/dim]"
    )
    turn = 0
    while True:
        try:
            line = console.input("[bold cyan]you ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0
        if not line:
            console.print("[dim]bye[/dim]")
            return 0
        turn += 1
        result = asyncio.run(
            loop.run(
                task=line, principal=principal, profile=args.profile, model=args.model
            )
        )
        console.print(f"[bold green]agent ›[/bold green] {result}\n")


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


def cmd_memory_inspect(args: argparse.Namespace) -> int:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, scope, kind, content, confidence, created_at, created_by,
                      expires_at
               FROM memory
               WHERE confidence > 0
               ORDER BY id DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
    table = Table(title=f"memory (last {args.limit})")
    for col in ("id", "scope", "kind", "conf", "created_by", "created_at", "content"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["id"]), r["scope"], r["kind"], f"{r['confidence']:.2f}",
            r["created_by"], str(r["created_at"]),
            (r["content"][:80] + "…") if len(r["content"]) > 80 else r["content"],
        )
    console.print(table)
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    principal = _resolve_principal(args.principal)
    rows = mem.search(query=args.query, principal=principal, top_k=args.top_k)
    if not rows:
        console.print("[dim](no results)[/dim]")
        return 0
    for r in rows:
        console.print(f"[cyan]#{r.id}[/cyan] [magenta]{r.scope}[/magenta] "
                      f"({r.kind}, conf={r.confidence:.2f}) {r.content}")
    return 0


def cmd_memory_reindex(args: argparse.Namespace) -> int:
    """Re-embed every active memory row with the current embedder and rebuild
    the vec0 table. Use after switching AGENT_EMBEDDING_MODEL or after a
    cold-start fallback wrote pseudo-embeddings."""
    from orchestrator.embed import embed_batch, embedding_dim, model_name, serialize, using_local_model
    from orchestrator.db import EMBEDDING_DIM, VEC_SCHEMA
    dim = embedding_dim()
    console.print(f"reindex: model={model_name()} dim={dim} local={using_local_model()}")
    if dim != EMBEDDING_DIM:
        console.print(
            f"[red]model dim ({dim}) != schema dim ({EMBEDDING_DIM}). "
            f"Update orchestrator/db.py EMBEDDING_DIM and re-seed.[/red]"
        )
        return 1
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, content FROM memory WHERE confidence > 0"
        ).fetchall()
        if not rows:
            console.print("(no memories to index)")
            return 0
        console.print(f"embedding {len(rows)} memories...")
        vecs = embed_batch([r["content"] for r in rows], input_type="document")
        conn.execute("DROP TABLE IF EXISTS memory_vec")
        conn.executescript(VEC_SCHEMA)
        for r, v in zip(rows, vecs):
            conn.execute(
                "INSERT INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                (r["id"], serialize(v)),
            )
    console.print(f"reindexed {len(rows)} memories")
    return 0


def cmd_memory_prune(args: argparse.Namespace) -> int:
    with connect() as conn:
        cur1 = conn.execute("DELETE FROM memory WHERE expires_at < CURRENT_TIMESTAMP")
        cur2 = conn.execute(
            "DELETE FROM events WHERE ends_at < datetime('now', '-30 days')"
        )
        conn.execute(
            """UPDATE memory
               SET confidence = confidence * 0.95
               WHERE last_seen < datetime('now', '-30 days') AND kind != 'fact'"""
        )
        cur4 = conn.execute(
            """INSERT INTO memory_archive
               (id, scope, kind, content, created_at, created_by, expires_at,
                confidence, last_seen)
               SELECT id, scope, kind, content, created_at, created_by, expires_at,
                      confidence, last_seen FROM memory WHERE confidence < 0.3"""
        )
        cur5 = conn.execute("DELETE FROM memory WHERE confidence < 0.3")
        if has_vec(conn):
            conn.execute(
                "DELETE FROM memory_vec WHERE rowid NOT IN (SELECT id FROM memory)"
            )
    console.print(
        f"pruned: expired_memory={cur1.rowcount} old_events={cur2.rowcount} "
        f"archived={cur4.rowcount} (then deleted={cur5.rowcount})"
    )
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

    chat = sub.add_parser("chat", help="interactive REPL (texting-style)")
    chat.add_argument("--principal", default="shupei")
    chat.add_argument("--profile", default="home", help="default 'home' so L2 actions don't prompt mid-chat")
    chat.add_argument("--model", default="claude-opus-4-7")

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

    memg = sub.add_parser("memory", help="inspect / search / prune memory").add_subparsers(
        dest="mem_cmd"
    )

    mi = memg.add_parser("inspect")
    mi.add_argument("--limit", type=int, default=20)
    mi.set_defaults(func=cmd_memory_inspect)

    ms = memg.add_parser("search")
    ms.add_argument("query")
    ms.add_argument("--principal", default="shupei")
    ms.add_argument("--top-k", dest="top_k", type=int, default=8)
    ms.set_defaults(func=cmd_memory_search)

    mp = memg.add_parser("prune")
    mp.set_defaults(func=cmd_memory_prune)

    mr = memg.add_parser("reindex", help="re-embed all active memories with current model")
    mr.set_defaults(func=cmd_memory_reindex)

    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ensure_schema()
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        return args.func(args)
    if args.cmd == "chat":
        return cmd_chat(args)
    if args.task:
        return cmd_run(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

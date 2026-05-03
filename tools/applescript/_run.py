"""Shared subprocess wrapper for `osascript`.

All AppleScript shims funnel through here so timeouts, error formatting,
and argument escaping are consistent. AppleScript values are passed via
stdin as a Python-built script (no shell interpolation of user input).
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass


@dataclass
class ASResult:
    ok: bool
    stdout: str
    stderr: str
    code: int

    def text(self) -> str:
        return self.stdout.strip()


def escape(value: str) -> str:
    """Quote a Python string for inclusion as an AppleScript string literal.

    AppleScript strings are double-quoted with backslash-escaped backslashes
    and double quotes. Newlines become literal `\\n`.
    """
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        + '"'
    )


async def run(script: str, *, timeout: float = 15.0) -> ASResult:
    """Execute an AppleScript snippet via osascript. Script is fed via stdin
    so we never construct a shell string from user input."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(script.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ASResult(False, "", f"osascript timed out after {timeout}s", -1)
    return ASResult(
        ok=proc.returncode == 0,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        code=proc.returncode or 0,
    )


def run_sync(script: str, *, timeout: float = 15.0) -> ASResult:
    """Synchronous variant for CLI smoke tests."""
    try:
        cp = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ASResult(False, "", f"osascript timed out after {timeout}s", -1)
    return ASResult(
        ok=cp.returncode == 0, stdout=cp.stdout, stderr=cp.stderr, code=cp.returncode
    )

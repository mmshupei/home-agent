"""Install / uninstall the agent's launchd jobs.

Copies (and rewrites) the plists in triggers/launchd/ into ~/Library/LaunchAgents,
substituting the user's actual home path so the same templates work for any
operator. Idempotent.

Usage:
  uv run python -m admin.launchd install
  uv run python -m admin.launchd uninstall
  uv run python -m admin.launchd status
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "triggers" / "launchd"
DEST_DIR = Path.home() / "Library" / "LaunchAgents"
HOME_PLACEHOLDER = "/Users/smo"


def _plists() -> list[Path]:
    return sorted(SRC_DIR.glob("*.plist"))


def _rewrite(src: Path, dest: Path) -> None:
    """Copy src→dest, rewriting the hardcoded /Users/smo path so the plist
    works on this operator's machine."""
    text = src.read_text(encoding="utf-8")
    home = str(Path.home())
    if home != HOME_PLACEHOLDER:
        text = text.replace(HOME_PLACEHOLDER, home)
    dest.write_text(text, encoding="utf-8")


def _label(plist: Path) -> str:
    # All our labels are the filename without .plist
    return plist.stem


def install() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "data" / "runs").mkdir(parents=True, exist_ok=True)

    for src in _plists():
        dest = DEST_DIR / src.name
        _rewrite(src, dest)
        # Bootstrap into the user's gui domain so the job runs at login.
        gui = f"gui/{_get_uid()}"
        subprocess.run(["launchctl", "bootout", gui, str(dest)],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", gui, str(dest)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"  {'OK' if ok else 'FAIL'}  {src.name}"
              + (f"\n      stderr: {r.stderr.strip()}" if not ok else ""))
    return 0


def uninstall() -> int:
    if not DEST_DIR.exists():
        print("(no LaunchAgents dir)")
        return 0
    gui = f"gui/{_get_uid()}"
    for src in _plists():
        dest = DEST_DIR / src.name
        if dest.exists():
            subprocess.run(["launchctl", "bootout", gui, str(dest)],
                           capture_output=True)
            dest.unlink()
            print(f"  removed {src.name}")
    return 0


def status() -> int:
    gui = f"gui/{_get_uid()}"
    for src in _plists():
        label = _label(src)
        r = subprocess.run(
            ["launchctl", "print", f"{gui}/{label}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            # Pull the next-run line out of the verbose output if present.
            line = next(
                (l for l in r.stdout.splitlines() if "next" in l.lower() or "last" in l.lower()),
                "(loaded; no schedule info)",
            )
            print(f"  loaded   {label}  {line.strip()}")
        else:
            print(f"  missing  {label}")
    return 0


def _get_uid() -> int:
    import os
    return os.getuid()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["install", "uninstall", "status"])
    args = p.parse_args()
    return {"install": install, "uninstall": uninstall, "status": status}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())

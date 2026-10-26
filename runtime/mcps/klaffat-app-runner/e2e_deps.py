#!/usr/bin/env python3
"""Install the worktree's locked E2E packages, then exec a fixed command.

Usage (inside the worktree's dev shell, from the worktree root):
    e2e_deps.py -- <command> [args...]

Playwright runs from tests/e2e/node_modules, which a fresh linked worktree
does not have. `npm ci` installs exactly the committed lockfile every time,
so a stale or hand-edited node_modules cannot change what the suite runs.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        raise RuntimeError("fixed command separator is required")
    print("klaffat-app-runner: installing tests/e2e packages (npm ci)", flush=True)
    installed = subprocess.run(
        ["npm", "ci", "--no-audit", "--no-fund", "--prefer-offline"],
        cwd=Path.cwd() / "tests" / "e2e",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if installed.returncode != 0:
        raise RuntimeError(f"npm ci failed with exit {installed.returncode}")
    os.execvp(argv[2], argv[2:])
    raise RuntimeError("fixed command did not exec")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except RuntimeError as exc:
        print(f"klaffat-app-runner: {exc}", file=sys.stderr)
        raise SystemExit(1)

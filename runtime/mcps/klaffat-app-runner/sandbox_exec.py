#!/usr/bin/env python3
"""Prepare the app-runner's private runtime view, then exec a fixed command.

Usage: sandbox_exec.py <home> -- <absolute command> [args...]
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def prepare_runtime_parent(path: Path, expected_uid: int) -> None:
    """Tighten the synthetic user-runtime parent without following links."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("runtime parent is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
    ):
        raise RuntimeError("runtime parent must be an owned real directory")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
        secured = path.lstat()
    except OSError as exc:
        raise RuntimeError("runtime parent could not be secured") from exc
    if secured.st_uid != expected_uid or stat.S_IMODE(secured.st_mode) != 0o700:
        raise RuntimeError("runtime parent permissions remain unsafe")


def prepare_home(path: Path, expected_uid: int) -> None:
    """Create the unit HOME as an owned private directory reached without links."""
    if not path.is_absolute():
        raise RuntimeError("home must be absolute")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("home could not be created") from exc
    if (
        canonical != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
    ):
        raise RuntimeError("home must be an owned canonical directory")
    os.chmod(path, 0o700, follow_symlinks=False)


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        raise RuntimeError("home and fixed command separator are required")
    command = argv[3]
    if not Path(command).is_absolute():
        raise RuntimeError("fixed command must be absolute")
    uid = os.getuid()
    prepare_runtime_parent(Path("/run/user") / str(uid), uid)
    prepare_home(Path(argv[1]), uid)
    os.execve(command, argv[3:], dict(os.environ))
    raise RuntimeError("fixed command did not exec")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except RuntimeError as exc:
        print(f"klaffat-app-runner sandbox setup failed: {exc}", file=sys.stderr)
        raise SystemExit(126)

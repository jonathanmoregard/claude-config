"""unit-read and trash run outside the Claude Code sandbox without a prompt, so
neither may pass on an argument that steers the tool underneath it."""
from __future__ import annotations

import os
import pwd
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT_READ = REPO / "runtime" / "scripts" / "unit-read"
TRASH = REPO / "runtime" / "scripts" / "trash"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def run(script: Path, tmp_path: Path, *args: str) -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    for name in ("systemctl", "gio"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >>"{calls}"\n')
        stub.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    return proc.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_unit_read_passes_a_checked_read_through(tmp_path: Path) -> None:
    rc, calls = run(UNIT_READ, tmp_path, "--user", "show", "-p", "ActiveState,SubState",
                    "gh-token.service")
    assert rc == 0
    assert calls == ["systemctl --user --no-pager show -p ActiveState,SubState -- gh-token.service"]
    rc, calls = run(UNIT_READ, tmp_path, "list-timers", "--all")
    assert rc == 0
    assert calls[-1] == "systemctl --no-pager list-timers --all --"


@pytest.mark.parametrize("args", [
    ("--user", "start", "x.service"),
    ("--user", "status", "-H", "example.invalid", "x"),
    ("--user", "status", "-lHexample.invalid", "x"),
    ("--user", "status", "--ho=example.invalid", "x"),
    ("status", "--machine=m", "x"),
    ("status", "-M", "m", "x"),
    ("show", "-p", "ActiveState", "--root=/tmp/r", "x"),
    ("status", "x;id"),
    ("status", "$(id)"),
    ("--user", "--user", "status", "x"),
    ("-H", "example.invalid", "status", "x"),
    (),
])
def test_unit_read_refuses_everything_else(tmp_path: Path, args: tuple[str, ...]) -> None:
    rc, calls = run(UNIT_READ, tmp_path, *args)
    assert rc == 64
    assert calls == []


@pytest.fixture
def scratch():
    root = REAL_HOME / "worktrees" / f".trash-test-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    yield root
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_symlink() or path.is_file() else path.rmdir()
    root.rmdir()


def test_trash_passes_paths_under_the_write_roots(tmp_path: Path, scratch: Path) -> None:
    (scratch / "f").write_text("x")
    (scratch / "link").symlink_to("/etc/hostname")
    rc, calls = run(TRASH, tmp_path, str(scratch / "f"), str(scratch / "link"))
    assert rc == 0
    assert calls == [f"gio trash -- {scratch}/f {scratch}/link"]


def test_trash_refuses_options_and_paths_elsewhere(tmp_path: Path, scratch: Path) -> None:
    (scratch / "out").symlink_to(REAL_HOME)
    (scratch / "f").write_text("x")
    for args, code in [
        (("--empty",), 64),
        (("--restore", "trash:///f"), 64),
        ((str(scratch / "f"), "--force"), 64),
        ((str(REAL_HOME / ".bashrc"),), 1),
        ((str(scratch / "out" / ".bashrc"),), 1),
        ((str(REAL_HOME / "worktrees"),), 1),
        ((f"{REAL_HOME}/worktrees/.",), 1),
        ((f"{scratch}/\n",), 1),
        ((str(REAL_HOME / "Repos" / ".."),), 1),
        ((str(scratch / ".."),), 1),
        ((str(scratch / "missing"),), 1),
        ((), 64),
    ]:
        rc, calls = run(TRASH, tmp_path, *args)
        assert (rc, calls) == (code, []), args

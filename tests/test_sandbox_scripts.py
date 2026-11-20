"""unit-read and `wait-for unit` run outside the Claude Code sandbox without a
prompt, so neither may pass on an argument that steers the tool underneath it.
`trash` runs inside it."""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote_to_bytes

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtime" / "scripts"
UNIT_READ = SCRIPTS / "unit-read"
TRASH = SCRIPTS / "trash"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def staged(script: Path, tmp_path: Path, bin_dir: Path) -> Path:
    """The script with its fixed PATH searching bin_dir first."""
    source, n = re.subn(r"\bPATH=/run/current-system/sw/bin:",
                        f"PATH={bin_dir}:/run/current-system/sw/bin:", script.read_text())
    assert n == 1
    copy = tmp_path / script.name
    copy.write_text(source)
    return copy


def run_unit_read(tmp_path: Path, *args: str, output: str = "",
                  env: dict[str, str] | None = None) -> tuple[int, list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    printed = tmp_path / "systemctl-output"
    printed.write_text(output)
    stub = bin_dir / "systemctl"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "systemctl $*" >>"{calls}"\n'
                    f'env -0 >"{tmp_path}/systemctl-env"\ncat "{printed}"\n')
    stub.chmod(0o755)
    proc = subprocess.run(["bash", str(staged(UNIT_READ, tmp_path, bin_dir)), *args],
                          capture_output=True, text=True, check=False, env=env)
    return (proc.returncode, calls.read_text().splitlines() if calls.exists() else [],
            proc.stdout)


def test_unit_read_passes_a_checked_read_through(tmp_path: Path) -> None:
    rc, calls, _ = run_unit_read(tmp_path, "--user", "show", "-p", "ActiveState,SubState",
                                 "gh-token.service")
    assert rc == 0
    assert calls == ["systemctl --user --no-pager show -p ActiveState,SubState -- gh-token.service"]
    rc, calls, _ = run_unit_read(tmp_path, "list-timers", "--all")
    assert rc == 0
    assert calls[-1] == "systemctl --no-pager list-timers --all --"


@pytest.mark.parametrize("given, passed", [("08", "8"), ("010", "10"), ("0", "0"),
                                           ("99999999999999999999", "10000")])
def test_unit_read_passes_line_counts_as_decimal_within_a_ceiling(
        tmp_path: Path, given: str, passed: str) -> None:
    rc, calls, _ = run_unit_read(tmp_path, "--user", "status", "-n", given, "x.service")
    assert rc == 0
    assert calls == [f"systemctl --user --no-pager status -n {passed} -- x.service"]


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
    rc, calls, _ = run_unit_read(tmp_path, *args)
    assert rc == 64
    assert calls == []


@pytest.mark.parametrize("args", [("--user", "show", "-p", "Environment"),
                                  ("--user", "show", "-p", "Id,Environment", "x.service"),
                                  ("show", "--property=Environment", "x.service"),
                                  ("show", "-p", "SetCredential", "x.service"),
                                  ("show", "-p", "Id,SetCredentialEncrypted", "x.service"),
                                  ("--user", "show-environment")])
def test_unit_read_shows_no_environment(tmp_path: Path, args: tuple[str, ...]) -> None:
    rc, calls, _ = run_unit_read(tmp_path, *args)
    assert (rc, calls) == (64, [])


@pytest.mark.parametrize("verb, output", [
    ("show", "Id=x.service\nEnvironment=TOKEN=leaked\nActiveState=active\n"),
    ("show", "Id=x.service\nSetCredential=db:leaked\nSetCredentialEncrypted=k:leaked\nActiveState=active\n"),
    ("cat", "[Service]\nSetCredential=db:leaked\nExecStart=/bin/true\n"),
    ("cat", "[Service]\nEnvironment=\"TOKEN=leaked\" \\\n  \"MORE=leaked\"\n"
            "  Environment=TOKEN=leaked\nExecStart=/bin/true\n"),
    ("cat", "[Service]\nEnvironment = TOKEN=leaked\nExecStart=/bin/true\n"),
    ("cat", "[Service]\nEnvironment=A=1 \\\n# c\n SECRET=leaked\nExecStart=/bin/true\n"),
    ("cat", "[Service]\nSetCredential=a:b \\\n  ; c \\\n\t# d\n leaked \\\n leaked\nExecStart=/bin/true\n"),
])
def test_unit_read_drops_environment_lines(tmp_path: Path, verb: str, output: str) -> None:
    rc, _, out = run_unit_read(tmp_path, "--user", verb, "x.service", output=output)
    assert rc == 0
    assert "leaked" not in out
    assert out.splitlines()[0] == output.splitlines()[0]
    assert out.splitlines()[-1] == output.splitlines()[-1]


@pytest.mark.parametrize("args", [("--user", "status"), ("status", "-n", "5"),
                                  ("--user", "status", "*"),
                                  ("--user", "status", "x.service", "gh-*.service")])
def test_unit_read_status_names_its_units(tmp_path: Path, args: tuple[str, ...]) -> None:
    rc, calls, _ = run_unit_read(tmp_path, *args)
    assert (rc, calls) == (64, [])


STATUS = """\u25cf app.slice - User Application Slice
     Active: active since Thu
     CGroup: /user.slice/app.slice
             \u251c\u2500app-x.scope
             \u2502 \u2514\u250012 /bin/tool --token leaked
             \u2514\u250013 /bin/other leaked
             |-14 /bin/ascii leaked
             `-15 /bin/ascii leaked

Nov 19 10:00:00 host tool[12]: started
"""


def test_unit_read_status_shows_no_process_tree(tmp_path: Path) -> None:
    rc, _, out = run_unit_read(tmp_path, "--user", "status", "app.slice", output=STATUS)
    assert rc == 0
    assert "leaked" not in out
    assert out.splitlines()[0] == STATUS.splitlines()[0]
    assert out.splitlines()[-1] == STATUS.splitlines()[-1]


def interpreter(script: Path) -> str:
    return script.read_text().splitlines()[0].removeprefix("#!").split()[0]


@pytest.mark.parametrize("script", [
    pytest.param(path, marks=pytest.mark.skipif(
        not os.path.exists(interpreter(path)), reason="the script's interpreter is NixOS-only"))
    for path in (UNIT_READ, TRASH, SCRIPTS / "wait-for")
], ids=lambda p: p.name)
def test_scripts_run_no_caller_bash_env(tmp_path: Path, script: Path) -> None:
    marker = tmp_path / "bash-env-ran"
    payload = tmp_path / "p.sh"
    payload.write_text(f": >{marker}\n")
    subprocess.run([str(script)], capture_output=True, text=True, timeout=30,
                   env={**os.environ, "BASH_ENV": str(payload), "ENV": str(payload)},
                   check=False)
    assert not marker.exists()


def caller_env(tmp_path: Path) -> dict[str, str]:
    """What a caller could point systemctl at: its own unit files, bus and manager."""
    return {**os.environ, "SYSTEMD_UNIT_PATH": str(tmp_path),
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp_path}/sys",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/bus",
            "XDG_RUNTIME_DIR": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path),
            "HOME": str(tmp_path), "UID": "0"}


@pytest.mark.parametrize("scope, expected", [
    ((), {}), (("--user",), {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}),
], ids=["system", "user"])
def test_unit_read_gives_systemctl_none_of_the_callers_environment(
        tmp_path: Path, scope: tuple[str, ...], expected: dict[str, str]) -> None:
    rc, _, _ = run_unit_read(tmp_path, *scope, "cat", "x.service", env=caller_env(tmp_path))
    assert rc == 0
    pairs = (tmp_path / "systemctl-env").read_text().split("\0")
    seen = dict(p.split("=", 1) for p in pairs if p)
    for name in ("PWD", "SHLVL", "_", "SHELLOPTS", "BASHOPTS", "PATH"):
        seen.pop(name, None)
    assert seen == expected


@pytest.mark.skipif(not os.path.exists(interpreter(UNIT_READ)) or not shutil.which("systemctl"),
                    reason="needs NixOS and systemd")
def test_unit_read_cat_reads_no_unit_file_the_caller_planted(tmp_path: Path) -> None:
    secret = tmp_path / "not-a-unit.txt"
    secret.write_text("PLANTED-TARGET\n")
    unit = f"unitreadprobe{uuid.uuid4().hex[:8]}@.service"
    (tmp_path / unit).write_text("[Unit]\n")
    (tmp_path / f"{unit}.d").mkdir()
    (tmp_path / f"{unit}.d" / "10-x.conf").symlink_to(secret)
    for scope in ((), ("--user",)):
        proc = subprocess.run([str(UNIT_READ), *scope, "cat", unit], capture_output=True,
                              text=True, timeout=30, check=False,
                              env={**os.environ, "SYSTEMD_UNIT_PATH": str(tmp_path)})
        assert "PLANTED-TARGET" not in proc.stdout


def run_trash(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(TRASH), *args], capture_output=True,
                          text=True, check=False, env={**os.environ, "HOME": str(home)})


def origin(record: Path) -> bytes:
    found = re.search(r"^Path=(.*)$", record.read_text(), re.M)
    assert found
    return unquote_to_bytes(found[1])


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for root in ("Repos", "worktrees"):
        (tmp_path / root / "project").mkdir(parents=True)
    return tmp_path


def test_trash_moves_into_the_trash_of_its_root_and_records_the_origin(home: Path) -> None:
    for root in ("Repos", "worktrees"):
        project = home / root / "project"
        (project / "f").write_text("x")
        (project / "d").mkdir()
        (project / "d" / "g").write_text("y")
        (project / "link").symlink_to("/etc/hostname")
        proc = run_trash(home, str(project / "f"), str(project / "d"), str(project / "link"))
        assert proc.returncode == 0, proc.stderr
        trash = home / root / ".trash"
        assert (trash / "files" / "f").read_text() == "x"
        assert (trash / "files" / "d" / "g").read_text() == "y"
        assert os.readlink(trash / "files" / "link") == "/etc/hostname"
        assert origin(trash / "info" / "d.trashinfo") == os.fsencode(project / "d")
        assert sorted(os.listdir(project)) == []


def test_trash_keeps_both_entries_of_the_same_name(home: Path) -> None:
    project = home / "worktrees" / "project"
    for content in ("one", "two"):
        (project / "same").write_text(content)
        assert run_trash(home, str(project / "same")).returncode == 0
    files = home / "worktrees" / ".trash" / "files"
    assert sorted(p.read_text() for p in files.iterdir()) == ["one", "two"]


def test_trash_takes_a_relative_path_and_a_name_that_is_not_utf8(home: Path) -> None:
    project = home / "worktrees" / "project"
    name = b"caf\xe9"
    with open(os.fsencode(project) + b"/" + name, "w") as f:
        f.write("x")
    proc = subprocess.run([sys.executable, "-I", str(TRASH), os.fsdecode(name)], cwd=project,
                          capture_output=True, text=True, check=False,
                          env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    info = home / "worktrees" / ".trash" / "info"
    [record] = info.iterdir()
    assert origin(record) == os.fsencode(project) + b"/" + name


@pytest.mark.parametrize("args, status", [
    ((), 64), (("--empty",), 64), (("-r", "x"), 64),
    (("{home}/elsewhere",), 1), (("{home}/worktrees",), 1),
    (("{home}/worktrees/project/absent",), 1), (("{home}/worktrees/.trash",), 1),
], ids=["none", "empty", "option", "outside", "root", "absent", "trash"])
def test_trash_refuses(home: Path, args: tuple[str, ...], status: int) -> None:
    (home / "elsewhere").write_text("x")
    (home / "worktrees" / ".trash").mkdir()
    before = sorted(str(p) for p in home.rglob("*"))
    proc = run_trash(home, *(a.format(home=home) for a in args))
    assert proc.returncode == status, proc.stderr
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_trash_refuses_what_is_already_in_the_trash(home: Path) -> None:
    project = home / "worktrees" / "project"
    (project / "f").write_text("x")
    assert run_trash(home, str(project / "f")).returncode == 0
    proc = run_trash(home, str(home / "worktrees" / ".trash" / "files" / "f"))
    assert proc.returncode == 1
    assert (home / "worktrees" / ".trash" / "files" / "f").exists()


def test_trash_empties_entries_older_than_thirty_days(home: Path) -> None:
    trash = home / "Repos" / ".trash"
    (trash / "files" / "old-dir").mkdir(parents=True)
    (trash / "files" / "old-dir" / "x").write_text("x")
    (trash / "files" / "recent").write_text("x")
    (trash / "info").mkdir()
    for name, days in (("old-dir", 31), ("recent", 29)):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
        (trash / "info" / f"{name}.trashinfo").write_text(
            f"[Trash Info]\nPath=/x/{name}\nDeletionDate={stamp}\n")
    (home / "worktrees" / "project" / "f").write_text("x")
    assert run_trash(home, str(home / "worktrees" / "project" / "f")).returncode == 0
    assert sorted(os.listdir(trash / "files")) == ["recent"]
    assert sorted(os.listdir(trash / "info")) == ["recent.trashinfo"]

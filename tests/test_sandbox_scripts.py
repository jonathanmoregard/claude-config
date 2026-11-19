"""unit-read, trash and `wait-for unit` run outside the Claude Code sandbox
without a prompt, so none may pass on an argument that steers the tool
underneath it, nor act on anything but what it checked."""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtime" / "scripts"
UNIT_READ = SCRIPTS / "unit-read"
TRASH = SCRIPTS / "trash"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
TRASH_DIR = REAL_HOME / ".local" / "share" / "Trash"
SCRATCH_PREFIX = f"{REAL_HOME}/worktrees/.trash-test-"


def staged(script: Path, tmp_path: Path, bin_dir: Path) -> Path:
    """The script with its fixed PATH searching bin_dir first."""
    source, n = re.subn(r"\bPATH=/run/current-system/sw/bin:",
                        f"PATH={bin_dir}:/run/current-system/sw/bin:", script.read_text())
    assert n == 1
    copy = tmp_path / script.name
    copy.write_text(source)
    return copy


def run_unit_read(tmp_path: Path, *args: str, output: str = "") -> tuple[int, list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    printed = tmp_path / "systemctl-output"
    printed.write_text(output)
    stub = bin_dir / "systemctl"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "systemctl $*" >>"{calls}"\ncat "{printed}"\n')
    stub.chmod(0o755)
    proc = subprocess.run(["bash", str(staged(UNIT_READ, tmp_path, bin_dir)), *args],
                          capture_output=True, text=True, check=False)
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


def trash_names() -> set[str]:
    info = TRASH_DIR / "info"
    return set(os.listdir(info)) if info.is_dir() else set()


def trashed_since(before: set[str]) -> list[tuple[str, str]]:
    """(original path, name under Trash/files), for this module's scratch trees only;
    other processes may be trashing concurrently."""
    moved = []
    for name in trash_names() - before:
        text = (TRASH_DIR / "info" / name).read_text()
        found = re.search(r"^Path=(.*)$", text, re.M)
        if found and unquote(found[1]).startswith(SCRATCH_PREFIX):
            moved.append((unquote(found[1]), name.removesuffix(".trashinfo")))
    return moved


def run_trash(*args: str, script: Path = TRASH) -> tuple[int, dict[str, str]]:
    before = trash_names()
    proc = subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True,
                          text=True, check=False)
    return proc.returncode, dict(trashed_since(before))


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@pytest.fixture
def scratch():
    root = Path(f"{SCRATCH_PREFIX}{uuid.uuid4().hex}")
    root.mkdir(parents=True)
    before = trash_names()
    yield root
    for original, name in trashed_since(before):
        if original.startswith(f"{root}/"):
            remove(TRASH_DIR / "files" / name)
            (TRASH_DIR / "info" / f"{name}.trashinfo").unlink()
    remove(root)


def test_trash_moves_paths_under_the_write_roots(scratch: Path) -> None:
    (scratch / "f").write_text("x")
    (scratch / "link").symlink_to("/etc/hostname")
    rc, moved = run_trash(str(scratch / "f"), str(scratch / "link"))
    assert rc == 0
    assert sorted(moved) == [f"{scratch}/f", f"{scratch}/link"]
    assert (TRASH_DIR / "files" / moved[f"{scratch}/f"]).read_text() == "x"
    assert os.readlink(TRASH_DIR / "files" / moved[f"{scratch}/link"]) == "/etc/hostname"
    assert not (scratch / "f").exists() and not (scratch / "link").is_symlink()


def test_trash_records_where_each_entry_came_from(scratch: Path) -> None:
    entry = scratch / "same name%"
    names = []
    for content in ("one", "two"):
        entry.write_text(content)
        rc, moved = run_trash(str(entry))
        assert rc == 0
        names.append(moved[str(entry)])
    assert [(TRASH_DIR / "files" / n).read_text() for n in names] == ["one", "two"]
    for name in names:
        info = (TRASH_DIR / "info" / f"{name}.trashinfo").read_text()
        parsed = re.fullmatch(r"\[Trash Info\]\nPath=(\S+)\n"
                              r"DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\n", info)
        assert parsed, info
        assert unquote(parsed[1]) == str(entry)


def test_trash_refuses_options_and_paths_elsewhere(scratch: Path) -> None:
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
        assert run_trash(*args) == (code, {}), args
    assert (scratch / "f").exists()


def test_trash_refuses_another_filesystem(scratch: Path) -> None:
    volume = scratch / "volume"
    if not shutil.which("btrfs") or subprocess.run(
            ["btrfs", "subvolume", "create", str(volume)], capture_output=True).returncode:
        pytest.skip("needs an unprivileged btrfs subvolume")
    try:
        (volume / "f").write_text("x")
        assert run_trash(str(volume / "f")) == (1, {})
        assert (volume / "f").exists()
    finally:
        (volume / "f").unlink(missing_ok=True)
        volume.rmdir()


def project(root: Path) -> Path:
    repo = root / "repo"
    for rel in (".git/hooks/pre-commit", ".git/config", ".git/objects/ab/cd",
                ".claude/settings.json", ".claude/hooks/guard.sh", ".claude/notes.md",
                ".mcp.json", "sub/ordinary.txt", "nested/deep/.zshrc"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x")
    (root / "wt").mkdir()
    (root / "wt" / ".git").write_text(f"gitdir: {repo}/.git\n")
    (root / "to-claude").symlink_to(repo / ".claude")
    (root / "linked").mkdir()
    (root / "linked" / ".claude").symlink_to(repo / ".claude")
    return repo


@pytest.mark.parametrize("rel", [
    "repo", "repo/.git", "repo/.git/hooks", "repo/.git/hooks/pre-commit", "repo/.git/config",
    "repo/.git/objects/ab", "wt", "wt/.git", "repo/.claude", "repo/.claude/settings.json",
    "repo/.claude/hooks", "repo/.claude/hooks/guard.sh", "repo/.mcp.json", "repo/nested",
    "repo/nested/deep/.zshrc", "to-claude/settings.json", "to-claude/hooks/guard.sh",
    "linked/.claude", "linked",
])
def test_trash_refuses_what_the_sandbox_protects(scratch: Path, rel: str) -> None:
    project(scratch)
    assert run_trash(str(scratch / rel)) == (1, {}), rel
    assert (scratch / rel).exists()


@pytest.mark.parametrize("rel", ["repo/sub", "repo/sub/ordinary.txt", "repo/.claude/notes.md",
                                 "to-claude"])
def test_trash_still_takes_ordinary_project_content(scratch: Path, rel: str) -> None:
    project(scratch)
    rc, moved = run_trash(str(scratch / rel))
    assert (rc, sorted(moved)) == (0, [f"{scratch}/{rel}"])
    assert not os.path.lexists(scratch / rel)


def walking(pid: int, root: Path, inside: bool = False) -> bool:
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if (target == str(root) and not inside) or target.startswith(f"{root}/"):
            return True
    return False


def await_walk(proc: subprocess.Popen, root: Path, inside: bool = False) -> None:
    deadline = time.monotonic() + 30
    while (proc.poll() is None and not walking(proc.pid, root, inside)
           and time.monotonic() < deadline):
        pass


def populate(tree: Path) -> None:
    for i in range(200):
        (tree / str(i)).mkdir(parents=True)
        for j in range(100):
            (tree / str(i) / str(j)).touch()


@pytest.mark.parametrize("victim", ["outside_home_ssh", "project_claude"])
def test_trash_moves_only_what_it_checked_despite_a_parent_swap(
        tmp_path: Path, scratch: Path, victim: str) -> None:
    decoy_home = tmp_path / "decoy-home"
    (decoy_home / ".ssh").mkdir(parents=True)
    (decoy_home / ".ssh" / "id_decoy").write_text("decoy")
    decoy_project = scratch / "decoy-project"
    (decoy_project / ".claude").mkdir(parents=True)
    (decoy_project / ".claude" / "settings.json").write_text("{}")
    swap_to, name = {"outside_home_ssh": (decoy_home, ".ssh"),
                     "project_claude": (decoy_project, ".claude")}[victim]
    parent = scratch / "a"
    tree = parent / name
    populate(tree)

    proc = subprocess.Popen([sys.executable, "-I", str(TRASH), str(tree)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        await_walk(proc, tree)
        swapped_mid_run = proc.poll() is None
        parent.rename(scratch / "a.real")
        parent.symlink_to(swap_to)
        _, err = proc.communicate(timeout=60)
    finally:
        if parent.is_symlink():
            parent.unlink()
    assert swapped_mid_run
    assert (swap_to / name).is_dir(), err
    assert list((swap_to / name).iterdir()), err
    assert not (scratch / "a.real" / name).exists(), err


@pytest.mark.parametrize("rel", [".git/config", ".claude/settings.json"])
def test_trash_takes_nothing_protected_moved_in_after_its_check(
        scratch: Path, rel: str) -> None:
    big = scratch / "big"
    populate(big)
    checked = scratch / "a"
    checked.mkdir()
    victim = scratch / "victim"
    (victim / rel).parent.mkdir(parents=True)
    (victim / rel).write_text("decoy")

    proc = subprocess.Popen([sys.executable, "-I", str(TRASH), str(checked), str(big)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    await_walk(proc, big)
    moved_mid_run = proc.poll() is None
    if checked.exists():
        victim.rename(checked / "victim")
    _, err = proc.communicate(timeout=60)
    assert moved_mid_run
    assert (victim / rel).exists() or (checked / "victim" / rel).exists(), err


@pytest.mark.parametrize("rel", [".git/config", ".claude/settings.json"])
def test_trash_puts_back_a_tree_that_gained_something_protected_during_its_walk(
        scratch: Path, rel: str) -> None:
    big = scratch / "big"
    populate(big)
    victim = scratch / "victim"
    (victim / rel).parent.mkdir(parents=True)
    (victim / rel).write_text("decoy")

    before = trash_names()
    proc = subprocess.Popen([sys.executable, "-I", str(TRASH), str(big)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    await_walk(proc, big, inside=True)
    moved_mid_run = proc.poll() is None
    victim.rename(big / "victim")
    _, err = proc.communicate(timeout=60)
    assert moved_mid_run
    assert trashed_since(before) == [], err
    assert (big / "victim" / rel).exists(), err
    assert proc.returncode == 1, err


@pytest.mark.parametrize("occupied", [True, False])
def test_trash_put_back_onto_a_recreated_directory_leaves_no_entry(
        tmp_path: Path, scratch: Path, occupied: bool) -> None:
    layout = tmp_path / "layout"
    (layout / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS.parent / "settings.json", layout / "settings.json")
    gate = tmp_path / "gate"
    gate.mkdir()

    def pause(step: str) -> str:
        return (f"open({str(gate / step)!r} + '.ready', 'w').close()\n"
                f"    while not os.path.exists({str(gate / step)!r} + '.go'): time.sleep(0.01)\n    ")

    source = TRASH.read_text()
    first = "    unique = reserve(info_fd, files_fd, target)\n"
    second = "    moved = os.stat(unique, dir_fd=files_fd, follow_symlinks=False)\n"
    assert source.count(first) == source.count(second) == 1
    source = source.replace(first, "    " + pause("swap") + first.lstrip())
    source = source.replace(second, "    " + pause("recreate") + second.lstrip())
    script = layout / "scripts" / "trash"
    script.write_text(source)

    def await_step(step: str) -> None:
        deadline = time.monotonic() + 30
        while not (gate / f"{step}.ready").exists() and proc.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert proc.poll() is None

    target = scratch / "a"
    target.mkdir()
    before = trash_names()
    proc = subprocess.Popen([sys.executable, "-I", str(script), str(target)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    await_step("swap")
    target.rename(scratch / "a.checked")
    target.mkdir()
    (gate / "swap.go").touch()
    await_step("recreate")
    target.mkdir()
    if occupied:
        (target / "occupant").write_text("x")
    recreated = target.stat().st_ino
    (gate / "recreate.go").touch()
    _, err = proc.communicate(timeout=30)
    assert trashed_since(before) == [], err
    assert "Traceback" not in err, err
    assert proc.returncode == 1, err
    assert target.stat().st_ino == recreated, err
    assert len(list(scratch.glob("a.restored-*"))) == 1, err


def test_trash_honours_the_profile_deny_write(tmp_path: Path, scratch: Path) -> None:
    layout = tmp_path / "layout"
    (layout / "scripts").mkdir(parents=True)
    script = layout / "scripts" / "trash"
    shutil.copy2(TRASH, script)
    held = scratch / "outer" / "held"
    (held / "inner").mkdir(parents=True)
    (scratch / "outer" / "free").write_text("x")
    (scratch / "tilde").write_text("x")
    tilde = f"~/{scratch.relative_to(REAL_HOME)}/tilde"
    (layout / "settings.json").write_text(json.dumps(
        {"sandbox": {"filesystem": {"denyWrite": [str(held), tilde]}}}))
    for target in (held, held / "inner", scratch / "outer", scratch / "tilde"):
        assert run_trash(str(target), script=script) == (1, {}), target
    rc, moved = run_trash(str(scratch / "outer" / "free"), script=script)
    assert (rc, sorted(moved)) == (0, [f"{scratch}/outer/free"])

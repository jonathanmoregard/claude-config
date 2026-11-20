"""`confined` runs outside the Claude Code sandbox without a prompt, so its own
jail is the only thing between agent-written code and the host. Real bwrap and
pasta, no mocks. Every jailed script ends by printing RAN: a jail that never
started prints nothing, and "the secret is not in the output" would pass on that.

The script reads the settings.json beside its own config tree, so each test
copies it into a scratch tree with a scratch settings.json. Trees and plants
live in the working repository: home, /tmp, /run and /var are replaced inside
the jail, and a nested call must still see its tree."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtime" / "scripts"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
SHARED_TMP = Path(f"/tmp/confined-{os.getuid()}")
BWRAP = shutil.which("bwrap")
PASTA = shutil.which("pasta")
RAN = "confined-ran-marker"
CANARY = "confined-canary-not-a-secret"
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]

INTERPRETER = (SCRIPTS / "confined").read_text().splitlines()[0].removeprefix("#!").split()[0]

pytestmark = pytest.mark.skipif(
    BWRAP is None or not os.path.exists(INTERPRETER),
    reason="bubblewrap or the launcher's interpreter is not installed here")
needs_pasta = pytest.mark.skipif(PASTA is None, reason="pasta is not installed here")


def make_tree(config: Path, settings: dict | None, search: tuple[str, ...] | None = None) -> Path:
    (config / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "record-user-unit-failure", config / "scripts")
    source = (SCRIPTS / "confined").read_text()
    if search is not None:
        source, n = re.subn(r"^SEARCH = \(.*?\)$", f"SEARCH = {search!r}", source,
                            flags=re.M | re.S)
        assert n == 1
    (config / "scripts" / "confined").write_text(source)
    (config / "scripts" / "confined").chmod(0o755)
    if settings is not None:
        (config / "settings.json").write_text(json.dumps(settings))
    return config / "scripts" / "confined"


def base_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SANDBOX_RUNTIME", "CLAUDECODE")}
    env.update(extra)
    return env


def run(script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script), *args], cwd=cwd, env=env or base_env(),
                          capture_output=True, text=True, timeout=timeout, check=False)


def jailed(script: Path, body: str, *, cwd: Path, env: dict[str, str] | None = None,
           flags: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    proc = run(script, *flags, "--", "bash", "-c", f"{body}\necho {RAN}", cwd=cwd, env=env)
    assert proc.stdout.rstrip().endswith(RAN), (proc.returncode, proc.stdout, proc.stderr)
    return proc


def answers(proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), *GIT_ID, "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture
def scratch():
    """A directory in the real home, under a real write root, so a write
    there proves the bind."""
    home = REAL_HOME / "worktrees" / f".confined-test-{uuid.uuid4().hex[:12]}"
    home.mkdir(parents=True)
    yield home
    shutil.rmtree(home)


@pytest.fixture
def world(scratch):
    home = scratch
    rw = home / "rw"
    rw.mkdir()
    repo = git_repo(rw / "proj")
    other = git_repo(rw / "other")
    var = repo / "data"
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("data/\n.env.local\n")
    for d in (repo / "locked", other / ".claude", home / "outside", var / "denied",
              var / "vault.d" / "1", var / "cred"):
        d.mkdir(parents=True)
    (other / ".claude" / "settings.local.json").write_text("{}\n")
    plants = {
        "denied_dir": var / "denied" / "secret",
        "denied_file": var / "denied-file",
        "glob_file": var / "sock-1",
        "cred": var / "cred" / "token",
        "vault": var / "vault.d" / "1" / "secret",
        "read_glob": var / "rule-1",
        "repo_env": repo / ".env.local",
        "home_outside": home / "outside" / "secret",
        "other_repo": other / "tracked.txt",
        "plain": var / "plain",
    }
    for path in plants.values():
        path.write_text(CANARY + "\n")
    (var / "vault").symlink_to(var / "vault.d")
    settings = {
        "sandbox": {
            "filesystem": {
                "allowWrite": [str(rw), "~/.confined-test-missing-root"],
                "denyWrite": [str(repo / "locked")],
                "denyRead": [str(var / "denied"), str(var / "denied-file"),
                             str(var / "sock-*"), "/nonexistent/confined-test"],
            },
            "credentials": {
                "files": [{"path": str(plants["cred"]), "mode": "mask"}],
                "envVars": [{"name": "CONFINED_TEST_DENIED", "mode": "deny"}],
            },
        },
        "permissions": {"deny": [  # arftl-allow: permission-widened
            f"Read(/{var}/vault.d/**)",
            f"Read(/{var}/rule-*)",
            f"Read(/{repo}/.env*)",
            "Read(//**/.git/**)",
            "Read(~/.confined-test-missing-file)",
            "Read(~/.claude/.credentials.json)",
            "Bash(rm -rf *)",  # arftl-allow: destructive-rm
        ]},
    }
    script = make_tree(var / "tree", settings)
    return {"script": script, "settings": settings, "var": var, "home": home, "rw": rw,
            "repo": repo, "other": other, "plants": plants}


@needs_pasta
def test_hides_denied_paths_credentials_read_rules_and_home(world) -> None:
    p = world["plants"]
    proc = jailed(world["script"], "\n".join(
        f'printf "{name}=%s\\n" "$(cat {path} 2>/dev/null)"' for name, path in p.items()
    ) + f'\nprintf "vault_ls=%s\\n" "$(ls -A {world["var"]}/vault.d)"'
        f'\nprintf "link=%s\\n" "$(cat {world["var"]}/vault/1/secret 2>/dev/null)"',
        cwd=world["repo"])
    lines = answers(proc)
    assert lines.pop("plain") == CANARY, "control: an ordinary file must stay readable"
    assert set(lines.values()) == {""}, lines


@needs_pasta
def test_home_keeps_only_what_the_jail_needs(world) -> None:
    sensitive = [REAL_HOME / rel for rel in (".zsh_history", ".bash_history", ".ssh",
                                             ".claude/projects", ".config/gh", "Repos")]
    proc = jailed(world["script"], "\n".join(
        f'[ -e "{path}" ] && echo "{path}=visible"' for path in sensitive),
        cwd=world["repo"])
    assert "=visible" not in proc.stdout, proc.stdout


@needs_pasta
def test_hides_denied_env(world) -> None:
    proc = jailed(world["script"],
                  'printf "denied=%s kept=%s\\n" "${CONFINED_TEST_DENIED:-}" "${CONFINED_TEST_KEPT:-}"',
                  cwd=world["repo"],
                  env=base_env(CONFINED_TEST_DENIED=CANARY, CONFINED_TEST_KEPT="kept"))
    assert "denied= kept=kept" in proc.stdout


CLAUDE_CREDS = REAL_HOME / ".claude" / ".credentials.json"
# Sizes and access(2) only: neither test reads the credential.
CREDS_PROBE = (f'printf "size=%s\\n" "$(wc -c <{CLAUDE_CREDS} 2>/dev/null)"\n'
               f'[ -w {CLAUDE_CREDS} ] && echo writable=yes || echo writable=no')


@pytest.mark.skipif(not CLAUDE_CREDS.is_file(), reason="no Claude credentials file here")
@needs_pasta
def test_claude_login_makes_the_credentials_usable_despite_a_read_rule(world) -> None:
    proc = jailed(world["script"], CREDS_PROBE, cwd=world["repo"], flags=("--claude-login",))
    assert "writable=yes" in proc.stdout
    assert re.search(r"^size=[1-9]", proc.stdout, re.M), proc.stdout


@pytest.mark.skipif(not CLAUDE_CREDS.is_file(), reason="no Claude credentials file here")
@needs_pasta
def test_a_command_gets_no_claude_credentials_unless_asked(world) -> None:
    before = CLAUDE_CREDS.stat()
    proc = jailed(world["script"], CREDS_PROBE + f"\necho x >>{CLAUDE_CREDS} 2>/dev/null",
                  cwd=world["repo"])
    assert re.search(r"^size=0?$", proc.stdout, re.M), proc.stdout
    after = CLAUDE_CREDS.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


@needs_pasta
def test_a_linked_credentials_file_outside_home_stays_hidden_without_the_flag(world) -> None:
    home = world["home"]
    links = {"creds": (home / "decoy-creds.json", Path("/etc/hostname")),
             "control": (home / "decoy-other.json", Path("/etc/machine-id"))}
    for link, target in links.values():
        link.symlink_to(target)
        world["settings"]["permissions"]["deny"].append(f"Read(/{link})")
    script = make_tree(world["var"] / "tree2", world["settings"])
    source = script.read_text()
    assert source.count(".claude/.credentials.json") == 1
    script.write_text(source.replace(".claude/.credentials.json",
                                     str(links["creds"][0].relative_to(REAL_HOME))))
    proc = jailed(script, "\n".join(
        f'printf "{name}=%s\\n" "$(wc -c <{os.path.realpath(target)} 2>/dev/null)"'
        for name, (_, target) in links.items()), cwd=world["repo"])
    lines = answers(proc)
    assert lines["control"] in ("", "0"), lines
    assert lines["creds"] in ("", "0"), lines


@needs_pasta
def test_claude_login_is_only_accepted_as_the_first_argument(world) -> None:
    proc = run(world["script"], "--chdir", str(world["repo"]), "--claude-login", "--", "true",
               cwd=world["repo"])
    assert proc.returncode == 64, proc.stderr


@needs_pasta
def test_a_refused_launch_leaves_the_checkout_as_it_was(world) -> None:
    script = make_tree(world["var"] / "tree2", world["settings"])
    source = script.read_text()
    marking = 'os.setxattr(fd, STUB_MARK, b"1")'
    assert source.count(marking) == 1
    script.write_text(source.replace(marking, 'raise OSError(95, "injected")'))
    before = sorted(str(p) for p in world["repo"].rglob("*"))
    proc = run(script, "--", "true", cwd=world["repo"])
    assert proc.returncode == 3, proc.stderr
    assert sorted(str(p) for p in world["repo"].rglob("*")) == before


@needs_pasta
def test_a_refused_launch_leaves_a_file_that_replaced_its_guard(world) -> None:
    script = make_tree(world["var"] / "tree2", world["settings"])
    source = script.read_text()
    marking = 'os.setxattr(fd, STUB_MARK, b"1")'
    assert source.count(marking) == 1
    script.write_text(source.replace(
        marking,
        'if content is not None: os.unlink(tmp, dir_fd=parent); '
        'os.write(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent), '
        'b"theirs"); raise OSError(95, "injected")\n'
        '            ' + marking))
    proc = run(script, "--", "true", cwd=world["repo"])
    assert proc.returncode == 3, proc.stderr
    kept = [p for p in world["repo"].rglob("*") if p.is_file() and p.read_bytes() == b"theirs"]
    assert len(kept) == 1, kept
    kept[0].unlink()


@needs_pasta
def test_a_stop_while_guards_are_placed_leaves_the_checkout_as_it_was(world) -> None:
    script = make_tree(world["var"] / "tree2", world["settings"])
    source = script.read_text()
    created = "os.mkdir(tmp, 0o755, dir_fd=parent)"
    assert source.count(created) == 1
    script.write_text(source.replace(created, f"{created}; os.kill(os.getpid(), signal.SIGTERM)"))
    before = sorted(str(p) for p in world["repo"].rglob("*"))
    proc = run(script, "--", "true", cwd=world["repo"])
    assert proc.returncode == 128 + signal.SIGTERM, proc.stderr
    assert sorted(str(p) for p in world["repo"].rglob("*")) == before


def injected(world, name: str, old: str, new: str) -> Path:
    script = make_tree(world["var"] / name, world["settings"])
    source = script.read_text()
    assert source.count(old) == 1, old
    script.write_text(source.replace(old, new))
    return script


def wait_until(condition, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def writes_mcp_json_later(repo: Path, delay: int) -> list[str]:
    return ["--", "bash", "-c",
            f'sleep {delay}; ( echo x >{repo}/.mcp.json ) 2>/dev/null && echo created; echo {RAN}']


@needs_pasta
def test_a_guard_still_being_placed_is_not_taken_for_real_config(world) -> None:
    repo = world["repo"]
    marking = 'os.setxattr(fd, STUB_MARK, b"1")'
    slow = injected(world, "slow", marking,
                    f'time.sleep(3 if name == ".mcp.json" else 0); {marking}')
    placing = subprocess.Popen([str(slow), "--", "true"], cwd=repo, env=base_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_until(lambda: os.path.lexists(repo / ".mcp.json"))
    later = run(world["script"], *writes_mcp_json_later(repo, 6), cwd=repo)
    placing.communicate(timeout=60)
    assert later.stdout.rstrip().endswith(RAN) and "created" not in later.stdout, later.stdout
    assert not (repo / ".mcp.json").exists()


@needs_pasta
def test_an_adopted_guard_replaced_before_it_is_locked_is_not_bound_unlocked(world) -> None:
    repo = world["repo"]
    adopting = "fd = self.adopt(parent, name)"
    slow = injected(world, "slow", adopting,
                    f'{adopting}; time.sleep(5 if name == ".mcp.json" else 0)')
    first = subprocess.Popen([str(world["script"]), "--", "sleep", "2"], cwd=repo,
                             env=base_env(), stdout=subprocess.PIPE, text=True)
    wait_until(lambda: os.path.lexists(repo / ".mcp.json"))
    adopter = subprocess.Popen([str(slow), *writes_mcp_json_later(repo, 10)], cwd=repo,
                               env=base_env(), stdout=subprocess.PIPE, text=True)
    first.communicate(timeout=60)
    wait_until(lambda: not os.path.lexists(repo / ".mcp.json"))
    third = run(world["script"], "--", "sleep", "4", cwd=repo)
    out, _ = adopter.communicate(timeout=60)
    assert third.returncode == 0, third.stderr
    assert out.rstrip().endswith(RAN) and "created" not in out, out
    assert not (repo / ".mcp.json").exists()


@needs_pasta
def test_a_guard_swapped_before_it_is_opened_is_not_removed(world) -> None:
    repo = world["repo"]
    created = re.search(r"os\.mkdir\((\w+), 0o755, dir_fd=parent\)",
                        (SCRIPTS / "confined").read_text())
    assert created
    entry = created[1]
    swap = (f"{created[0]}; os.rename({entry}, {entry} + '.ours', src_dir_fd=parent, "
            f"dst_dir_fd=parent); os.mkdir({entry}, 0, dir_fd=parent)")
    script = injected(world, "swap", created[0], swap)
    proc = run(script, "--", "true", cwd=repo)
    assert proc.returncode == 3, proc.stderr
    theirs = [p for p in repo.iterdir() if p.is_dir() and p.stat().st_mode & 0o777 == 0]
    for p in theirs:
        p.chmod(0o755)
    assert len(theirs) == 1, sorted(repo.iterdir())


@needs_pasta
def test_a_stop_just_after_the_jail_starts_leaves_the_checkout_as_it_was(world) -> None:
    started = "preexec_fn=lambda: (die_with_parent(), net.enter()))"
    script = injected(world, "stop", started,
                      f"{started}\n    os.kill(os.getpid(), signal.SIGTERM)")
    before = sorted(str(p) for p in world["repo"].rglob("*"))
    proc = run(script, "--", "sleep", "30", cwd=world["repo"])
    assert proc.returncode == 128 + signal.SIGTERM, proc.stderr
    assert sorted(str(p) for p in world["repo"].rglob("*")) == before


@needs_pasta
def test_a_second_stop_while_guards_are_removed_leaves_the_checkout_as_it_was(world) -> None:
    locking = "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
    script = make_tree(world["var"] / "stop2", world["settings"])
    source = script.read_text()
    for old, new in ((locking, f"{locking}; os.kill(os.getpid(), signal.SIGTERM)"),
                     ("net = Network(tools)",
                      "os.kill(os.getpid(), signal.SIGTERM); net = Network(tools)")):
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    script.write_text(source)
    before = sorted(str(p) for p in world["repo"].rglob("*"))
    proc = run(script, "--", "true", cwd=world["repo"])
    assert proc.returncode == 128 + signal.SIGTERM, proc.stderr
    assert sorted(str(p) for p in world["repo"].rglob("*")) == before


@needs_pasta
def test_has_its_own_pid_and_ipc_namespaces(world) -> None:
    ns = {k: os.readlink(f"/proc/self/ns/{k}") for k in ("pid", "ipc")}
    proc = jailed(world["script"],
                  f'[ -e /proc/{os.getpid()} ] && echo sees-host-pid\n'
                  'echo "pid=$(readlink /proc/self/ns/pid) ipc=$(readlink /proc/self/ns/ipc)"',
                  cwd=world["repo"])
    assert "sees-host-pid" not in proc.stdout
    assert ns["pid"] not in proc.stdout and ns["ipc"] not in proc.stdout


@needs_pasta
def test_writes_reach_only_the_working_repo_and_the_shared_tmp(world) -> None:
    rw, repo, tag = world["rw"], world["repo"], uuid.uuid4().hex[:8]
    targets = {
        "repo": repo / f"new-{tag}",
        "shared_tmp": SHARED_TMP / f"new-{tag}",
        "write_root": rw / f"new-{tag}",
        "other_repo": world["other"] / "tracked.txt",
        "other_settings": world["other"] / ".claude" / "settings.local.json",
        "deny_write": repo / "locked" / "new",
        "var_tmp": Path(f"/var/tmp/confined-test-{tag}"),
        "home_top": REAL_HOME / f".confined-test-{tag}",
        "host_tmp": Path(f"/tmp/confined-test-{tag}"),
    }
    try:
        jailed(world["script"], "\n".join(f'echo x >>{path} 2>/dev/null' for path in targets.values()),
               cwd=repo)
        landed = {name for name, path in targets.items()
                  if path.exists() and "x" in path.read_text()}
    finally:
        for name in ("shared_tmp", "home_top", "host_tmp", "var_tmp"):
            targets[name].unlink(missing_ok=True)
    assert landed == {"repo", "shared_tmp"}


@needs_pasta
def test_host_tmp_is_not_visible_even_entries_created_later(world) -> None:
    tag = uuid.uuid4().hex[:8]
    early, late = Path(f"/tmp/confined-early-{tag}"), Path(f"/tmp/confined-late-{tag}.sock")
    early.write_text(CANARY)
    listener = socket.socket(socket.AF_UNIX)
    try:
        proc = subprocess.Popen(
            [str(world["script"]), "--", "bash", "-c",
             f'sleep 3; ls -A /tmp; [ -e {early} ] && echo early=seen; [ -e {late} ] && echo late=seen; '
             f'echo {RAN}'],
            cwd=world["repo"], env=base_env(), stdout=subprocess.PIPE, text=True)
        time.sleep(1)
        listener.bind(str(late))
        out, _ = proc.communicate(timeout=60)
    finally:
        listener.close()
        early.unlink()
        late.unlink(missing_ok=True)
    assert out.rstrip().endswith(RAN), out
    assert "=seen" not in out, out


PROTECTED_ATTEMPTS = {
    "settings": "echo x >>{checkout}/.claude/settings.local.json",
    "settings_mv": "echo {{}} >{checkout}/.claude/new && mv {checkout}/.claude/new "
                   "{checkout}/.claude/settings.local.json",
    "hook": "echo x >>{common}/hooks/pre-commit",
    "new_hook": ": >{common}/hooks/post-checkout",
    "config": "echo x >>{common}/config",
    "tracked": "echo v2 >>{checkout}/tracked.txt",
}


@pytest.fixture
def worktrees(world):
    main = world["repo"]
    wt = world["rw"] / "wt"
    (main / "sub").mkdir()
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.local.json").write_text("{}\n")
    (main / ".git" / "hooks").mkdir(exist_ok=True)
    (main / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    (wt / "sub").mkdir()
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "settings.local.json").write_text("{}\n")
    return {"main": main, "wt": wt, "common": main / ".git"}


@pytest.mark.parametrize("where", ["main", "main/sub", "wt", "wt/sub"])
@needs_pasta
def test_protects_agent_and_git_config_in_the_cwd_repo(world, worktrees, where: str) -> None:
    checkout = worktrees[where.split("/")[0]]
    proc = jailed(world["script"], "\n".join(
        f'( {cmd.format(checkout=checkout, common=worktrees["common"])} ) 2>/dev/null '
        f'&& echo {name}=yes || echo {name}=no'
        for name, cmd in PROTECTED_ATTEMPTS.items()), cwd=checkout / where.partition("/")[2])
    assert {"settings=no", "settings_mv=no", "hook=no", "new_hook=no", "config=no",
            "tracked=yes"} <= set(proc.stdout.split()), proc.stdout


@pytest.mark.parametrize("where", ["main", "wt"])
@needs_pasta
def test_protected_directories_cannot_be_renamed_away(world, worktrees, where: str) -> None:
    """Moving a protected entry's parent aside and recreating it would undo
    the protection; so would repointing a worktree at another git dir."""
    checkout, common = worktrees[where], worktrees["common"]
    targets = {"top": checkout, "claude": checkout / ".claude", "dotgit": checkout / ".git",
               "common": common}
    edits = {}
    if where == "wt":
        targets["gitdir"] = common / "worktrees" / "wt"
        edits = {"commondir": common / "worktrees" / "wt" / "commondir",
                 "dotgit_file": checkout / ".git"}
    proc = jailed(world["script"], "\n".join(
        [f'( mv {path} {path}.moved ) 2>/dev/null && echo {name}=moved || echo {name}=held'
         for name, path in targets.items()] +
        [f'( echo x >>{path} ) 2>/dev/null && echo {name}=moved || echo {name}=held'
         for name, path in edits.items()]), cwd=checkout / "sub")
    assert "=moved" not in proc.stdout, proc.stdout


ABSENT = [".mcp.json", ".claude/settings.json", ".claude/hooks/x", ".claude/skills/x/SKILL.md",
          ".vscode/settings.json", ".git/commondir", ".git/config.worktree"]


@pytest.mark.parametrize("claude_dir", [False, True])
@needs_pasta
def test_absent_protected_paths_cannot_be_created_and_leave_no_stub(world, claude_dir: bool) -> None:
    repo = world["repo"]
    if claude_dir:
        (repo / ".claude").mkdir()
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))
    proc = jailed(world["script"], "\n".join(
        f'( mkdir -p "$(dirname {repo}/{rel})" && echo x >{repo}/{rel} ) 2>/dev/null '
        f'&& echo "{rel}=created"'
        for rel in ABSENT) +
        f'\n( mv {repo}/.claude {repo}/.claude.moved ) 2>/dev/null && echo ".claude=created"'
        f'\n( echo x >{repo}/ordinary ) && echo ordinary=yes',
        cwd=repo)
    assert "=created" not in proc.stdout and "ordinary=yes" in proc.stdout, proc.stdout
    (repo / "ordinary").unlink()
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before


@needs_pasta
def test_a_guard_outlives_a_shorter_concurrent_jail(world) -> None:
    repo = world["repo"]
    long = subprocess.Popen(
        [str(world["script"]), "--", "bash", "-c",
         f'sleep 4; ( echo x >{repo}/.mcp.json ) 2>/dev/null && echo created; echo {RAN}'],
        cwd=repo, env=base_env(), stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    short = run(world["script"], "--", "true", cwd=repo)
    out, _ = long.communicate(timeout=60)
    assert short.returncode == 0, short.stderr
    assert out.rstrip().endswith(RAN) and "created" not in out, out
    assert not (repo / ".mcp.json").exists()


@pytest.mark.parametrize("entry", [".claude", ".claude/settings.json", ".mcp.json"])
@needs_pasta
def test_refuses_a_repo_whose_protected_entry_is_a_symlink(world, entry: str) -> None:
    repo, marker = world["repo"], world["var"] / "should-not-exist"
    target = world["home"] / "outside" / "target"
    target.mkdir()
    (repo / entry).parent.mkdir(parents=True, exist_ok=True)
    (repo / entry).symlink_to(target)
    proc = run(world["script"], "--", "touch", str(marker), cwd=repo)
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("case", ["write_root", "planted_git_above_root", "outside_roots",
                                  "deny_write", "tmp_itself", "gitdir_outside_roots",
                                  "gitdir_inside_the_repo"])
@needs_pasta
def test_refuses_a_working_directory_it_cannot_confine_to_one_repo(world, case: str) -> None:
    rw, home = world["rw"], world["home"]
    marker = world["var"] / "should-not-exist"
    if case == "write_root":
        cwd = rw
    elif case == "planted_git_above_root":
        (home / ".git").mkdir()
        (rw / "plain").mkdir()
        cwd = rw / "plain"
    elif case == "outside_roots":
        cwd = git_repo(home / "outside" / "repo")
    elif case == "deny_write":
        cwd = world["repo"] / "locked"
    elif case == "tmp_itself":
        cwd = Path("/tmp")
    elif case == "gitdir_inside_the_repo":
        cwd = rw / "inner"
        (cwd / "gd").mkdir(parents=True)
        (cwd / "gd" / "HEAD").write_text("ref: refs/heads/main\n")
        (cwd / ".git").write_text(f"gitdir: {cwd}/gd\n")
    else:
        stray = git_repo(home / "outside" / "stray")
        (rw / "pointer").mkdir()
        (rw / "pointer" / ".git").write_text(f"gitdir: {stray}/.git\n")
        cwd = rw / "pointer"
    proc = run(world["script"], "--", "touch", str(marker), cwd=cwd)
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("layout", ["own_git_dir", "pointed_git_dir"])
@needs_pasta
def test_a_git_pointer_linked_to_a_hidden_file_tells_nothing_of_it(world, layout: str) -> None:
    secret = world["plants"]["cred"]
    assert CANARY in secret.read_text()
    cwd = world["rw"] / "linked"
    gitdir = cwd / ".git" if layout == "own_git_dir" else world["rw"] / "elsewhere"
    cwd.mkdir(exist_ok=True)
    gitdir.mkdir(exist_ok=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    (gitdir / "commondir").symlink_to(secret)
    if layout == "pointed_git_dir":
        (cwd / ".git").write_text(f"gitdir: {gitdir}\n")
    proc = run(world["script"], "--", "true", cwd=cwd)
    assert CANARY not in proc.stdout + proc.stderr


@pytest.mark.parametrize("plant", ["commondir", "gitfile", "worktree_entry", "bare_layout"])
@needs_pasta
def test_a_planted_git_pointer_makes_no_other_repository_writable(world, plant: str) -> None:
    rw, other = world["rw"], world["other"]
    cwd = rw / "planted"
    cwd.mkdir()
    if plant == "commondir":
        (cwd / ".git").mkdir()
        (cwd / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (cwd / ".git" / "commondir").write_text(f"{other}/.git\n")
    elif plant == "gitfile":
        (cwd / ".git").write_text(f"gitdir: {other}/.git\n")
    elif plant == "worktree_entry":
        subprocess.run(["git", "-C", str(other), "worktree", "add", "-q", str(rw / "other-wt")],
                       check=True)
        (cwd / ".git").write_text(f"gitdir: {other}/.git/worktrees/other-wt\n")
    else:
        entry = other / "worktrees" / "n"
        entry.mkdir(parents=True)
        (entry / "HEAD").write_text("ref: refs/heads/main\n")
        (entry / "commondir").write_text("../..\n")
        (entry / "gitdir").write_text(f"{cwd}/.git\n")
        (cwd / ".git").write_text(f"gitdir: {entry}\n")
    targets = [other / "tracked.txt", other / ".claude" / "settings.local.json",
               other / ".git" / "hooks" / "pre-commit", other / ".git" / "config",
               other / ".git" / "refs" / "heads" / "planted", other / ".mcp.json"]
    before = {t: t.read_bytes() if t.exists() else None for t in targets}
    proc = run(world["script"], "--", "bash", "-c",
               "\n".join(f"echo x >>{t} 2>/dev/null" for t in targets) + f"\necho {RAN}", cwd=cwd)
    assert proc.returncode == 64 or proc.stdout.rstrip().endswith(RAN), proc.stderr
    assert {t: t.read_bytes() if t.exists() else None for t in targets} == before


@needs_pasta
def test_a_git_dir_swapped_for_a_link_mid_launch_exposes_nothing(world) -> None:
    rw, secret_dir = world["rw"], world["plants"]["denied_dir"].parent
    cwd, gitdir, parked = rw / "planted", rw / "gd", rw / "gd.real"
    cwd.mkdir()
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    (cwd / ".git").write_text(f"gitdir: {gitdir}\n")
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            gitdir.rename(parked)
            gitdir.symlink_to(secret_dir)
            time.sleep(0.01)
            gitdir.unlink()
            parked.rename(gitdir)
            time.sleep(0.01)

    swapper = threading.Thread(target=swap)
    swapper.start()
    try:
        seen = [run(world["script"], "--", "sh", "-c",
                    f"cat {gitdir}/secret 2>/dev/null; echo {RAN}", cwd=cwd).stdout
                for _ in range(25)]
        assert swapper.is_alive()
    finally:
        stop.set()
        swapper.join()
    assert CANARY not in "".join(seen)
    assert any(RAN in out for out in seen)


@needs_pasta
def test_the_command_inherits_no_descriptor_of_the_host(world) -> None:
    proc = jailed(world["script"],
                  'for fd in /proc/self/fd/*; do echo "fd=$(readlink "$fd")"; done', cwd=world["repo"])
    opened = [line[3:] for line in proc.stdout.splitlines() if line.startswith("fd=") and line[3:]]
    assert opened
    assert not [o for o in opened if not o.startswith(("/dev/", "/proc/", "pipe:"))], opened


def session_in(repo: Path) -> Path:
    project = repo / "pkg" / "app"
    project.mkdir(parents=True)
    return project


@needs_pasta
def test_refuses_a_session_project_below_the_top_of_the_repository(world) -> None:
    project, marker = session_in(world["repo"]), world["var"] / "should-not-exist"
    proc = run(world["script"], "--session", str(project), "--", "touch", str(marker),
               cwd=world["repo"])
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@needs_pasta
def test_refuses_a_repository_inside_the_session_project(world) -> None:
    project = world["repo"]
    inner = project / ".claude"
    (inner / ".git").mkdir(parents=True)
    (inner / "settings.local.json").write_text("{}\n")
    proc = run(world["script"], "--session", str(project), "--", "sh", "-c",
               "echo x >>settings.local.json", cwd=inner)
    assert proc.returncode == 64, proc.stderr
    assert (inner / "settings.local.json").read_text() == "{}\n"


@pytest.mark.parametrize("where, status", [("project", 64), ("repo", 0)])
@needs_pasta
def test_the_session_project_is_found_among_the_ancestors(world, where: str, status: int) -> None:
    project = session_in(world["repo"])
    launcher = world["var"] / "claude" / "versions" / "launcher"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("bash"), launcher)
    proc = subprocess.run(
        [str(launcher), "-c", f"(cd {world['repo']} && {world['script']} -- true); exit $?"],
        cwd=project if where == "project" else world["repo"], env=base_env(),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == status, proc.stderr


@needs_pasta
def test_refuses_when_the_session_project_could_be_moved_into_the_repository(world) -> None:
    project = world["rw"] / "group" / "session"
    project.mkdir(parents=True)
    marker = world["var"] / "should-not-exist"
    proc = run(world["script"], "--session", str(project), "--", "touch", str(marker),
               cwd=world["repo"])
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@needs_pasta
def test_a_session_project_beside_the_repository_is_out_of_reach(world) -> None:
    project = world["other"]
    before = (project / "tracked.txt").read_text()
    proc = run(world["script"], "--session", str(project), "--", "sh", "-c",
               f"echo x >>{project}/tracked.txt 2>/dev/null; ls {project} 2>/dev/null | wc -l",
               cwd=world["repo"])
    assert (proc.returncode, proc.stdout.strip()) == (0, "0"), proc.stderr
    assert (project / "tracked.txt").read_text() == before


@needs_pasta
def test_git_reads_work_from_a_linked_worktree_but_its_git_dir_is_read_only(world, worktrees) -> None:
    wt, common = worktrees["wt"], worktrees["common"]
    proc = jailed(world["script"],
                  'echo "status=$(git status --porcelain >/dev/null 2>&1; echo $?)"\n'
                  'echo "log=$(git log --oneline -1 >/dev/null 2>&1; echo $?)"\n'
                  f'echo x >>{common}/refs/heads/planted 2>/dev/null\necho "ref=$?"', cwd=wt)
    got = answers(proc)
    assert (got["status"], got["log"]) == ("0", "0"), proc.stdout
    assert got["ref"] != "0" and not (common / "refs" / "heads" / "planted").exists()


@needs_pasta
def test_worktree_and_module_guards_survive_renaming_their_parent(world, worktrees) -> None:
    git = worktrees["common"]
    admin = git / "worktrees" / "wt"
    module = git / "modules" / "m"
    nested = module / "modules" / "n"
    for gitdir in (module, nested):
        (gitdir / "hooks").mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        (gitdir / "config").write_text("[core]\n")
    guarded = [admin / "commondir", admin / "config.worktree", module / "config",
               module / "hooks" / "post-checkout", nested / "config"]
    before = {p: p.read_bytes() if p.exists() else None for p in guarded}
    parents = [admin, git / "worktrees", nested, module / "modules", module, git / "modules"]
    proc = jailed(world["script"], "\n".join(
        [f"( mv {d} {d}.moved ) 2>/dev/null && echo moved={d}" for d in parents] +
        [f"( mkdir -p {p.parent} && echo planted >{p} ) 2>/dev/null" for p in guarded]),
        cwd=worktrees["main"])
    assert "moved=" not in proc.stdout, proc.stdout
    assert {p: p.read_bytes() if p.exists() else None for p in guarded} == before


@pytest.mark.parametrize("name", ["r[1]"])
@needs_pasta
def test_other_worktrees_entries_are_guarded_whatever_the_repository_is_called(
        world, name: str) -> None:
    main = git_repo(world["rw"] / name)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(world["rw"] / "wt1")],
                   check=True)
    commondir = main / ".git" / "worktrees" / "wt1" / "commondir"
    before = commondir.read_bytes()
    proc = jailed(world["script"], f"echo x >>'{commondir}' 2>/dev/null && echo written=yes",
                  cwd=main)
    assert "written=yes" not in proc.stdout, proc.stdout
    assert commondir.read_bytes() == before


@needs_pasta
def test_git_commits_in_the_checkout_and_its_submodule_but_not_their_hooks_or_config(
        world, worktrees) -> None:
    main, module = worktrees["main"], worktrees["common"] / "modules" / "lib"
    subprocess.run(["git", "-C", str(main), "-c", "protocol.file.allow=always", "submodule",
                    "add", "-q", str(world["other"]), "lib"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), *GIT_ID, "commit", "-qm", "lib"], check=True)
    git = "git " + " ".join(GIT_ID)
    attempts = {
        "status": "git status --porcelain >/dev/null",
        "commit": f"echo a >a.txt && git add a.txt && {git} commit -qm a",
        "sub_commit": f"echo b >lib/b.txt && git -C lib add b.txt && {git} -C lib commit -qm b",
        "sub_hook": f": >{module}/hooks/post-commit",
        "sub_config": f"echo x >>{module}/config",
    }
    proc = jailed(world["script"], "\n".join(
        f"( {cmd} ) 2>/dev/null && echo {name}=yes || echo {name}=no"
        for name, cmd in attempts.items()), cwd=main)
    assert {"status=yes", "commit=yes", "sub_commit=yes", "sub_hook=no",
            "sub_config=no"} <= set(proc.stdout.split()), proc.stdout


@pytest.mark.parametrize("span, status", [
    ("24h", 3), ("1440min", 3), ("86400s", 3), ("25h", 64), ("1441min", 64), ("86401s", 64),
    ("999999999h", 64)])
def test_max_runtime_is_bounded(world, span: str, status: int) -> None:
    """Without a settings file an accepted span gets as far as reading it: 3."""
    script = make_tree(world["var"] / "tree2", None)
    proc = run(script, "--detach", "ok-slug", "--max-runtime", span, "--", "true",
               cwd=world["repo"])
    assert proc.returncode == status, proc.stderr


@pytest.mark.skipif(not CLAUDE_CREDS.is_file(), reason="no Claude credentials file here")
@pytest.mark.parametrize("where", ["denyRead", "credentials", "absent"])
@needs_pasta
def test_claude_login_says_so_when_the_profile_hides_the_credentials(world, where: str) -> None:
    sandbox = world["settings"]["sandbox"]
    if where == "denyRead":
        sandbox["filesystem"]["denyRead"].append("~/.claude")
    elif where == "credentials":
        sandbox["credentials"]["files"].append({"path": "~/.claude/.credentials.json", "mode": "mask"})
    script = make_tree(world["var"] / "tree2", world["settings"])
    if where == "absent":
        source = script.read_text()
        assert source.count(".claude/.credentials.json") == 1
        script.write_text(source.replace(".claude/.credentials.json", ".claude/.confined-absent"))
    marker = world["var"] / "should-not-exist"
    proc = run(script, "--claude-login", "--", "touch", str(marker), cwd=world["repo"])
    assert proc.returncode == 3 and "credentials" in proc.stderr, proc.stderr
    assert not marker.exists()


@needs_pasta
def test_bwrap_starts_without_the_callers_loader_variables(world) -> None:
    """The interpreter has loaded a caller's LD_PRELOAD before the script runs;
    bwrap, outside the jail too, must not load it again. Pid 1 is bwrap."""
    preload = "/nonexistent/confined-test-preload.so"
    proc = jailed(world["script"],
                  'echo "bwrap=$(tr "\\0" "\\n" </proc/1/environ | grep -c ^LD_)"\n'
                  'echo "command=$LD_PRELOAD"', cwd=world["repo"],
                  env=base_env(LD_PRELOAD=preload, LD_LIBRARY_PATH="/nonexistent"))
    assert answers(proc) == {"bwrap": "0", "command": preload}, proc.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@needs_pasta
def test_refuses_when_a_guard_cannot_be_placed(world) -> None:
    repo = world["repo"]
    (repo / ".claude").mkdir()
    (repo / ".claude").chmod(0o555)
    try:
        proc = run(world["script"], "--", "sh", "-c",
                   f"chmod u+w {repo}/.claude; echo x >{repo}/.claude/settings.json", cwd=repo)
    finally:
        (repo / ".claude").chmod(0o755)
    assert proc.returncode == 3, proc.stderr
    assert not (repo / ".claude" / "settings.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda s: s["sandbox"]["filesystem"]["allowWrite"].append("relative/dir"),
    lambda s: s["sandbox"]["filesystem"]["denyRead"].append("relative"),
    lambda s: s["sandbox"]["filesystem"]["denyWrite"].append("relative"),
    lambda s: s["sandbox"]["credentials"]["files"].append({"path": "rel/token", "mode": "mask"}),
    lambda s: s["permissions"]["deny"].append("Read(relative/**)"),
    lambda s: s["permissions"]["deny"].append("Read(**/secrets)"),
], ids=["allowWrite", "denyRead", "denyWrite", "credentials", "read_relative", "read_relative_glob"])
@needs_pasta
def test_an_entry_it_cannot_honour_fails_closed(world, mutate) -> None:
    mutate(world["settings"])
    script = make_tree(world["var"] / "tree2", world["settings"])
    marker = world["var"] / "should-not-exist"
    proc = run(script, "--", "touch", str(marker), cwd=world["repo"])
    assert proc.returncode == 3, proc.stderr
    assert not marker.exists()


@needs_pasta
def test_identity_is_the_real_user_and_git_sees_no_guards(world) -> None:
    world["plants"]["repo_env"].unlink()
    proc = jailed(world["script"],
                  'echo "uid=$(id -u) gid=$(id -g)"\n'
                  f'out=$(git -C {world["repo"]} status --porcelain 2>&1); echo "git=$?[$out]"',
                  cwd=world["repo"])
    assert f"uid={os.getuid()} gid={os.getgid()}" in proc.stdout
    assert "git=0[]" in proc.stdout, proc.stdout


@needs_pasta
def test_chdir_targets_another_repo(world, tmp_path: Path) -> None:
    proc = run(world["script"], "--chdir", str(world["repo"]), "--", "bash", "-c",
               "pwd; echo x >chdir-proof", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == str(world["repo"])
    assert (world["repo"] / "chdir-proof").read_text() == "x\n"


def host_ipv4_addresses() -> set[str]:
    lines = Path("/proc/net/fib_trie").read_text().splitlines()
    return {lines[i - 1].split()[-1] for i, line in enumerate(lines)
            if "/32 host LOCAL" in line} - {"127.0.0.1"}


def default_gateway() -> str | None:
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
    return None


NET_PROBE = """
import socket, sys
port, hosts = int(sys.argv[1]), sys.argv[2:]
for host in hosts:
    try:
        socket.create_connection((host, port), timeout=4).close()
        print(f"{host}=reachable")
    except ConnectionRefusedError:
        print(f"{host}=refused")
    except OSError as e:
        print(f"{host}=blocked:{e.errno}")
try:
    socket.create_connection(("github.com", 443), timeout=10).close()
    print("outbound=reachable")
except OSError as e:
    print(f"outbound=blocked:{e!r}")
"""


@needs_pasta
def test_host_and_private_networks_are_out_of_reach_but_the_internet_is_not(world) -> None:
    accepted: list[tuple] = []
    srv = socket.socket()
    srv.bind(("0.0.0.0", 0))
    srv.listen(16)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, peer = srv.accept()
            except OSError:
                continue
            accepted.append(peer)
            conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    gateway = default_gateway()
    hosts = sorted(host_ipv4_addresses()) + ["127.0.0.1"] + ([gateway] if gateway else [])
    try:
        proc = jailed(world["script"],
                      'iptables -F OUTPUT 2>/dev/null; ip route flush table main 2>/dev/null\n'
                      f"python3 -c '{NET_PROBE}' {port} {' '.join(hosts)}", cwd=world["repo"])
    finally:
        stop.set()
        thread.join()
        srv.close()
    out = answers(proc)
    assert accepted == [], f"the host listener accepted {accepted} from the jail: {out}"
    if gateway:
        assert out[gateway].startswith("blocked"), out
    assert out["outbound"] == "reachable", out


@needs_pasta
def test_host_unix_sockets_are_out_of_reach_but_the_nix_daemon(world) -> None:
    tag = uuid.uuid4().hex[:8]
    places = [Path(f"/run/user/{os.getuid()}"), world["home"], Path("/dev/shm")]
    paths = [place / f"confined-test-{tag}.sock" for place in places if place.is_dir()]
    listeners = []
    try:
        for path in paths:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            listener.listen(1)
            listeners.append(listener)
        probe = ("import socket, sys\nfor p in sys.argv[1:]:\n"
                 "    s = socket.socket(socket.AF_UNIX)\n"
                 "    try:\n        s.connect(p); print(p + '=connected')\n"
                 "    except OSError:\n        print(p + '=unreachable')\n")
        proc = jailed(world["script"],
                      f"python3 -c \"{probe}\" {' '.join(map(str, paths))} "
                      "/nix/var/nix/daemon-socket/socket", cwd=world["repo"])
    finally:
        for listener in listeners:
            listener.close()
        for path in paths:
            path.unlink(missing_ok=True)
    out = answers(proc)
    assert {out[str(p)] for p in paths} == {"unreachable"}, out
    if Path("/nix/var/nix/daemon-socket/socket").exists():
        assert out["/nix/var/nix/daemon-socket/socket"] == "connected", out


@needs_pasta
def test_abstract_sockets_are_out_of_reach(world) -> None:
    proc = jailed(world["script"], 'echo "abstract=$(grep -c " @" /proc/net/unix)"', cwd=world["repo"])
    assert answers(proc)["abstract"] == "0"


@needs_pasta
def test_nested_bwrap_works(world) -> None:
    proc = run(world["script"], "--", BWRAP, "--ro-bind", "/", "/", "true", cwd=world["repo"])
    assert proc.returncode == 0, proc.stderr


@needs_pasta
def test_confined_runs_inside_confined(world) -> None:
    proc = run(world["script"], "--", str(world["script"]), "--", "sh", "-c",
               "echo inner=$SANDBOX_RUNTIME", cwd=world["repo"])
    assert (proc.returncode, proc.stdout) == (0, "inner=1\n"), proc.stderr


@pytest.mark.parametrize("status", [0, 7])
@needs_pasta
def test_status_stdout_and_sandbox_runtime_pass_through(world, status: int) -> None:
    proc = run(world["script"], "--", "sh", "-c",
               f'echo "out SANDBOX_RUNTIME=$SANDBOX_RUNTIME"; exit {status}', cwd=world["repo"])
    assert (proc.returncode, proc.stdout) == (status, "out SANDBOX_RUNTIME=1\n")


@needs_pasta
def test_path_does_not_choose_the_jail_tools(world) -> None:
    fake = world["var"] / "fakebin"
    fake.mkdir()
    hit = world["var"] / "fake-ran"
    for name in ("bwrap", "pasta", "iptables-restore"):
        (fake / name).write_text(f"#!/bin/sh\ntouch {hit}\nexit 0\n")
        (fake / name).chmod(0o755)
    env = base_env(PATH=f"{fake}:{os.environ['PATH']}")
    proc = run(world["script"], "--", "sh", "-c", "echo $SANDBOX_RUNTIME", cwd=world["repo"], env=env)
    assert proc.stdout == "1\n" and not hit.exists()


@pytest.mark.parametrize("missing", ["pasta", "bwrap", "iptables-restore"])
def test_refuses_without_a_jail_tool(world, missing: str) -> None:
    only = world["var"] / "only"
    only.mkdir()
    for name in {"pasta", "bwrap", "iptables-restore", "bash"} - {missing}:
        found = shutil.which(name)
        if found:
            (only / name).symlink_to(found)
    script = make_tree(world["var"] / "tree2", world["settings"], search=(str(only),))
    marker = world["var"] / "should-not-exist"
    proc = run(script, "--", "touch", str(marker), cwd=world["repo"])
    assert proc.returncode == 3, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("args", [
    ("--foo", "--", "touch", "{marker}"),
    ("touch", "{marker}"),
    ("--",),
    ("--detach", "Bad_Slug", "--", "touch", "{marker}"),
    ("--detach", "-x", "--", "touch", "{marker}"),
    ("--detach", "claude-pull", "--", "touch", "{marker}"),
    ("--detach", "a" * 42, "--", "touch", "{marker}"),
    ("--detach", "ok-slug", "touch", "{marker}"),
    ("--detach", "ok-slug", "--"),
    ("--detach",),
    ("--chdir",),
    ("--chdir", "--", "touch", "{marker}"),
    ("--max-runtime", "1h", "--", "touch", "{marker}"),
    ("--detach", "ok-slug", "--max-runtime", "forever", "--", "touch", "{marker}"),
    ("--detach", "ok-slug", "--max-runtime", "0", "--", "touch", "{marker}"),
    (),
])
def test_refuses_bad_usage(world, args: tuple[str, ...]) -> None:
    marker = world["var"] / "should-not-exist"
    proc = run(world["script"], *(a.format(marker=marker) for a in args), cwd=world["repo"])
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("state", ["missing", "unreadable", "malformed", "no-sandbox-block"])
def test_refuses_without_readable_settings(world, state: str) -> None:
    script = make_tree(world["var"] / "tree2", None)
    settings = world["var"] / "tree2" / "settings.json"
    if state == "unreadable":
        settings.write_text("{}")
        settings.chmod(0)
    elif state == "malformed":
        settings.write_text("{not json")
    elif state == "no-sandbox-block":
        # arftl-allow: permission-widened
        settings.write_text(json.dumps({"permissions": {"deny": []}}))
    marker = world["var"] / "should-not-exist"
    proc = run(script, "--", "touch", str(marker), cwd=world["repo"])
    assert proc.returncode == 3, proc.stderr
    assert not marker.exists()


def user_bus() -> bool:
    try:
        return subprocess.run(["systemctl", "--user", "show", "-p", "Version"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def unit_props(unit: str) -> dict[str, str]:
    out = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "SubState",
                          "-p", "Result", unit], capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def settle(unit: str, timeout: float = 60) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        props = unit_props(unit)
        if props.get("ActiveState") in ("failed", "inactive") or props.get("SubState") == "exited":
            return props
        time.sleep(0.5)
    return unit_props(unit)


@pytest.fixture
def detached():
    slugs: list[str] = []

    def new() -> tuple[str, Path]:
        slug = f"confined-test-{uuid.uuid4().hex[:8]}"
        slugs.append(slug)
        return slug, SHARED_TMP / f"{slug}.log"

    yield new
    for slug in slugs:
        subprocess.run(["systemctl", "--user", "stop", f"{slug}.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", f"{slug}.service"], capture_output=True)
        (SHARED_TMP / f"{slug}.log").unlink(missing_ok=True)
        (REAL_HOME / ".local/state/claude-unit-failures" / f"{slug}.json").unlink(missing_ok=True)


needs_bus = pytest.mark.skipif(not user_bus(), reason="no systemd user bus in this environment")


@needs_bus
@needs_pasta
def test_detach_runs_jailed_and_outlives_the_launcher(world, detached) -> None:
    slug, log = detached()
    marker = world["repo"] / "detached"
    body = (f'sleep 3; (: >{world["other"]}/escape) 2>/dev/null && e=yes || e=no; '
            f'printf "runtime=%s escape=%s cred=%s pwd=%s\\n" "$SANDBOX_RUNTIME" "$e" '
            f'"$(cat {world["plants"]["cred"]} 2>/dev/null)" "$PWD" >{marker}')
    proc = run(world["script"], "--detach", slug, "--", "bash", "-c", body, cwd=world["repo"])
    assert proc.returncode == 0, proc.stderr
    assert f"{slug}.service" in proc.stdout and str(log) in proc.stdout
    assert not marker.exists(), "the launcher must return before the job ends"
    assert settle(f"{slug}.service")["Result"] == "success"
    assert marker.read_text() == f"runtime=1 escape=no cred= pwd={world['repo']}\n"
    assert log.read_text().splitlines()[-1] == "MARKER_OK"
    assert subprocess.run(["pgrep", "-f", slug], capture_output=True, text=True).stdout == ""
    waited = subprocess.run([str(SCRIPTS / "wait-for"), "unit", f"{slug}.service", "--user",
                             "--timeout", "30"], capture_output=True, text=True, timeout=60)
    assert waited.returncode == 0 and "result=success" in waited.stdout, waited.stdout


@needs_bus
@needs_pasta
def test_detach_failure_is_the_unit_result(world, detached) -> None:
    slug, log = detached()
    proc = run(world["script"], "--detach", slug, "--", "sh", "-c", "exit 7", cwd=world["repo"])
    assert proc.returncode == 0, proc.stderr
    assert settle(f"{slug}.service")["Result"] == "exit-code"
    assert log.read_text().splitlines()[-1] == "MARKER_FAILED rc=7"
    waited = subprocess.run([str(SCRIPTS / "wait-for"), "unit", f"{slug}.service", "--user",
                             "--timeout", "30"], capture_output=True, text=True, timeout=60)
    assert waited.returncode == 1, waited.stdout


@needs_bus
@needs_pasta
def test_detach_is_bounded_in_time(world, detached) -> None:
    slug, _ = detached()
    proc = run(world["script"], "--chdir", str(world["repo"]), "--detach", slug,
               "--max-runtime", "3s", "--", "sleep", "300", cwd=world["var"])
    assert proc.returncode == 0, proc.stderr
    assert settle(f"{slug}.service", timeout=30)["Result"] == "timeout"
    assert subprocess.run(["pgrep", "-f", slug], capture_output=True, text=True).stdout == ""


@needs_bus
@needs_pasta
def test_detach_refuses_a_used_slug_and_says_so(world, detached) -> None:
    slug, log = detached()
    assert run(world["script"], "--detach", slug, "--", "true", cwd=world["repo"]).returncode == 0
    settle(f"{slug}.service")
    again = run(world["script"], "--detach", slug, "--", "touch", str(world["repo"] / "second"),
                cwd=world["repo"])
    assert again.returncode == 64, again.stderr
    assert "already" in again.stderr and "new slug" in again.stderr, again.stderr
    assert log.read_text().splitlines()[-1] == "MARKER_OK"
    time.sleep(1)
    assert not (world["repo"] / "second").exists()


@needs_pasta
def test_sandbox_canary_probe_still_runs_ordinary_work(tmp_path: Path) -> None:
    """The agent-e2e canary's own setup.sh and probe.sh against the shipped
    sandbox block: every plant reads hidden, ordinary work still runs."""
    canary_dir = REPO / "tests" / "agent-e2e" / "sandbox-canary"
    home, work, rundir = tmp_path / "home", tmp_path / "work", tmp_path / "run"
    for d in (home / ".claude", work, rundir):
        d.mkdir(parents=True)
    shutil.copy2(REPO / "runtime" / "settings.json", home / ".claude" / "settings.json")
    script = make_tree(home / ".claude", None)
    setup = subprocess.run(["bash", str(canary_dir / "setup.sh"), str(work), str(rundir)],
                           env=base_env(HOME=str(home)), capture_output=True, text=True, timeout=60)
    assert setup.returncode == 0, setup.stderr
    runtime_plant = Path(f"/run/user/{os.getuid()}/e2e-sandbox-canary") / \
        hashlib.sha256(str(rundir).encode()).hexdigest()[:16]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    try:
        probe = run(script, "--", "bash", "probe.sh", cwd=work,
                    env=base_env(HOME=str(home), KITTY_LISTEN_ON=settings["env"]["KITTY_LISTEN_ON"]),
                    timeout=300)
    finally:
        if runtime_plant.is_dir():
            shutil.rmtree(runtime_plant)
    out = answers(probe)
    assert out == {
        "ran": "yes", "ssh-key": "hidden", "codex-auth": "hidden", "gh-token": "hidden",
        "linked-deny": "hidden", "runtime-dir": "hidden", "denied-env": "hidden",
        "cwd-write": "ok", "git-commit": "ok", "nix-eval": "ok", "nix-daemon": "ok",
        "github": "ok",
    }, probe.stdout + probe.stderr

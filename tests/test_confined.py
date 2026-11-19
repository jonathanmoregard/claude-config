"""`confined` runs outside the Claude Code sandbox without a prompt, so its own
jail is the only thing between agent-written code and the host. Real bwrap,
no mocks. Every jailed script ends by printing RAN: a jail that never started
prints nothing, and "the secret is not in the output" would pass on that.

The script reads the settings.json beside its own config tree, so each test
copies it into a scratch tree with a scratch settings.json."""
from __future__ import annotations

import functools
import hashlib
import json
import socket
import os
import pwd
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtime" / "scripts"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
BWRAP = shutil.which("bwrap")
RAN = "confined-ran-marker"
CANARY = "confined-canary-not-a-secret"

pytestmark = pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
SEARCH_LINE = re.compile(r"^SEARCH = \(", re.M)


@functools.cache
def passt_bin() -> str | None:
    try:
        out = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", "nixpkgs#passt"],
                             capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.stdout.split()
    if out.returncode != 0 or not lines or not os.access(f"{lines[-1]}/bin/pasta", os.X_OK):
        return None
    return f"{lines[-1]}/bin"


needs_pasta = pytest.mark.skipif(
    passt_bin() is None,
    reason="`nix build nixpkgs#passt` is unavailable: confined refuses to run without pasta")


def make_tree(config: Path, settings: dict | None, search: tuple[str, ...] | None = None) -> Path:
    """The one substitution: pasta is not installed on the host yet, so the
    copy searches the passt store path first (or `search` replaces the tuple)."""
    (config / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "record-user-unit-failure", config / "scripts")
    source = (SCRIPTS / "confined").read_text()
    assert len(SEARCH_LINE.findall(source)) == 1
    if search is not None:
        source = re.sub(r"^SEARCH = \(.*\)$", f"SEARCH = {search!r}", source, flags=re.M)
    elif passt_bin():
        source = SEARCH_LINE.sub(f'SEARCH = ("{passt_bin()}", ', source)
    (config / "scripts" / "confined").write_text(source)
    (config / "scripts" / "confined").chmod(0o755)
    if settings is not None:
        (config / "settings.json").write_text(json.dumps(settings))
    return config / "scripts" / "confined"


def base_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "SANDBOX_RUNTIME"}
    env.update(extra)
    return env


def run(script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script), *args], cwd=cwd, env=env or base_env(),
                          capture_output=True, text=True, timeout=timeout, check=False)


def jailed(script: Path, body: str, *, cwd: Path, env: dict[str, str] | None = None
           ) -> subprocess.CompletedProcess[str]:
    proc = run(script, "--", "bash", "-c", f"{body}\necho {RAN}", cwd=cwd, env=env)
    assert proc.stdout.rstrip().endswith(RAN), (proc.returncode, proc.stdout, proc.stderr)
    return proc


@pytest.fixture
def home_scratch():
    """A directory in the real home, outside /tmp, so a write there proves the
    allowWrite bind and not the /tmp one."""
    root = REAL_HOME / "worktrees" / f".confined-test-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root)


@pytest.fixture
def world(tmp_path: Path, home_scratch: Path):
    rw, other = home_scratch / "rw", home_scratch / "other"
    for d in (rw / "locked", other, tmp_path / "denied", tmp_path / "vault.d" / "1",
              tmp_path / "cred", home_scratch / "sealed"):
        d.mkdir(parents=True)
    plants = {
        "denied_dir": tmp_path / "denied" / "secret",
        "denied_file": tmp_path / "denied-file",
        "glob_file": tmp_path / "sock-1",
        "cred": tmp_path / "cred" / "token",
        "vault": tmp_path / "vault.d" / "1" / "secret",
        "home_rule": home_scratch / "sealed" / "secret",
        "plain": tmp_path / "plain",
    }
    for path in plants.values():
        path.write_text(CANARY + "\n")
    (tmp_path / "vault").symlink_to(tmp_path / "vault.d")
    home_rel = home_scratch.relative_to(REAL_HOME)
    settings = {
        "sandbox": {
            "filesystem": {
                "allowWrite": [str(rw), "~/.confined-test-missing-root"],
                "denyWrite": [str(rw / "locked")],
                "denyRead": [str(tmp_path / "denied"), str(tmp_path / "denied-file"),
                             str(tmp_path / "sock-*"), "/nonexistent/confined-test"],
            },
            "credentials": {
                "files": [{"path": str(plants["cred"]), "mode": "mask"}],
                "envVars": [{"name": "CONFINED_TEST_DENIED", "mode": "deny"}],
            },
        },
        "permissions": {"deny": [
            f"Read(/{tmp_path}/vault.d/**)",
            f"Read(~/{home_rel}/sealed/**)",
            f"Read(/{tmp_path}/pla*)",
            "Read(~/.confined-test-missing-file)",
            "Read(~/.claude/.credentials.json)",
            "Bash(rm -rf *)",
        ]},
    }
    script = make_tree(tmp_path / "tree", settings)
    return {"script": script, "tmp": tmp_path, "rw": rw, "other": other,
            "home": home_scratch, "plants": plants}


@needs_pasta
def test_hides_denied_paths_credentials_and_read_rules(world) -> None:
    p = world["plants"]
    proc = jailed(world["script"], "\n".join(
        f'printf "{name}=%s\\n" "$(cat {path} 2>/dev/null)"' for name, path in p.items()
    ) + f'\nprintf "vault_ls=%s\\n" "$(ls -A {world["tmp"]}/vault.d)"'
        f'\nprintf "link=%s\\n" "$(cat {world["tmp"]}/vault/1/secret 2>/dev/null)"',
        cwd=world["rw"])
    lines = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert lines.pop("plain") == CANARY, "control: an ordinary file must stay readable"
    assert lines == {"denied_dir": "", "denied_file": "", "glob_file": "", "cred": "",
                     "vault": "", "home_rule": "", "vault_ls": "", "link": ""}


@needs_pasta
def test_hides_denied_env_and_real_home_secrets(world) -> None:
    ssh, codex = REAL_HOME / ".ssh", REAL_HOME / ".codex" / "auth.json"
    proc = jailed(world["script"],
                  'printf "denied=%s kept=%s\\n" "${CONFINED_TEST_DENIED:-}" "${CONFINED_TEST_KEPT:-}"\n'
                  f'printf "ssh=%s\\n" "$(ls -A {ssh} 2>/dev/null | wc -l)"\n'
                  f'printf "codex=%s\\n" "$(wc -c <{codex} 2>/dev/null)"',
                  cwd=world["rw"],
                  env=base_env(CONFINED_TEST_DENIED=CANARY, CONFINED_TEST_KEPT="kept"))
    assert "denied= kept=kept" in proc.stdout
    if ssh.is_dir():
        assert "ssh=0" in proc.stdout
    if codex.is_file():
        assert "codex=0" in proc.stdout


CLAUDE_CREDS = REAL_HOME / ".claude" / ".credentials.json"
# Sizes and access(2) only: neither test reads the credential.
CREDS_PROBE = (f'printf "size=%s\\n" "$(wc -c <{CLAUDE_CREDS} 2>/dev/null)"\n'
               f'[ -w {CLAUDE_CREDS} ] && echo writable=yes || echo writable=no')


@pytest.mark.skipif(not CLAUDE_CREDS.is_file(), reason="no Claude credentials file here")
@needs_pasta
def test_claude_credentials_stay_usable_despite_a_read_rule(world) -> None:
    proc = jailed(world["script"], CREDS_PROBE, cwd=world["rw"])
    assert "writable=yes" in proc.stdout
    assert re.search(r"^size=[1-9]", proc.stdout, re.M), proc.stdout


@pytest.mark.skipif(not CLAUDE_CREDS.is_file(), reason="no Claude credentials file here")
@pytest.mark.parametrize("where", ["denyRead", "credentials"])
@needs_pasta
def test_claude_credentials_hidden_when_the_sandbox_block_hides_them(tmp_path: Path, where: str) -> None:
    sandbox: dict = {"filesystem": {"allowWrite": [], "denyRead": []}, "credentials": {"files": []}}
    if where == "denyRead":
        sandbox["filesystem"]["denyRead"].append("~/.claude")
    else:
        sandbox["credentials"]["files"].append({"path": "~/.claude/.credentials.json", "mode": "mask"})
    script = make_tree(tmp_path / "tree", {"sandbox": sandbox})
    proc = jailed(script, CREDS_PROBE, cwd=tmp_path)
    assert re.search(r"^size=0?$", proc.stdout, re.M), proc.stdout


@needs_pasta
def test_has_its_own_pid_and_ipc_namespaces(world) -> None:
    ns = {k: os.readlink(f"/proc/self/ns/{k}") for k in ("pid", "ipc")}
    proc = jailed(world["script"],
                  f'[ -e /proc/{os.getpid()} ] && echo sees-host-pid\n'
                  'echo "pid=$(readlink /proc/self/ns/pid) ipc=$(readlink /proc/self/ns/ipc)"',
                  cwd=world["rw"])
    assert "sees-host-pid" not in proc.stdout
    assert ns["pid"] not in proc.stdout and ns["ipc"] not in proc.stdout


@needs_pasta
def test_writes_only_under_allow_write_and_tmp(world) -> None:
    rw, tag = world["rw"], uuid.uuid4().hex[:8]
    targets = {
        "allow": rw / "new",
        "tmp": world["tmp"] / f"new-{tag}",
        "deny_write": rw / "locked" / "new",
        "home_elsewhere": world["other"] / "new",
        "home_top": REAL_HOME / f".confined-test-{tag}",
    }
    try:
        proc = jailed(world["script"], "\n".join(
            f'(: >{path}) 2>/dev/null && echo {name}=yes || echo {name}=no'
            for name, path in targets.items()), cwd=rw)
    finally:
        targets["home_top"].unlink(missing_ok=True)
    assert {"allow=yes", "tmp=yes", "deny_write=no", "home_elsewhere=no",
            "home_top=no"} <= set(proc.stdout.split())


GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


@pytest.fixture
def repo(world):
    main, wt = world["rw"] / "main", world["rw"] / "wt"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    (main / "sub").mkdir()
    (main / "sub" / "keep").write_text("")
    (main / "tracked.txt").write_text("v1\n")
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.local.json").write_text("{}\n")
    hook = main / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), *GIT_ID, "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "settings.local.json").write_text("{}\n")
    return {"main": main, "wt": wt, "common": main / ".git"}


@pytest.mark.parametrize("where", ["main", "main/sub", "wt", "wt/sub"])
@needs_pasta
def test_protects_agent_and_git_config_in_the_cwd_repo(world, repo, where: str) -> None:
    checkout = repo[where.split("/")[0]]
    common = repo["common"]
    attempts = {
        "settings": f"echo x >>{checkout}/.claude/settings.local.json",
        "settings_mv": f"echo {{}} >{checkout}/.claude/new && mv {checkout}/.claude/new "
                       f"{checkout}/.claude/settings.local.json",
        "hook": f"echo x >>{common}/hooks/pre-commit",
        "new_hook": f": >{common}/hooks/post-checkout",
        "config": f"echo x >>{common}/config",
        "tracked": f"echo v2 >>{checkout}/tracked.txt",
    }
    proc = jailed(world["script"], "\n".join(
        f'( {cmd} ) 2>/dev/null && echo {name}=yes || echo {name}=no'
        for name, cmd in attempts.items()
    ) + f'\n[ -e {checkout}/.mcp.json ] && echo mcp=present || echo mcp=absent',
        cwd=world["rw"] / where)
    assert {"settings=no", "settings_mv=no", "hook=no", "new_hook=no", "config=no",
            "tracked=yes", "mcp=absent"} <= set(proc.stdout.split()), proc.stdout


@pytest.mark.parametrize("where", ["main", "wt"])
@needs_pasta
def test_protected_directories_cannot_be_renamed_away(world, repo, where: str) -> None:
    """Moving a protected entry's parent aside and recreating it would undo
    the protection; so would repointing a worktree at another git dir."""
    checkout, common = repo[where], repo["common"]
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


@needs_pasta
def test_identity_is_the_real_user_and_git_works(world, repo) -> None:
    proc = jailed(world["script"],
                  'echo "uid=$(id -u) gid=$(id -g)"\n'
                  f'out=$(git -C {repo["main"]} status --porcelain 2>&1); echo "git=$?[$out]"',
                  cwd=repo["main"])
    assert f"uid={os.getuid()} gid={os.getgid()}" in proc.stdout
    assert "git=0[]" in proc.stdout, proc.stdout


NET_PROBE = """
import socket, struct, sys
port = int(sys.argv[1])
def reach(host, p):
    try:
        socket.create_connection((host, p), timeout=5).close()
        return "reachable"
    except OSError:
        return "unreachable"
gw = None
for line in open("/proc/net/route").readlines()[1:]:
    f = line.split()
    if f[1] == "00000000":
        gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
print("abstract=%d" % sum(" @" in l for l in open("/proc/net/unix")))
print("loopback=" + reach("127.0.0.1", port))
print("gateway=" + (reach(gw, port) if gw else "none"))
print("outbound=" + reach("github.com", 443))
"""


@pytest.fixture
def net_probe(world):
    if not any(" @" in line for line in open("/proc/net/unix")):
        pytest.skip("the host has no abstract sockets to hide")
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=3).close()
        proc = jailed(world["script"], f"python3 -c '{NET_PROBE}' {port}", cwd=world["rw"])
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


@needs_pasta
def test_host_loopback_and_abstract_sockets_are_out_of_reach(net_probe) -> None:
    assert (net_probe["abstract"], net_probe["loopback"], net_probe["gateway"]) == \
        ("0", "unreachable", "unreachable"), net_probe


@needs_pasta
def test_outbound_network_still_works(net_probe) -> None:
    assert net_probe["outbound"] == "reachable", net_probe


@needs_pasta
def test_nested_bwrap_works(world) -> None:
    proc = run(world["script"], "--", BWRAP, "--ro-bind", "/", "/", "true", cwd=world["rw"])
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("status", [0, 7])
@needs_pasta
def test_status_stdout_and_sandbox_runtime_pass_through(world, status: int) -> None:
    proc = run(world["script"], "--", "sh", "-c",
               f'echo "out SANDBOX_RUNTIME=$SANDBOX_RUNTIME"; exit {status}', cwd=world["rw"])
    assert (proc.returncode, proc.stdout) == (status, "out SANDBOX_RUNTIME=1\n")


@needs_pasta
def test_path_does_not_choose_bwrap_or_pasta(world, tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    hit = tmp_path / "fake-ran"
    for name in ("bwrap", "pasta"):
        (fake / name).write_text(f"#!/bin/sh\ntouch {hit}\nexit 0\n")
        (fake / name).chmod(0o755)
    env = base_env(PATH=f"{fake}:{os.environ['PATH']}")
    proc = run(world["script"], "--", "sh", "-c", "echo $SANDBOX_RUNTIME", cwd=world["rw"], env=env)
    assert proc.stdout == "1\n" and not hit.exists()


@pytest.mark.parametrize("missing", ["pasta", "bwrap"])
def test_refuses_without_pasta_or_bwrap(tmp_path: Path, missing: str) -> None:
    only = tmp_path / "only"
    only.mkdir()
    present = {"pasta": BWRAP, "bwrap": f"{passt_bin()}/pasta" if passt_bin() else None}[missing]
    if present:
        (only / Path(present).name).symlink_to(present)
    script = make_tree(tmp_path / "tree", {"sandbox": {}}, search=(str(only),))
    marker = tmp_path / "should-not-exist"
    proc = run(script, "--", "touch", str(marker), cwd=tmp_path)
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
    (),
])
def test_refuses_bad_usage(world, args: tuple[str, ...]) -> None:
    marker = world["tmp"] / "should-not-exist"
    proc = run(world["script"], *(a.format(marker=marker) for a in args), cwd=world["rw"])
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("where", ["other", "locked", "home"])
def test_refuses_cwd_outside_writable_roots(world, where: str) -> None:
    cwd = {"other": world["other"], "locked": world["rw"] / "locked", "home": REAL_HOME}[where]
    marker = world["tmp"] / "should-not-exist"
    proc = run(world["script"], "--", "touch", str(marker), cwd=cwd)
    assert proc.returncode == 64, proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("state", ["missing", "unreadable", "malformed", "no-sandbox-block"])
def test_refuses_without_readable_settings(tmp_path: Path, state: str) -> None:
    script = make_tree(tmp_path / "tree", None)
    settings = tmp_path / "tree" / "settings.json"
    if state == "unreadable":
        settings.write_text("{}")
        settings.chmod(0)
    elif state == "malformed":
        settings.write_text("{not json")
    elif state == "no-sandbox-block":
        settings.write_text(json.dumps({"permissions": {"deny": []}}))
    marker = tmp_path / "should-not-exist"
    proc = run(script, "--", "touch", str(marker), cwd=tmp_path)
    assert proc.returncode == 3, proc.stderr
    assert not marker.exists()


def user_bus() -> bool:
    try:
        return subprocess.run(["systemctl", "--user", "show", "-p", "Version"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def unit_state(unit: str) -> str:
    return subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "SubState",
                           "--value", unit], capture_output=True, text=True).stdout.split()


@pytest.mark.skipif(not user_bus(), reason="no systemd user bus in this environment")
@needs_pasta
def test_detach_runs_jailed_and_outlives_the_launcher(world) -> None:
    slug = f"confined-test-{uuid.uuid4().hex[:8]}"
    unit, log = f"{slug}.service", Path(f"/tmp/{slug}.log")
    marker = world["tmp"] / "detached"
    body = (f'sleep 3; (: >{world["other"]}/escape) 2>/dev/null && e=yes || e=no; '
            f'printf "runtime=%s escape=%s cred=%s abstract=%s\\n" "$SANDBOX_RUNTIME" "$e" '
            f'"$(cat {world["plants"]["cred"]} 2>/dev/null)" "$(grep -c " @" /proc/net/unix)" >{marker}')
    try:
        proc = run(world["script"], "--detach", slug, "--", "bash", "-c", body, cwd=world["rw"])
        assert proc.returncode == 0, proc.stderr
        assert unit in proc.stdout and str(log) in proc.stdout
        assert not marker.exists(), "the launcher must return before the job ends"
        deadline = time.monotonic() + 60
        while unit_state(unit) != ["active", "exited"] and time.monotonic() < deadline:
            time.sleep(0.5)
        assert marker.read_text() == "runtime=1 escape=no cred= abstract=0\n"
        assert log.read_text().splitlines()[-1] == "MARKER_OK"
        lingering = subprocess.run(["pgrep", "-f", slug], capture_output=True, text=True).stdout
        assert lingering == "", "pasta or the jail outlived the job"
        waited = subprocess.run([str(SCRIPTS / "wait-for"), "unit", unit, "--user", "--timeout", "30"],
                                capture_output=True, text=True, timeout=60)
        assert waited.returncode == 0 and "result=success" in waited.stdout, waited.stdout
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        log.unlink(missing_ok=True)


@needs_pasta
def test_sandbox_canary_probe_parity(tmp_path: Path) -> None:
    """The agent-e2e canary's own setup.sh and probe.sh, graded against its
    check.sh's sandbox-on expectations, with two deliberate differences:
    the gh token reads `hidden`, not `masked` (no proxy here swaps a masked
    value back in), and the home plants are declared in the scratch settings
    because confined takes ~ from passwd while setup.sh plants under $HOME."""
    canary_dir = REPO / "tests" / "agent-e2e" / "sandbox-canary"
    home, work, rundir = tmp_path / "home", tmp_path / "work", tmp_path / "run"
    for d in (home / ".claude", work, rundir):
        d.mkdir(parents=True)
    shutil.copy2(REPO / "runtime" / "settings.json", home / ".claude" / "settings.json")
    script = make_tree(home / ".claude", None)
    env = base_env(HOME=str(home))
    setup = subprocess.run(["bash", str(canary_dir / "setup.sh"), str(work), str(rundir)],
                           env=env, capture_output=True, text=True, timeout=60)
    assert setup.returncode == 0, setup.stderr
    runtime_plant = Path(f"/run/user/{os.getuid()}/e2e-sandbox-canary") / \
        hashlib.sha256(str(rundir).encode()).hexdigest()[:16]
    try:
        settings_path = home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["sandbox"]["filesystem"]["denyRead"] += [str(home / ".ssh"),
                                                          str(home / ".codex" / "auth.json")]
        settings["sandbox"]["credentials"]["files"].append(
            {"path": str(home / ".cache/gh-tokens/host/token"), "mode": "mask"})
        settings_path.write_text(json.dumps(settings))
        probe = run(script, "--", "bash", "probe.sh", cwd=work,
                    env=base_env(HOME=str(home), KITTY_LISTEN_ON=settings["env"]["KITTY_LISTEN_ON"]),
                    timeout=300)
    finally:
        if runtime_plant.is_dir():
            shutil.rmtree(runtime_plant)
    out = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    assert out == {
        "ran": "yes", "ssh-key": "hidden", "codex-auth": "hidden", "gh-token": "hidden",
        "linked-deny": "hidden", "runtime-dir": "hidden", "denied-env": "hidden",
        "cwd-write": "ok", "git-commit": "ok", "nix-eval": "ok", "nix-daemon": "ok",
        "github": "ok",
    }, probe.stdout + probe.stderr

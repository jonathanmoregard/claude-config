"""`confined` runs outside the Claude Code sandbox without a prompt, so its own
jail is the only thing between agent-written code and the host. Real bwrap,
no mocks. Every jailed script ends by printing RAN: a jail that never started
prints nothing, and "the secret is not in the output" would pass on that.

The script reads the settings.json beside its own config tree, so each test
copies it into a scratch tree with a scratch settings.json."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
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


def make_tree(root: Path, settings: dict | None) -> Path:
    (root / "scripts").mkdir(parents=True)
    for name in ("confined", "record-user-unit-failure"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    if settings is not None:
        (root / "settings.json").write_text(json.dumps(settings))
    return root / "scripts" / "confined"


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
            "Bash(rm -rf *)",
        ]},
    }
    script = make_tree(tmp_path / "tree", settings)
    return {"script": script, "tmp": tmp_path, "rw": rw, "other": other,
            "home": home_scratch, "plants": plants}


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


def test_has_its_own_pid_and_ipc_namespaces(world) -> None:
    ns = {k: os.readlink(f"/proc/self/ns/{k}") for k in ("pid", "ipc")}
    proc = jailed(world["script"],
                  f'[ -e /proc/{os.getpid()} ] && echo sees-host-pid\n'
                  'echo "pid=$(readlink /proc/self/ns/pid) ipc=$(readlink /proc/self/ns/ipc)"',
                  cwd=world["rw"])
    assert "sees-host-pid" not in proc.stdout
    assert ns["pid"] not in proc.stdout and ns["ipc"] not in proc.stdout


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


def test_nested_bwrap_works(world) -> None:
    proc = run(world["script"], "--", BWRAP, "--ro-bind", "/", "/", "true", cwd=world["rw"])
    assert proc.returncode == 0, proc.stderr


def test_status_stdout_and_sandbox_runtime_pass_through(world) -> None:
    proc = run(world["script"], "--", "sh", "-c", 'echo "out SANDBOX_RUNTIME=$SANDBOX_RUNTIME"; exit 7',
               cwd=world["rw"])
    assert (proc.returncode, proc.stdout) == (7, "out SANDBOX_RUNTIME=1\n")


def test_path_does_not_choose_bwrap(world, tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    hit = tmp_path / "fake-bwrap-ran"
    (fake / "bwrap").write_text(f"#!/bin/sh\ntouch {hit}\nexec \"$@\"\n")
    (fake / "bwrap").chmod(0o755)
    env = base_env(PATH=f"{fake}:{os.environ['PATH']}")
    proc = run(world["script"], "--", "sh", "-c", "echo $SANDBOX_RUNTIME", cwd=world["rw"], env=env)
    assert proc.stdout == "1\n" and not hit.exists()


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
def test_detach_runs_jailed_and_outlives_the_launcher(world) -> None:
    slug = f"confined-test-{uuid.uuid4().hex[:8]}"
    unit, log = f"{slug}.service", Path(f"/tmp/{slug}.log")
    marker = world["tmp"] / "detached"
    body = (f'sleep 3; (: >{world["other"]}/escape) 2>/dev/null && e=yes || e=no; '
            f'printf "runtime=%s escape=%s cred=%s\\n" "$SANDBOX_RUNTIME" "$e" '
            f'"$(cat {world["plants"]["cred"]} 2>/dev/null)" >{marker}')
    try:
        proc = run(world["script"], "--detach", slug, "--", "bash", "-c", body, cwd=world["rw"])
        assert proc.returncode == 0, proc.stderr
        assert unit in proc.stdout and str(log) in proc.stdout
        assert not marker.exists(), "the launcher must return before the job ends"
        deadline = time.monotonic() + 60
        while unit_state(unit) != ["active", "exited"] and time.monotonic() < deadline:
            time.sleep(0.5)
        assert marker.read_text() == "runtime=1 escape=no cred=\n"
        assert log.read_text().splitlines()[-1] == "MARKER_OK"
        waited = subprocess.run([str(SCRIPTS / "wait-for"), "unit", unit, "--user", "--timeout", "30"],
                                capture_output=True, text=True, timeout=60)
        assert waited.returncode == 0 and "result=success" in waited.stdout, waited.stdout
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        log.unlink(missing_ok=True)


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

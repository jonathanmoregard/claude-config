#!/usr/bin/env python3
"""Structured agent-config e2e broker over stdio MCP.

A sandboxed agent session cannot run the real headless agent that
scripts/agent-e2e/agent_e2e.py drives (Codex: no network, getaddrinfo
EAI_AGAIN). This server runs outside the sandbox and exposes one ``run`` tool:
an old/new campaign of one scenario, nothing else.

- The harness is origin/master's, checked out fresh per campaign from the
  broker's own mirror of the canonical remote; neither a candidate ref nor any
  local checkout replaces it, and git never runs in a local checkout.
- Both arms are refs on origin (``origin/<branch>`` or a commit reachable from
  one), resolved to full SHAs before the run: only pushed, reviewable config is
  ever staged.
- The scenario comes from the new ref's tests/agent-e2e tree, by name.
- Model, sample count, parallelism, per-run budget, per-run timeout and the
  whole call's worst-case wall time are bounded by config.json.
- At most maxCampaigns campaigns at once, each holding a slot lock until its
  unit stops. Git work on the shared mirror is serialised by a separate lock
  held only during setup. One maxQueueSec deadline bounds both waits.
- No caller-chosen paths, commands or environment.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional


class _InFlight:
    __slots__ = ("cancelled", "proc")

    def __init__(self) -> None:
        self.cancelled = False
        self.proc: Optional[subprocess.Popen] = None


_write_lock = threading.Lock()
_in_flight_lock = threading.Lock()
_in_flight: dict[Any, _InFlight] = {}

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from broker_paths import VERIFY_BINDS, canonical, roots, verify_binds_args  # noqa: E402
CONFIG_PATH = str(HERE / "config.json")
TAG = "[agent-e2e-broker]"
PROTOCOL = "https"

_POLICY_KEYS = frozenset({
    "version", "remoteUrl", "stateDir", "harness", "scenarioRoot", "claudeExecutable",
    "path", "models", "maxSamples", "maxJobs", "maxBudgetUsd",
    "maxTimeoutSec", "maxCallSec", "outputLimitChars", "readOnlyPaths",
    "maxCampaigns", "maxQueueSec",
})
_POSITIVE_INTS = ("maxSamples", "maxJobs", "maxTimeoutSec", "maxCallSec", "outputLimitChars",
                  "maxCampaigns", "maxQueueSec")
SLOT_POLL_SEC = 2.0
# Worst case of the setup steps' own subprocess timeouts, in order: mirror init
# 60, fetch 300, three refs resolved at up to 2 x 60 each, three scenario
# cat-files at 60, harness cat-file 60 + clone 120 + checkout 120, scenario
# archive: tar 120 then archive wait 120. _prune's local rmtree is not bounded
# and is assumed small (a week of campaign dirs). The client timeout in
# mcp-servers.json must cover maxQueueSec + this + the longest run.
SETUP_BOUND_SEC = 60 + 300 + 3 * 2 * 60 + 3 * 60 + 60 + 120 + 120 + 120 + 120
_ARGS = frozenset({"scenario", "old_ref", "new_ref", "model", "n", "jobs",
                   "budget_usd", "timeout_sec"})
_SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BRANCH_RE = re.compile(r"^origin/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_FILES = ("prompt.md", "setup.sh", "check.sh")
MAX_READ_BYTES = 256 * 1024
RETAIN_SEC = 7 * 86400


def _abs(value: object, key: str) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise RuntimeError(f"policy {key} must be an absolute path")
    return Path(value)


def _load_policy(config: dict) -> dict:
    raw = config.get("agentE2eBroker") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("policy must contain an agentE2eBroker object")
    missing = sorted(_POLICY_KEYS - raw.keys())
    if missing:
        raise RuntimeError(f"policy is missing keys: {', '.join(missing)}")
    unknown = sorted(raw.keys() - _POLICY_KEYS)
    if unknown:
        raise RuntimeError(f"policy has unknown keys: {', '.join(unknown)}")
    if raw["version"] != 1:
        raise RuntimeError("unsupported policy version")
    for key in _POSITIVE_INTS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"policy {key} must be a positive integer")
    budget = raw["maxBudgetUsd"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise RuntimeError("policy maxBudgetUsd must be a positive number")
    models = raw["models"]
    if not isinstance(models, list) or not models or not all(
            isinstance(m, str) and re.fullmatch(r"[a-z0-9.-]{1,64}", m) for m in models):
        raise RuntimeError("policy models must be a non-empty list of model aliases")
    path = raw["path"]
    if not isinstance(path, list) or not path or not all(
            isinstance(p, str) and os.path.isabs(p) and ":" not in p for p in path):
        raise RuntimeError("policy path must be a non-empty list of absolute directories")
    for key in ("harness", "scenarioRoot"):
        rel = raw[key]
        if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in rel.split("/"):
            raise RuntimeError(f"policy {key} must be a repo-relative path")
    extra = raw["readOnlyPaths"]
    if not isinstance(extra, list):
        raise RuntimeError("policy readOnlyPaths must be a list of absolute paths")
    url = raw["remoteUrl"]
    if not isinstance(url, str) or not re.fullmatch(rf"{PROTOCOL}://[A-Za-z0-9._@:/-]+", url):
        raise RuntimeError(f"policy remoteUrl must be a plain {PROTOCOL}:// URL")
    home = _passwd_home()
    policy = dict(raw)
    policy["stateDir"] = Path(os.path.realpath(_abs(raw["stateDir"], "stateDir")))
    if any(r == policy["stateDir"] or r in policy["stateDir"].parents for r in roots(home)):
        raise RuntimeError("policy stateDir must lie outside ~/Repos and ~/worktrees")
    policy["mirror"] = policy["stateDir"] / "mirror.git"
    # Each running campaign holds one slot. Slot 0 is the pre-slot broker's
    # single campaign lock, so a broker still running that code in an open
    # session occupies a slot instead of standing outside the cap. The mirror
    # lock serialises git work on the shared mirror and pruning during setup.
    policy["slots"] = [policy["stateDir"] / "lock",
                       *(policy["stateDir"] / f"slot-{i}" for i in range(1, raw["maxCampaigns"]))]
    policy["lock"] = policy["stateDir"] / "mirror.lock"
    policy["campaigns"] = policy["stateDir"] / "campaigns"
    policy["readOnlyPaths"] = [canonical(p, home) for p in extra
                               if not isinstance(p, str) or os.path.lexists(p)]
    policy["claudeExecutable"] = _abs(raw["claudeExecutable"], "claudeExecutable")
    return policy


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"BROKEN safety boundary: broker config is unreadable: {exc}") from exc


def _int(args: dict, key: str, default: int, cap: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cap:
        raise RuntimeError(f"{key} must be an integer between 1 and {cap}")
    return value


def _validate_request(args: object, policy: dict) -> dict:
    if not isinstance(args, dict):
        raise RuntimeError("arguments must be an object")
    unexpected = sorted(set(args) - _ARGS)
    if unexpected:
        raise RuntimeError(f"unexpected fields: {', '.join(unexpected)}")
    scenario = args.get("scenario")
    if not isinstance(scenario, str) or not _SCENARIO_RE.fullmatch(scenario):
        raise RuntimeError("scenario must be a tests/agent-e2e directory name ([a-z0-9-])")
    refs = {}
    for key in ("old_ref", "new_ref"):
        ref = args.get(key)
        if not isinstance(ref, str) or not (_SHA_RE.fullmatch(ref) or (
                _BRANCH_RE.fullmatch(ref) and ".." not in ref and "//" not in ref
                and not ref.endswith((".", "/", ".lock")))):
            raise RuntimeError(f"{key} must be origin/<branch> or a full 40-hex commit")
        refs[key] = ref
    model = args.get("model")
    if model not in policy["models"]:
        raise RuntimeError(f"model must be one of: {', '.join(policy['models'])}")
    n = _int(args, "n", 1, policy["maxSamples"])
    jobs = _int(args, "jobs", 1, policy["maxJobs"])
    timeout = _int(args, "timeout_sec", policy["maxTimeoutSec"], policy["maxTimeoutSec"])
    budget = args.get("budget_usd", policy["maxBudgetUsd"])
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
            or not 0 < budget <= policy["maxBudgetUsd"]:
        raise RuntimeError(f"budget_usd must be in (0, {policy['maxBudgetUsd']}]")
    worst = math.ceil(2 * n / jobs) * timeout
    if worst > policy["maxCallSec"]:
        raise RuntimeError(f"worst-case wall time {worst}s (2 arms x n={n} / jobs={jobs} x "
                           f"{timeout}s) exceeds {policy['maxCallSec']}s; lower n or timeout_sec")
    return {"scenario": scenario, **refs, "model": model, "n": n, "jobs": jobs,
            "timeout": timeout, "budget": float(budget), "call_timeout": worst + 120}


# Sandboxed sessions can write the config checkout, and git runs programs its
# config names, so the broker never runs git there (gitsecurityboundary(7)).
# It keeps its own bare mirror outside every sandbox write root, fetched from
# the canonical remote by URL and refspec, with no agent socket, a scrubbed
# environment, and command-line overrides of what config could make git run.
_GIT_OVERRIDES = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                  "-c", "credential.helper=", "-c", "core.pager=cat", "-c", "gc.auto=0",
                  "-c", "maintenance.auto=false", "-c", "fetch.recurseSubmodules=false")


def _git_env(policy: dict, protocol: str) -> dict[str, str]:
    state = policy["stateDir"]
    return {"PATH": ":".join(policy["path"]), "LANG": "C.UTF-8", "HOME": str(state / "home"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(state / "gitconfig"),
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": protocol,
            "GIT_PROTOCOL_FROM_USER": "0", "GIT_NO_LAZY_FETCH": "1"}


def _remote_protocol(policy: dict) -> str:
    return policy["remoteUrl"].split("://", 1)[0]


def _mirror_fds(policy: dict) -> tuple:
    fd = policy.get("mirror_fd")
    return (fd,) if fd is not None else ()


def _git_run(policy: dict, argv: list, timeout: int = 60, protocol: str = "file"
             ) -> subprocess.CompletedProcess:
    # Own process group, killed whole on timeout. Git and its helpers also
    # inherit the mirror lock's fd, so the flock stays held while any of them
    # lives, even past the broker's death.
    argv = ["git", *_GIT_OVERRIDES, *argv]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_git_env(policy, protocol), stdin=subprocess.DEVNULL,
                            start_new_session=True, pass_fds=_mirror_fds(policy))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git(policy: dict, *argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _git_run(policy, [f"--git-dir={policy['mirror']}", *argv], timeout)


# Every path the broker or the systemd manager writes lies in stateDir, outside
# every sandbox write root: in a shared root such as /tmp, a planted symlink or
# a guessed campaign name redirected those writes into the owner's files.
def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f"BROKEN safety boundary: {path} is not a private directory of this user")
    return path


def _update_mirror(policy: dict) -> None:
    state = _private_dir(policy["stateDir"])
    _private_dir(state / "home")
    (state / "gitconfig").write_text("[safe]\n\tbareRepository = explicit\n")
    if not policy["mirror"].exists():
        init = _git_run(policy, ["init", "--quiet", "--bare", "--template=", str(policy["mirror"])])
        if init.returncode != 0:
            raise RuntimeError(f"could not create the mirror: {init.stderr.strip()[:300]}")
    try:
        fetch = _git_run(policy, [f"--git-dir={policy['mirror']}", "fetch", "--quiet", "--prune",
                                  "--no-tags", "--no-recurse-submodules", policy["remoteUrl"],
                                  "+refs/heads/*:refs/remotes/origin/*"],
                         timeout=300, protocol=_remote_protocol(policy))
    except subprocess.TimeoutExpired:
        fetch = subprocess.CompletedProcess([], 124, "", "timed out")
    # Fail closed: stale refs must never stand in for what origin holds now.
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch {policy['remoteUrl']} failed: {fetch.stderr.strip()[:300]}")


def _checkout_harness(policy: dict, sha: str, dest: Path) -> Path:
    """A fresh checkout of SHA at DEST: the harness stages arms with `git archive`, so it
    must sit in a repository, and the config repo's own working tree never runs."""
    if _git(policy, "cat-file", "-e", f"{sha}:{policy['harness']}").returncode != 0:
        raise RuntimeError(f"BROKEN safety boundary: origin/master has no {policy['harness']}")
    for argv in (["clone", "--quiet", "--shared", "--no-checkout", "--template=",
                  str(policy["mirror"]), str(dest)],
                 ["-C", str(dest), "checkout", "--quiet", "--detach", sha]):
        done = _git_run(policy, argv, timeout=120)
        if done.returncode != 0:
            raise RuntimeError(f"could not check out the harness at {sha}: "
                               f"{done.stderr.strip()[:300]}")
    return dest / policy["harness"]


def _resolve(policy: dict, ref: str) -> str:
    if _SHA_RE.fullmatch(ref):
        if _git(policy, "cat-file", "-e", f"{ref}^{{commit}}").returncode != 0:
            raise RuntimeError(f"{ref} is not a commit on origin (after fetch)")
        held = _git(policy, "branch", "-r", "--contains", ref)
        if held.returncode != 0 or not held.stdout.strip():
            raise RuntimeError(f"{ref} is not reachable from any origin branch; push it first")
        return ref
    out = _git(policy, "rev-parse", "--verify", "--quiet", f"refs/remotes/{ref}^{{commit}}")
    if out.returncode != 0:
        raise RuntimeError(f"{ref} does not exist on origin (after fetch)")
    return out.stdout.strip()


def _env(policy: dict) -> dict[str, str]:
    # Built from scratch: no caller tokens, agent sockets or API keys reach the
    # run. `claude -p` authenticates with the subscription credentials file.
    home = str(Path.home())
    return {"HOME": home, "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("USER", ""),
            "PATH": ":".join(policy["path"]), "LANG": "C.UTF-8"}


def _manager_env(policy: dict) -> dict[str, str]:
    return {**_env(policy), "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def _passwd_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# Everything under a campaign's output dir was writable by scenario code, so
# no component is followed if it is a symlink, only a regular file is read,
# and at most LIMIT bytes of it.
def _read_beneath(root: Path, rel: str, limit: int = MAX_READ_BYTES, tail: bool = True) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds: list[int] = []
    try:
        fds.append(os.open(root, flags | os.O_DIRECTORY))
        *dirs, name = rel.split("/")
        for d in dirs:
            fds.append(os.open(d, flags | os.O_DIRECTORY, dir_fd=fds[-1]))
        fds.append(os.open(name, flags | os.O_NONBLOCK, dir_fd=fds[-1]))
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode):
            return f"[{rel}: not a regular file; not read]"
        if tail:
            os.lseek(fds[-1], max(0, st.st_size - limit), os.SEEK_SET)
        chunks, left = [], limit
        while left and (chunk := os.read(fds[-1], left)):
            chunks.append(chunk)
            left -= len(chunk)
    except FileNotFoundError:
        return ""
    except OSError as e:
        return f"[{rel}: not read: {e.strerror}]"
    finally:
        for fd in fds:
            os.close(fd)
    return b"".join(chunks).decode("utf-8", "replace")


def _extract_scenarios(policy: dict, sha: str, dest: Path) -> None:
    dest.mkdir(mode=0o700)
    archive = subprocess.Popen(["git", *_GIT_OVERRIDES, f"--git-dir={policy['mirror']}", "archive",
                                sha, "--", policy["scenarioRoot"]], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=_git_env(policy, "file"),
                               stdin=subprocess.DEVNULL, pass_fds=_mirror_fds(policy))
    # The archive must be gone before the caller frees the mirror lock, on every path.
    try:
        tar = subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=archive.stdout,
                             capture_output=True, env=_env(policy), timeout=120)
        archive.stdout.close()
        if archive.wait(timeout=120) != 0 or tar.returncode != 0:
            raise RuntimeError(f"could not extract {policy['scenarioRoot']} from {sha}")
    finally:
        if archive.poll() is None:
            archive.kill()
            archive.wait()
        archive.stdout.close()


# The harness runs in a hardened user unit, because scenario setup.sh and
# check.sh come from a pushed branch and run as the owner. What stays reachable
# is what a campaign needs: its output dir, the Claude login, the harness
# checkout, scenarios, mirror and readOnlyPaths (targets of its skill
# symlinks) read-only, and the network. bwrap, which the harness puts the
# agent under, must still mount a fresh /proc; ProtectKernelTunables=,
# ProtectKernelLogs= and ProtectHostname= overmount /proc and make the kernel
# refuse that (measured), so they are left out. Under PrivatePIDs= the harness
# is PID 1 and ignores SIGTERM, hence KillSignal=SIGKILL.
HARDENING = (
    "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateUsers=self",
    "NoNewPrivileges=yes", "PrivatePIDs=yes", "PrivateIPC=yes", "PrivateDevices=yes",
    "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
    "LockPersonality=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
    "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "SystemCallFilter=@system-service @mount", "SystemCallErrorNumber=EPERM",
    "RestrictNamespaces=user mnt pid net",
)
HIDDEN = ("/run/github-app", "/run/dbus", "/run/nscd", "/run/agenix", "/run/agenix.d",
          "/run/secrets")
UNIT_PREFIX = "claude-agent-e2e-"


def _unit_properties(policy: dict, campaign: Path, runtime: int) -> list[str]:
    home = Path.home()
    exe = policy["claudeExecutable"]
    out = campaign / "out"
    return [
        "Type=exec", "Description=agent-e2e campaign", f"RuntimeMaxSec={runtime}",
        "KillSignal=SIGKILL",
        f"WorkingDirectory={out}", f"StandardOutput=append:{campaign}/harness.out",
        f"StandardError=append:{campaign}/harness.err",
        *HARDENING,
        *(f"InaccessiblePaths=-{p}" for p in HIDDEN),
        f"BindPaths={out}", f"BindPaths=-{home}/.claude/.credentials.json",
        # The harness checkout borrows the mirror's objects (clone --shared).
        *(f"BindReadOnlyPaths={p}" for p in (campaign / "harness", campaign / "scenarios",
                                             policy["mirror"],
                                             *(p for p, _ in policy["readOnlyPaths"]))),
        *(f"BindReadOnlyPaths=-{p}" for p in (
            home / ".claude/plugins", home / ".config/caveman",
            # dcg's config.toml links through the nix store back to this one.
            home / ".config/dcg", home / ".claude/dcg.toml",
            exe.parent, Path(os.path.realpath(exe)).parent)),
    ]


# The unit shares the host's network namespace, and with it every abstract unix
# socket there (X11, ICE, ...). Landlock's abstract-socket scope refuses those,
# leaving path sockets and the network the campaign needs; no Landlock, no run.
_SCOPE_ABSTRACT_SOCKETS = r'''
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
attr = (ctypes.c_uint64 * 3)(0, 0, 1)
fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
if fd < 0 or libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
    sys.exit("BROKEN safety boundary: cannot scope abstract unix sockets: "
             + os.strerror(ctypes.get_errno()))
os.execvp(sys.argv[1], sys.argv[1:])
'''
_VERIFY_THEN_EXEC = 'refuse() { exit 125; }\n' + VERIFY_BINDS + 'exec "$@"\n'


# The supervisor is signalled by the kernel when the broker dies (pdeathsig);
# it then stops the unit and kills its own group. It also holds the campaign
# lock, so the lock frees only once the unit has stopped.
_SUPERVISOR = ('unit=$1; systemctl=$2; shift 2; '
               'trap "$systemctl --user stop $unit.service; kill -KILL 0" TERM; "$@" & wait $!')


def _supervised(argv: list[str], policy: dict, campaign: Path, runtime: int) -> list[str]:
    path = ":".join(policy["path"])
    tools = {name: shutil.which(name, path=path)
             for name in ("setpriv", "bash", "env", "stat", "systemd-run", "systemctl", "python3")}
    missing = [name for name, found in tools.items() if not found]
    if missing:
        raise RuntimeError(f"BROKEN safety boundary: not on policy path: {', '.join(missing)}")
    unit = UNIT_PREFIX + campaign.name
    start = [tools["systemd-run"], "--user", f"--unit={unit}", "--wait", "--collect",
             "--expand-environment=no"]
    for prop in _unit_properties(policy, campaign, runtime):
        start += ["-p", prop]
    env = [f"{k}={v}" for k, v in _env(policy).items()]
    return [tools["setpriv"], "--pdeathsig", "TERM", "--", tools["bash"], "-c", _SUPERVISOR,
            "agent-e2e-supervisor", unit, tools["systemctl"],
            *start, "--", tools["env"], "-i", *env,
            tools["bash"], "-c", _VERIFY_THEN_EXEC, "agent-e2e-broker",
            *verify_binds_args(tools["stat"], policy["readOnlyPaths"]),
            tools["python3"], "-I", "-c", _SCOPE_ABSTRACT_SOCKETS, *argv]


def _run_argv(argv: list[str], env: dict, timeout_s: int, rec: Optional[_InFlight],
              lock_fd: int):
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
                            pass_fds=(lock_fd,))

    def signal_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass  # already exited

    if rec is not None:
        with _in_flight_lock:
            rec.proc = proc
            cancelled = rec.cancelled
        if cancelled:
            signal_group(signal.SIGTERM)
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            signal_group(signal.SIGKILL)
            out, err = proc.communicate()
        return 124, out or "", (err or "") + f"\n{TAG} harness exceeded {timeout_s}s; stopped"


_SECRET_PATTERNS = [
    ("anthropic-api", re.compile(r"sk-ant-(?:api|oat|ort)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\bgh[psoure]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("slack-token", re.compile(r"\bxox[boaprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws-access", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("tavily-key", re.compile(r"\btvly-[A-Za-z0-9_\-]{20,}\b")),
    ("age-secret-key", re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{58}\b")),
]
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----"
    r"(?:.*?-----END (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----|.*\Z)",
    re.DOTALL,
)


def _redact(body: str) -> str:
    body = _PRIVATE_KEY_RE.sub(lambda m: f"[REDACTED:private-key:{len(m.group(0))}chars]", body)
    for label, pat in _SECRET_PATTERNS:
        body = pat.sub(lambda m, _l=label: f"[REDACTED:{_l}:{len(m.group(0))}chars]", body)
    return body


def _report(out: Path, scenario: str, n: int) -> str:
    lines = []
    for arm in ("new", "old"):
        for i in range(1, n + 1):
            run = f"runs/{scenario}/{arm}/{i}"
            try:
                v = json.loads(_read_beneath(out, f"{run}/verdict.json", tail=False))
            except ValueError:
                continue
            if not isinstance(v, dict):
                continue
            text = _read_beneath(out, f"{run}/final.txt", limit=4096, tail=False).strip()
            lines.append(f"- {v.get('arm')}#{v.get('run')} {v.get('verdict')} "
                         f"(${v.get('cost_usd')}, {v.get('seconds')}s): {v.get('why', '')}\n"
                         f"  run dir: {out / run}\n  final.txt: {text[:600]!r}")
    return "\n".join(lines)


def _prune(campaigns: Path) -> None:
    cutoff = time.time() - RETAIN_SEC
    for entry in os.scandir(campaigns):
        if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _acquire(paths: list, deadline: float, rec: Optional[_InFlight], what: str,
             busy: str) -> int:
    """Poll PATHS until one locks, the request is cancelled, or DEADLINE passes.
    Polling is not first-come-first-served: a later caller may win a freed lock."""
    while True:
        for path in paths:
            fd = _open_lock(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
        if rec is not None and rec.cancelled:
            raise RuntimeError(f"cancelled while waiting for {what}")
        if time.monotonic() >= deadline:
            raise RuntimeError(busy)
        time.sleep(SLOT_POLL_SEC)


def _acquire_slot(policy: dict, rec: Optional[_InFlight], deadline: float) -> int:
    """A free campaign slot's locked fd. The fd goes to the supervisor, so the
    slot frees only once the unit stops."""
    return _acquire(policy["slots"], deadline, rec, "a campaign slot",
                    f"all {len(policy['slots'])} agent-e2e campaign slots stayed busy "
                    f"for {policy['maxQueueSec']}s; try again later")


def _tool_run(args: dict, rec: Optional[_InFlight] = None) -> str:
    policy = _load_policy(_load_config())
    req = _validate_request(args, policy)
    exe = policy["claudeExecutable"]
    if not os.access(exe, os.X_OK):
        raise RuntimeError(f"BROKEN safety boundary: claude is not executable: {exe}")
    _private_dir(policy["stateDir"])
    # One deadline bounds every wait before the run: slot, then mirror lock.
    deadline = time.monotonic() + policy["maxQueueSec"]
    lock = _acquire_slot(policy, rec, deadline)
    try:
        campaign = None
        mirror = _acquire([policy["lock"]], deadline, rec, "the mirror lock",
                          f"the agent-e2e mirror lock stayed busy past the "
                          f"{policy['maxQueueSec']}s queue limit; try again later")
        policy["mirror_fd"] = mirror
        try:
            def check_cancel() -> None:
                if rec is not None and rec.cancelled:
                    raise RuntimeError("cancelled during campaign setup")
            check_cancel()
            _update_mirror(policy)
            check_cancel()
            master = _resolve(policy, "origin/master")
            check_cancel()
            old = _resolve(policy, req["old_ref"])
            check_cancel()
            new = _resolve(policy, req["new_ref"])
            check_cancel()
            scen_rel = f"{policy['scenarioRoot']}/{req['scenario']}"
            for name in _RUN_FILES:
                if _git(policy, "cat-file", "-e", f"{new}:{scen_rel}/{name}").returncode != 0:
                    raise RuntimeError(f"{req['new_ref']} has no {scen_rel}/{name}")
            campaigns = _private_dir(policy["campaigns"])
            _prune(campaigns)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            campaign = Path(tempfile.mkdtemp(prefix=f"{stamp}-{req['scenario']}-{new[:8]}-",
                                             dir=campaigns))
            out = campaign / "out"
            out.mkdir(mode=0o700)
            for name in ("harness.out", "harness.err"):
                os.close(os.open(campaign / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
            _extract_scenarios(policy, new, campaign / "scenarios")
            check_cancel()
            harness = _checkout_harness(policy, master, campaign / "harness")
            check_cancel()
        except BaseException:
            if campaign is not None:
                shutil.rmtree(campaign / "harness", ignore_errors=True)
            raise
        finally:
            policy.pop("mirror_fd", None)
            os.close(mirror)
        try:
            argv = [sys.executable, "-I", str(harness), "run",
                    str(campaign / "scenarios" / scen_rel),
                    "--arm", f"old={old}", "--arm", f"new={new}",
                    "--n", str(req["n"]), "--jobs", str(req["jobs"]), "--model", req["model"],
                    "--budget", f"{req['budget']:g}", "--timeout", str(req["timeout"]),
                    "--out", str(out / "runs"), "--claude", str(exe)]
            rc, _, client = _run_argv(_supervised(argv, policy, campaign, req["call_timeout"]),
                                      _manager_env(policy), req["call_timeout"] + 60, rec, lock)
            stdout, stderr = (_read_beneath(campaign, name) for name in ("harness.out", "harness.err"))
            stderr = "\n".join(part for part in (stderr.rstrip(), client.strip()) if part)
        finally:
            shutil.rmtree(campaign / "harness", ignore_errors=True)
    finally:
        os.close(lock)
    header = (f"agent-e2e campaign `{req['scenario']}` model={req['model']} n={req['n']} "
              f"jobs={req['jobs']} budget=${req['budget']:g} timeout={req['timeout']}s\n"
              f"harness = origin/master = {master}\n"
              f"old = {req['old_ref']} = {old}\nnew = {req['new_ref']} = {new}\n"
              f"output: {out}\n")
    body = "\n".join([header, stdout.rstrip(), "--- per run ---",
                      _report(out, req["scenario"], req["n"]),
                      *(["--- stderr ---", stderr.rstrip()[-4000:]] if stderr.strip() else []),
                      f"--- harness exit {rc} ---"])
    body = _redact(body)
    limit = policy["outputLimitChars"]
    return body if len(body) <= limit else body[:limit] + f"\n{TAG} output truncated; see {out}"


TOOLS = [{
    "name": "run",
    "description": (
        "Runs one agent-config e2e scenario (tests/agent-e2e/<scenario> from new_ref) "
        "through scripts/agent-e2e/agent_e2e.py with a real headless Claude agent, arm "
        "old=old_ref vs arm new=new_ref, outside the caller's sandbox. Refs must be on origin "
        "(origin/<branch> or a pushed 40-hex commit). Up to maxCampaigns campaigns run at "
        "once; when every slot is busy the call waits for one to free (up to maxQueueSec, "
        "not strictly first-come-first-served), then blocks until the run is done (about an hour at "
        "most after setup). Returns the harness summary table, every run's "
        "verdict and final.txt, and the output directory holding transcripts. Every PASS, "
        "FAIL and INCONCLUSIVE is reported; nothing is retried."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "scenario": {"type": "string", "description": "Directory name under tests/agent-e2e."},
            "old_ref": {"type": "string", "description": "Baseline config, e.g. origin/master."},
            "new_ref": {"type": "string", "description": "Candidate config, e.g. origin/feat/x."},
            "model": {"type": "string", "description": "Model alias for the agent under test."},
            "n": {"type": "integer", "description": "Samples per arm (default 1)."},
            "jobs": {"type": "integer", "description": "Parallel runs (default 1)."},
            "budget_usd": {"type": "number", "description": "Per-run --max-budget-usd cap."},
            "timeout_sec": {"type": "integer", "description": "Per-run timeout in seconds."},
        },
        "required": ["scenario", "old_ref", "new_ref", "model"],
        "additionalProperties": False,
    },
}]

SERVER_INFO = {"name": "agent-e2e-broker", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}


def _respond(msg_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error is not None else "result"] = error if error is not None else result
    with _write_lock:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _send_response(msg_id, result=None, error=None) -> None:
    with _in_flight_lock:
        rec = _in_flight.pop(msg_id, None)
        if rec is not None and rec.cancelled:
            return
    _respond(msg_id, result=result, error=error)


def _on_cancelled(req_id) -> None:
    with _in_flight_lock:
        rec = _in_flight.get(req_id)
        if rec is None:
            return
        rec.cancelled = True
        proc = rec.proc
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already exited


def _dispatch_tools_call(msg_id, params, rec) -> None:
    try:
        name = params.get("name")
        if name != "run":
            _send_response(msg_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
            return
        text = _tool_run(params.get("arguments") or {}, rec)
        _send_response(msg_id, result={"content": [{"type": "text", "text": text}]})
    except Exception as e:
        _send_response(msg_id, result={"content": [{"type": "text", "text": f"ERROR: {e}"}],
                                       "isError": True})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        _respond(msg_id, result={"protocolVersion": "2024-11-05",
                                 "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO})
        return
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        if params.get("requestId") is not None:
            _on_cancelled(params["requestId"])
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        if msg_id is None:
            return
        with _in_flight_lock:
            if msg_id in _in_flight:
                rec = None
            else:
                rec = _in_flight[msg_id] = _InFlight()
        if rec is None:
            _respond(msg_id, error={"code": -32600, "message": f"Request id {msg_id} already in flight"})
            return
        threading.Thread(target=_dispatch_tools_call, args=(msg_id, params, rec),
                         daemon=True, name=f"agent-e2e-broker-{msg_id}").start()
        return
    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:
            sys.stderr.write(f"{TAG} unhandled error: {e}\n")
            sys.stderr.flush()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with _in_flight_lock:
            if not _in_flight:
                return
        time.sleep(0.05)


if __name__ == "__main__":
    main()

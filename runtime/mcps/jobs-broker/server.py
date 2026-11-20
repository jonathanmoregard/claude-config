#!/usr/bin/env python3
"""Jobs broker over stdio MCP: long jobs as systemd user units, run under srt.

The caller chooses the command. It runs under Anthropic's sandbox runtime with
the session's own sandbox policy (the `sandbox` block of ~/.claude/settings.json),
its writes narrowed to the repo; the unit around it adds durability and hides
home and system secrets. The only caller inputs either takes are the repo
directory, the slug, the runtime bound and the profile name.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from broker_paths import canonical, is_git_dir  # noqa: E402
RECORDER = HERE.parents[1] / "scripts" / "record-user-unit-failure"
SYSTEM_BIN = "/run/current-system/sw/bin"
PYTHON = f"{SYSTEM_BIN}/python3"
SRT = f"{SYSTEM_BIN}/srt"
RIPGREP = f"{SYSTEM_BIN}/rg"
NIX_CACHE = ".cache/nix"
MANAGED = Path("/etc/claude-code")
STATE = ".local/state/claude-jobs"
HELD = f"{STATE}/quarantine"
# What Claude Code, srt, Codex and Cursor protect, together: config an agent,
# editor, shell, direnv or git reads and runs later.
PROTECTED_NAMES = frozenset({
    ".git", ".claude", ".codex", ".agents", ".cursor", ".vscode", ".idea", ".mcp.json", ".envrc",
    ".gitmodules", ".gitconfig", ".ripgreprc", ".bashrc", ".bash_profile", ".bash_login",
    ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin",
})
PREFIX = "claude-job-"
PROFILES = ("repo", "nix")
MIN_RUNTIME_SEC = 10
DEFAULT_RUNTIME_SEC = 4 * 3600
MAX_RUNTIME_SEC = 6 * 3600
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
DEFAULT_LOG_LINES = 100
MAX_LOG_LINES = 500
MAX_LOG_BYTES = 64 * 1024
MAX_WAIT_SEC = 540
MAX_SCAN_ENTRIES = 2_000_000

_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
_RULE_RE = re.compile(r"([A-Za-z]+)(?:\((.*)\))?", re.DOTALL)
_SHOW = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
         "ExecMainStartTimestamp", "ExecMainExitTimestamp")

# srt needs what is let through here (measured): inet sockets and its own
# network namespace for the domain-allowlisted proxy that replaces
# PrivateNetwork=, user/mnt/pid namespaces and @mount for bubblewrap, and a
# /proc it can mount, which ProtectKernelTunables=, ProtectKernelLogs= and
# ProtectHostname= each prevent.
HARDENING = (
    "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateUsers=self",
    "NoNewPrivileges=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "SystemCallFilter=@system-service @mount", "SystemCallErrorNumber=EPERM", "PrivatePIDs=yes",
    "RestrictNamespaces=user mnt pid net", "PrivateIPC=yes", "PrivateDevices=yes",
    "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
    "LockPersonality=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
    "CapabilityBoundingSet=",
)
HIDDEN = ("/run/github-app", "/run/dbus", "/run/nscd", "/run/agenix", "/run/agenix.d",
          "/run/secrets")

# The manager resolves BindPaths= only when the unit starts, so a directory
# swapped for a symlink after confine_repo checked it would be bound instead.
# The job therefore first proves each bound path is the inode that was checked.
#
# Under PrivatePIDs this shell is PID 1 and cannot die of SIGTERM, so a stop
# (systemctl stop, logout, RuntimeMaxSec) exits 143, which the unit counts as
# a clean stop (SuccessExitStatus=143); the job's own 143 is reported as 1.
WRAPPER = r'''
stopping=
trap stopping=1 TERM
while [ "$1" != -- ]; do
  if [ "$(stat -c %d:%i -- "$1")" != "$2" ]; then
    echo "jobs-broker: $1 is not the directory that was checked; job not run"
    echo "MARKER_FAILED rc=125"
    exit 125
  fi
  shift 2
done
shift
"$@"
rc=$?
if [ -n "$stopping" ]; then echo "MARKER_FAILED stopped rc=$rc"; exit 143; fi
if [ "$rc" = 0 ]; then echo MARKER_OK; else echo "MARKER_FAILED rc=$rc"; fi
if [ "$rc" = 143 ]; then exit 1; fi
exit "$rc"
'''


class Job(NamedTuple):
    unit: str
    repo: Path
    ident: str
    readonly: list
    argv: list
    runtime: int
    profile: str
    policy: dict
    baseline: dict


def _fields(args: object, allowed: set) -> dict:
    if not isinstance(args, dict):
        raise RuntimeError("arguments must be an object")
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        raise RuntimeError(f"unexpected fields: {', '.join(unexpected)}")
    return args


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RuntimeError(f"{key} must be an integer between {low} and {high}")
    return value


def unit_name(slug: object) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise RuntimeError("slug must be 1-48 of [A-Za-z0-9_-], starting alphanumeric")
    return PREFIX + slug


def log_path(home: Path, unit: str) -> Path:
    return home / ".local/state/claude-jobs" / f"{unit}.log"


def policy_path(home: Path, unit: str) -> Path:
    return home / ".local/state/claude-jobs" / f"{unit}.srt.json"


class Scan(NamedTuple):
    git_state: list
    protected: list


def _protected_name(name: str, parts: tuple, globs: list) -> bool:
    return name.lower() in PROTECTED_NAMES or any(_glob_match(g, parts) for g in globs)


# srt protects only hooks and config of a `.git` dir, and no bare repo; a
# commondir, a submodule's config or a worktree's config.worktree is as good a
# plant. So every git dir and gitfile in the tree is write-denied whole, as is
# every other name that runs later, at any depth.
def _scan(repo: Path, globs: list) -> Scan:
    found, protected, pending, seen = [], [], [repo], 0
    if any(_glob_match(g, repo.parts) for g in globs):
        protected.append(repo)
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_SCAN_ENTRIES:
                        raise RuntimeError(f"{repo} has over {MAX_SCAN_ENTRIES} entries to scan")
                    path = Path(entry.path)
                    if entry.name == ".git":
                        if entry.is_symlink():
                            raise RuntimeError(f"{entry.path} is a symlink")
                        found.append(path)
                    elif _protected_name(entry.name, path.parts, globs):
                        protected.append(path)
                    elif entry.is_dir(follow_symlinks=False):
                        if is_git_dir(path):
                            found.append(path)
                        else:
                            pending.append(entry.path)
        except OSError as e:
            raise RuntimeError(f"cannot scan {repo} for git dirs: {e}") from None
    return Scan(found, protected)


def baseline_path(home: Path, unit: str) -> Path:
    return home / STATE / f"{unit}.baseline.json"


def write_baseline(job: Job, home: Path) -> None:
    path = baseline_path(home, job.unit)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                   "w") as fh:
        json.dump(job.baseline, fh)


def _git_dir_at(dir_fd: int, name: str) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        kinds = [os.stat(n, dir_fd=fd, follow_symlinks=False).st_mode for n in ("HEAD", "objects", "refs")]
    except OSError:
        return False
    finally:
        os.close(fd)
    return stat.S_ISREG(kinds[0]) and stat.S_ISDIR(kinds[1]) and stat.S_ISDIR(kinds[2])


# Claude Code quarantines what a sandboxed command leaves behind that runs
# later; this is that step for a job. It runs as the owner outside every
# sandbox, so it walks by directory fd and moves with renameat: a directory
# swapped for a symlink mid-walk is neither followed nor moved through.
def sweep(unit: str, home: Path) -> list:
    try:
        base = json.loads(baseline_path(home, unit).read_text())
        repo, ident = Path(base["repo"]), base["ident"]
        existing, globs = set(base["existing"]), [tuple(g) for g in base["globs"]]
        st = os.lstat(repo)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"no usable baseline for {unit}; nothing moved: {e}") from None
    if not stat.S_ISDIR(st.st_mode) or f"{st.st_dev}:{st.st_ino}" != ident:
        raise RuntimeError(f"{repo} is not the directory the job ran in; nothing moved")
    dest, moved = home / HELD / f"{unit}.{time.time_ns()}", []
    for top, dirs, files, fd in os.fwalk(repo, follow_symlinks=False):
        rel_top = os.path.relpath(top, repo)
        for name in [*dirs, *files]:
            rel = os.path.normpath(os.path.join(rel_top, name))
            if not (_protected_name(name, (*repo.parts, *Path(rel).parts), globs)
                    or (name in dirs and _git_dir_at(fd, name))):
                continue
            if name in dirs:
                dirs.remove(name)
            if rel in existing:
                continue
            (dest / rel).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(name, dest / rel, src_dir_fd=fd)
            moved.append(rel)
            print(f"jobs-broker: {rel} appeared during the job; moved out of the repo to {dest / rel}")
    if moved:
        with open(home / STATE / f"{unit}.moved", "a") as fh:
            fh.writelines(f"{rel}\n" for rel in moved)
    return moved


def _gitdir_pointer(gitfile: Path) -> Path | None:
    try:
        with open(gitfile, encoding="utf-8") as fh:
            match = re.fullmatch(r"gitdir: (.+)\n?", fh.read(MAX_ARG_CHARS))
    except (OSError, UnicodeDecodeError):
        return None
    return Path(os.path.realpath(gitfile.parent / match.group(1))) if match else None


def _common_dir(gitdir: Path) -> Path:
    try:
        return Path(os.path.realpath(gitdir / (gitdir / "commondir").read_text().strip()))
    except (OSError, UnicodeDecodeError):
        return gitdir


# A worktree's or submodule's git dir outside the repo is hidden with the rest
# of home, so it is bound read-only for git to read; srt never lets it be written.
def _outside_git_dirs(repo: Path, git_state: list, home: Path) -> list:
    found = set()
    for gitfile in (p for p in git_state if p.is_file()):
        gitdir = _gitdir_pointer(gitfile)
        if gitdir is not None:
            found.update(canonical(str(p), home, git_ok=True)
                         for p in {gitdir, _common_dir(gitdir)} if repo not in p.parents)
    unique = sorted(found)
    return [(p, i) for p, i in unique if not any(q in p.parents for q, _ in unique)]


def settings_sources(home: Path) -> list:
    """(file, must exist) for each settings file Claude Code merges for this
    user; the repo's own .claude/settings*.json are never trusted here."""
    dropins = MANAGED / "managed-settings.d"
    try:
        extra = sorted(p for p in dropins.iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        extra = []
    except OSError as e:
        raise RuntimeError(f"cannot list {dropins}: {e}") from None
    return [(home / ".claude/settings.json", True), (MANAGED / "managed-settings.json", False),
            *((p, True) for p in extra)]


def _read_settings(path: Path, required: bool) -> dict | None:
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"{path} is missing; no job runs without the session's "
                               "sandbox policy") from None
        return None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot read {path}: {e}") from None
    if not isinstance(settings, dict):
        raise RuntimeError(f"{path} is not a settings object")
    return settings


# Claude Code's own reading of a rule's path when it builds its sandbox.
def _rule_path(content: str, settings_dir: Path, repo: Path, home: Path) -> str:
    if "\\" in content:
        raise RuntimeError(f"rule path {content!r} has an escape this broker does not translate")
    if content.startswith("//"):
        return content[1:]
    if content.startswith("/"):
        return str(settings_dir / content[1:])
    if content == "~" or content.startswith("~/"):
        return str(home / content[2:])
    return str(repo / content)


class SessionPolicy(NamedTuple):
    sandbox: dict
    deny_read: list
    deny_write: list
    denied_domains: list


def _merge_source(path: Path, settings: dict, repo: Path, home: Path, base: bool,
                  into: SessionPolicy) -> None:
    sandbox = settings.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise RuntimeError(f"{path}: sandbox is not an object")
    fs, net = sandbox.get("filesystem") or {}, sandbox.get("network") or {}
    if not base and (set(sandbox) - {"filesystem", "network"} or set(fs) - {"denyRead", "denyWrite"}
                     or set(net) - {"deniedDomains"}):
        raise RuntimeError(f"{path}: only deny entries of a sandbox block are understood here")
    into.deny_read.extend(fs.get("denyRead", []))
    into.deny_write.extend(fs.get("denyWrite", []))
    into.denied_domains.extend(net.get("deniedDomains", []))
    rules = (settings.get("permissions") or {}).get("deny", [])
    if not isinstance(rules, list):
        raise RuntimeError(f"{path}: permissions.deny is not a list")
    for rule in rules:
        if not isinstance(rule, str):
            raise RuntimeError(f"{path}: deny rule {rule!r} is not a string")
        match = _RULE_RE.fullmatch(rule.strip())
        tool, content = match.groups() if match else (None, None)
        if tool == "WebFetch" and content and content.startswith("domain:"):
            into.denied_domains.append(content[len("domain:"):])
        elif tool in ("Read", "Edit", "Write"):
            if not content:
                raise RuntimeError(f"{path}: deny rule {rule!r} names no path")
            target = into.deny_read if tool == "Read" else into.deny_write
            target.append(_rule_path(content, path.parent, repo, home))


def session_policy(repo: Path, home: Path) -> SessionPolicy:
    """The sandbox the session runs under, from sandbox settings and permission
    deny rules of every trusted scope, as Claude Code composes it."""
    loaded = [(p, s) for p, required in settings_sources(home)
              if (s := _read_settings(p, required)) is not None]
    user = loaded[0][1]
    if not isinstance(user.get("sandbox"), dict):
        raise RuntimeError("~/.claude/settings.json has no sandbox block; no job runs without it")
    policy = SessionPolicy(user["sandbox"], [], [], [])
    for i, (path, settings) in enumerate(loaded):
        _merge_source(path, settings, repo, home, i == 0, policy)
    return policy


def _expand_home(p: str, home: Path) -> str:
    return str(home / p[2:]) if p.startswith("~/") else p


def _glob(p: str) -> tuple | None:
    while p.endswith("/**"):
        p = p[:-3]
    return PurePosixPath(p).parts if any(c in p for c in "*?[") else None


def _glob_match(pattern: tuple, parts: tuple) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_glob_match(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) \
        and _glob_match(pattern[1:], parts[1:])


def srt_policy(repo: Path, scan: Scan, profile: str, home: Path, session: SessionPolicy) -> dict:
    fs = session.sandbox.get("filesystem") or {}
    session_writes = {_expand_home(p, home) for p in fs.get("allowWrite", [])}
    writes = [str(repo)]
    if profile == "nix" and str(home / NIX_CACHE) in session_writes:
        writes.append(str(home / NIX_CACHE))
    net = session.sandbox.get("network") or {}
    return {
        "network": {**net, "deniedDomains": [*net.get("deniedDomains", []),
                                             *session.denied_domains]},
        "filesystem": {
            "denyRead": session.deny_read, "allowRead": fs.get("allowRead", []),
            "allowWrite": writes,
            # A name absent at the top level is denied too: srt holds it with
            # an empty file, so it cannot be created.
            "denyWrite": list(dict.fromkeys([
                *(p for p in session.deny_write if _glob(p) is None),
                *map(str, scan.protected), *map(str, scan.git_state),
                *(str(repo / n) for n in sorted(PROTECTED_NAMES))])),
        },
        "mandatoryDenySearchDepth": 10,
        "ripgrep": {"command": RIPGREP},
    }


def validate_start(args: object, home: Path) -> Job:
    args = _fields(args, {"repo", "argv", "slug", "max_runtime_sec", "profile"})
    unit = unit_name(args.get("slug"))
    argv = args.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV or not all(
            isinstance(a, str) and "\0" not in a and len(a) <= MAX_ARG_CHARS for a in argv):
        raise RuntimeError(f"argv must be 1-{MAX_ARGV} strings of at most {MAX_ARG_CHARS} chars")
    runtime = _int(args, "max_runtime_sec", DEFAULT_RUNTIME_SEC, MIN_RUNTIME_SEC, MAX_RUNTIME_SEC)
    profile = args.get("profile", "repo")
    if profile not in PROFILES:
        raise RuntimeError(f"profile must be one of: {', '.join(PROFILES)}")
    repo, ident = canonical(args.get("repo"), home)
    try:
        dot_git = os.lstat(repo / ".git").st_mode
    except FileNotFoundError:
        dot_git = 0
    if not (stat.S_ISDIR(dot_git) or stat.S_ISREG(dot_git)):
        raise RuntimeError(f"{repo} is not the top level of a git work tree")
    session = session_policy(repo, home)
    # srt drops a write glob on Linux, as bwrap binds only concrete paths, so the
    # broker expands each to what it matches in the repo when the job starts.
    globs = [g for p in session.deny_write if (g := _glob(_expand_home(p, home)))]
    scan = _scan(repo, globs)
    baseline = {"repo": str(repo), "ident": ident, "globs": globs,
                "existing": [str(p.relative_to(repo)) for p in (*scan.git_state, *scan.protected)
                             if p != repo]}
    return Job(unit, repo, ident, _outside_git_dirs(repo, scan.git_state, home), list(argv),
               runtime, profile, srt_policy(repo, scan, profile, home, session), baseline)


def properties(job: Job, home: Path) -> list:
    log = log_path(home, job.unit)
    sweep_cmd = f"+{PYTHON} -I {HERE / 'server.py'} sweep {job.unit}"
    props = [
        # oneshot: ExecStartPost= runs once argv exits 0, which a RemainAfterExit=
        # exec unit's ExecStopPost= does only on a later stop (measured).
        "Type=oneshot", "RemainAfterExit=yes", "SuccessExitStatus=143",
        f"TimeoutStartSec={job.runtime}",
        f"Description=jobs-broker job in {job.repo}",
        f"WorkingDirectory={job.repo}", f"StandardOutput=append:{log}",
        # "+": the recorder and the sweep work on the real home, which the job's
        # ProtectHome=tmpfs would otherwise swallow. Their arguments are fixed here.
        f"ExecStartPost={sweep_cmd}",
        f"ExecStopPost=+{PYTHON} {RECORDER} {job.unit} {log}", f"ExecStopPost={sweep_cmd}",
        # ProtectHome=tmpfs already empties /run/user, hiding the user bus. A
        # TemporaryFileSystem= there would make the "+" recorder fail at NAMESPACE.
        *HARDENING,
        *(f"InaccessiblePaths=-{p}" for p in HIDDEN),
        f"BindPaths={job.repo}", *(f"BindReadOnlyPaths={p}" for p, _ in job.readonly),
        f"BindReadOnlyPaths={policy_path(home, job.unit)}",
        f"BindReadOnlyPaths=-{home}/.config/git",
    ]
    if job.profile == "nix":
        props += [f"BindPaths={home / NIX_CACHE}",
                  f"BindReadOnlyPaths=-{home}/.nix-memory-pressure",
                  "Slice=ram-heavy.slice", "OOMPolicy=kill"]
    return props


def start_argv(job: Job, home: Path) -> list:
    user = pwd.getpwuid(os.getuid()).pw_name
    env = [f"HOME={home}", f"USER={user}", f"LOGNAME={user}", "LANG=C.UTF-8",
           f"PATH=/etc/profiles/per-user/{user}/bin:{SYSTEM_BIN}"]
    cmd = ["systemd-run", "--user", "--no-block", f"--unit={job.unit}", "--expand-environment=no"]
    for prop in properties(job, home):
        cmd += ["-p", prop]
    return [*cmd, "--", f"{SYSTEM_BIN}/env", "-i", *env, f"{SYSTEM_BIN}/bash", "-c", WRAPPER,
            "jobs-broker", str(job.repo), job.ident,
            *(a for p, i in job.readonly for a in (str(p), i)),
            "--", SRT, "-s", str(policy_path(home, job.unit)), "--", *job.argv]


def _env(home: Path) -> dict:
    return {"HOME": str(home), "PATH": SYSTEM_BIN, "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def _home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _systemctl(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True,
                          timeout=timeout, env=_env(_home()), stdin=subprocess.DEVNULL)


def _show(unit: str) -> dict:
    done = _systemctl("show", f"--property={','.join(_SHOW)}", f"{unit}.service")
    if done.returncode != 0:
        raise RuntimeError(f"systemctl could not answer about {unit}.service: "
                           f"{done.stderr.strip()[:300]}")
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def _settled(snap: dict) -> bool:
    return snap.get("ActiveState") in ("failed", "inactive") or (
        snap.get("ActiveState") == "active" and snap.get("SubState") == "exited")


def tool_start(args: object, home: Path) -> str:
    job = validate_start(args, home)
    log = log_path(home, job.unit)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(log.parent / ".start.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _show(job.unit).get("LoadState") == "loaded":
            raise RuntimeError(f"{job.unit}.service already exists; stop it first or pick a new slug")
        log.unlink(missing_ok=True)
        os.close(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        policy = policy_path(home, job.unit)
        policy.unlink(missing_ok=True)
        with os.fdopen(os.open(policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600), "w") as fh:
            json.dump(job.policy, fh)
        write_baseline(job, home)
        (home / STATE / f"{job.unit}.moved").unlink(missing_ok=True)
        done = subprocess.run(start_argv(job, home), capture_output=True, text=True, timeout=120,
                              env=_env(home), stdin=subprocess.DEVNULL)
    finally:
        os.close(lock)
    if done.returncode != 0:
        raise RuntimeError(f"systemd-run could not start {job.unit}.service: "
                           f"{done.stderr.strip()[:500]}")
    slug = job.unit[len(PREFIX):]
    return (f"started {job.unit}.service in {job.repo} "
            f"(profile {job.profile}, stopped after {job.runtime}s)\nlog: {log}\n"
            f"outcome: status slug={slug} wait_sec<={MAX_WAIT_SEC}; "
            f"host side: wait-for unit {job.unit} --user")


def tool_status(args: object, home: Path) -> str:
    args = _fields(args, {"slug", "wait_sec"})
    unit = unit_name(args.get("slug"))
    deadline = time.monotonic() + _int(args, "wait_sec", 0, 0, MAX_WAIT_SEC)
    while True:
        snap = _show(unit)
        if snap.get("LoadState") != "loaded":
            return f"no job {unit}.service is loaded (never started, or stopped)"
        if _settled(snap) or time.monotonic() >= deadline:
            break
        time.sleep(2)
    try:
        moved = len((home / STATE / f"{unit}.moved").read_text().splitlines())
    except FileNotFoundError:
        moved = 0
    return (f"unit {unit}.service {snap.get('ActiveState')}/{snap.get('SubState')} "
            f"result={snap.get('Result')} status={snap.get('ExecMainStatus')} "
            f"started={snap.get('ExecMainStartTimestamp')} "
            f"exited={snap.get('ExecMainExitTimestamp')}\n"
            + (f"{moved} files that run later appeared in the repo and were moved out "
               f"after the job (listed in the log)\n" if moved else "")
            + f"{'settled' if _settled(snap) else 'running'}; log: {log_path(home, unit)}")


def tool_log(args: object, home: Path) -> str:
    args = _fields(args, {"slug", "lines"})
    unit = unit_name(args.get("slug"))
    lines = _int(args, "lines", DEFAULT_LOG_LINES, 1, MAX_LOG_LINES)
    log = log_path(home, unit)
    try:
        fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return f"no log at {log}"
    with os.fdopen(fd, "rb") as fh:
        fh.seek(max(0, fh.seek(0, os.SEEK_END) - MAX_LOG_BYTES))
        tail = fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    return f"{log}, last {len(tail)} lines:\n" + "\n".join(tail)


def tool_stop(args: object, home: Path) -> str:
    args = _fields(args, {"slug"})
    unit = unit_name(args.get("slug"))
    before = _show(unit)
    if before.get("LoadState") != "loaded":
        raise RuntimeError(f"no job {unit}.service is loaded")
    done = _systemctl("stop", f"{unit}.service", timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"could not stop {unit}.service: {done.stderr.strip()[:300]}")
    _systemctl("reset-failed", f"{unit}.service")
    return (f"stopped and unloaded {unit}.service (was {before.get('ActiveState')}/"
            f"{before.get('SubState')} result={before.get('Result')}); "
            f"log kept at {log_path(home, unit)}")


_SLUG = {"type": "string", "description": "Job name; the unit is claude-job-<slug>."}
TOOLS = [
    {"name": "start",
     "description": (
         "Starts a long job (nix build, VM test gate, long test suite) as a detached systemd "
         "user unit that outlives this session. argv runs in repo under the session's own "
         "sandbox policy, network allowlist included, with writes limited to repo; the rest of "
         "home and secrets are hidden, every git dir and agent/editor/shell config file is "
         "read-only (git commit fails), and it is killed after max_runtime_sec. Such files the "
         "job creates (a new .git, .claude/, .envrc, ...) are moved out of the repo when it "
         "ends. Profile nix also lets it write the nix cache and uses the ram-heavy slice. "
         "Read the outcome later with status (wait_sec blocks until it settles) and log."),
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string",
                  "description": "Top level of a git work tree under ~/Repos or ~/worktrees."},
         "argv": {"type": "array", "items": {"type": "string"},
                  "description": "Command and arguments, e.g. [\"nix\", \"build\", \".#x\"]."},
         "slug": _SLUG,
         "max_runtime_sec": {"type": "integer",
                             "description": f"Default {DEFAULT_RUNTIME_SEC}, max {MAX_RUNTIME_SEC}."},
         "profile": {"type": "string", "enum": list(PROFILES), "description": "Default repo."},
     }, "required": ["repo", "argv", "slug"], "additionalProperties": False}},
    {"name": "status",
     "description": "State, result and exit status of a job this broker started.",
     "inputSchema": {"type": "object", "properties": {
         "slug": _SLUG,
         "wait_sec": {"type": "integer",
                      "description": f"Block up to this long (max {MAX_WAIT_SEC}) for it to settle."},
     }, "required": ["slug"], "additionalProperties": False}},
    {"name": "log",
     "description": "Tail of a job's combined stdout/stderr, ending MARKER_OK or MARKER_FAILED rc=N.",
     "inputSchema": {"type": "object", "properties": {
         "slug": _SLUG,
         "lines": {"type": "integer",
                   "description": f"Default {DEFAULT_LOG_LINES}, max {MAX_LOG_LINES}."},
     }, "required": ["slug"], "additionalProperties": False}},
    {"name": "stop",
     "description": "Stops a job this broker started and unloads its unit, freeing the slug.",
     "inputSchema": {"type": "object", "properties": {"slug": _SLUG},
                     "required": ["slug"], "additionalProperties": False}},
]
_HANDLERS = {"start": tool_start, "status": tool_status, "log": tool_log, "stop": tool_stop}

SERVER_INFO = {"name": "jobs-broker", "version": "1.0.0"}
_write_lock = threading.Lock()


def _respond(msg_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    out["error" if error is not None else "result"] = error if error is not None else result
    with _write_lock:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _call(msg_id, params: dict) -> None:
    handler = _HANDLERS.get(params.get("name"))
    if handler is None:
        _respond(msg_id, error={"code": -32601, "message": f"Unknown tool: {params.get('name')}"})
        return
    try:
        text = handler(params.get("arguments") or {}, _home())
        _respond(msg_id, result={"content": [{"type": "text", "text": text}]})
    except Exception as e:
        _respond(msg_id, result={"content": [{"type": "text", "text": f"ERROR: {e}"}],
                                 "isError": True})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        _respond(msg_id, result={"protocolVersion": "2024-11-05",
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": SERVER_INFO})
    elif method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
    elif method == "tools/call" and msg_id is not None:
        threading.Thread(target=_call, args=(msg_id, msg.get("params") or {}),
                         daemon=True).start()
    elif msg_id is not None and not str(method).startswith("notifications/"):
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    if sys.argv[1:2] == ["sweep"]:
        unit = sys.argv[2] if len(sys.argv) == 3 else ""
        try:
            sweep(unit_name(unit.removeprefix(PREFIX)), _home())
        except RuntimeError as e:
            sys.exit(f"jobs-broker: sweep failed: {e}")
        return
    threads_before = threading.active_count()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            _handle(msg)
    deadline = time.monotonic() + 30
    while threading.active_count() > threads_before and time.monotonic() < deadline:
        time.sleep(0.05)


if __name__ == "__main__":
    main()

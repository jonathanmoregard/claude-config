#!/usr/bin/env python3
"""Jobs broker over stdio MCP: long jobs as hardened systemd user units.

The caller chooses the command. The unit properties built here are the
boundary; the only caller inputs they take are the repo directory, the slug,
the runtime bound and the profile name.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from broker_paths import canonical  # noqa: E402
RECORDER = HERE.parents[1] / "scripts" / "record-user-unit-failure"
SYSTEM_BIN = "/run/current-system/sw/bin"
PYTHON = f"{SYSTEM_BIN}/python3"
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
_SHOW = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
         "ExecMainStartTimestamp", "ExecMainExitTimestamp")

HARDENING = (
    "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateUsers=self",
    "NoNewPrivileges=yes", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
    "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM", "PrivatePIDs=yes",
    "RestrictNamespaces=yes", "PrivateIPC=yes", "PrivateDevices=yes",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes",
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


class Target(NamedTuple):
    repo: Path
    writable: list
    readonly: list


class Job(NamedTuple):
    unit: str
    target: Target
    argv: list
    runtime: int
    profile: str


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


def _dot_git_entries(repo: Path) -> list:
    found, pending, seen = [], [repo], 0
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_SCAN_ENTRIES:
                        raise RuntimeError(f"{repo} has over {MAX_SCAN_ENTRIES} entries to scan")
                    if entry.name == ".git":
                        if entry.is_symlink():
                            raise RuntimeError(f"{entry.path} is a symlink")
                        found.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        except OSError as e:
            raise RuntimeError(f"cannot scan {repo} for git dirs: {e}") from None
    return found


# Every git dir a job could reach is bound read-only: the repo's own, its
# common dir, and every nested repo's or submodule's. Each is a mount point,
# and so is every directory between the repo and it, so none can be renamed
# away and replaced by one the job wrote.
def _git_binds(repo: Path, home: Path) -> list:
    readonly = []
    for entry in _dot_git_entries(repo):
        if entry.is_dir():
            gitdir = entry
        else:
            readonly.append(canonical(str(entry), home, "file", git_ok=True))
            gitdir = _gitdir_pointer(entry)
            if gitdir is None:
                continue
        for path in {gitdir, _common_dir(gitdir)}:
            readonly.append(canonical(str(path), home, git_ok=True))
    unique = sorted(set(readonly))
    return [(p, i) for p, i in unique if not any(q in p.parents for q, _ in unique)]


def confine_repo(raw: object, home: Path) -> Target:
    repo = canonical(raw, home)
    readonly = _git_binds(repo[0], home)
    between = {a for p, _ in readonly for a in p.parents if repo[0] in a.parents}
    writable = [repo, *(canonical(str(a), home) for a in sorted(between))]
    return Target(repo[0], writable, readonly)


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
    return Job(unit, confine_repo(args.get("repo"), home), list(argv), runtime, profile)


def properties(job: Job, home: Path) -> list:
    log = log_path(home, job.unit)
    props = [
        "Type=exec", "RemainAfterExit=yes", "SuccessExitStatus=143",
        f"RuntimeMaxSec={job.runtime}",
        f"Description=jobs-broker job in {job.target.repo}",
        f"WorkingDirectory={job.target.repo}", f"StandardOutput=append:{log}",
        # "+": the recorder writes the real ~/.local/state, which the job's
        # ProtectHome=tmpfs would otherwise swallow. Its arguments are fixed here.
        f"ExecStopPost=+{PYTHON} {RECORDER} {job.unit} {log}",
        # ProtectHome=tmpfs already empties /run/user, hiding the user bus. A
        # TemporaryFileSystem= there would make the "+" recorder fail at NAMESPACE.
        *HARDENING,
        *(f"InaccessiblePaths=-{p}" for p in HIDDEN),
        *(f"BindPaths={path}" for path, _ in job.target.writable),
        *(f"BindReadOnlyPaths={path}" for path, _ in job.target.readonly),
        f"BindReadOnlyPaths=-{home}/.config/git",
    ]
    if job.profile == "nix":
        props += [f"BindPaths={home}/.cache/nix", f"BindReadOnlyPaths=-{home}/.nix-memory-pressure",
                  "Slice=ram-heavy.slice", "OOMPolicy=kill"]
    return props


def start_argv(job: Job, home: Path) -> list:
    user = pwd.getpwuid(os.getuid()).pw_name
    env = [f"HOME={home}", f"USER={user}", f"LOGNAME={user}", "LANG=C.UTF-8",
           f"PATH=/etc/profiles/per-user/{user}/bin:{SYSTEM_BIN}"]
    cmd = ["systemd-run", "--user", f"--unit={job.unit}", "--expand-environment=no"]
    for prop in properties(job, home):
        cmd += ["-p", prop]
    cmd += ["--", f"{SYSTEM_BIN}/env", "-i", *env, f"{SYSTEM_BIN}/bash", "-c", WRAPPER,
            "jobs-broker"]
    for path, ident in [*job.target.writable, *job.target.readonly]:
        cmd += [str(path), ident]
    return [*cmd, "--", *job.argv]


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
        done = subprocess.run(start_argv(job, home), capture_output=True, text=True, timeout=120,
                              env=_env(home), stdin=subprocess.DEVNULL)
    finally:
        os.close(lock)
    if done.returncode != 0:
        raise RuntimeError(f"systemd-run could not start {job.unit}.service: "
                           f"{done.stderr.strip()[:500]}")
    slug = job.unit[len(PREFIX):]
    return (f"started {job.unit}.service in {job.target.repo} "
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
    return (f"unit {unit}.service {snap.get('ActiveState')}/{snap.get('SubState')} "
            f"result={snap.get('Result')} status={snap.get('ExecMainStatus')} "
            f"started={snap.get('ExecMainStartTimestamp')} "
            f"exited={snap.get('ExecMainExitTimestamp')}\n"
            f"{'settled' if _settled(snap) else 'running'}; log: {log_path(home, unit)}")


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
         "Starts a long job (nix build, VM test gate, long test suite) as a detached, hardened "
         "systemd user unit that outlives this session. It runs argv in repo with: repo "
         "writable, the rest of home hidden, no network (nix builds still fetch through the "
         "nix daemon), secrets hidden, every git dir read-only (git commit fails), killed after "
         "max_runtime_sec. Profile nix also binds the nix cache and uses the ram-heavy slice. "
         "Read the outcome later with status (wait_sec blocks until it settles) and log."),
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string", "description": "Absolute dir under ~/Repos or ~/worktrees."},
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

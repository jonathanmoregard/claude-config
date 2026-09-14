"""Job directories, state files, launch and inspection (contract §Jobs, §CLI).

A job is ``jobs/<job_id>/`` in the state dir: ``meta.json``, ``prompt.md``,
``events.jsonl``, ``stderr.log``, ``status``, ``exit_code``, ``result.txt``
and ``result.json``. ``create_job`` writes the first three files with status
``queued``; ``launch`` starts ``runner.py`` detached (``systemd-run --user``,
falling back to a ``setsid`` ``Popen``); everything else here reads what the
runner wrote. All writes are atomic.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import adapters
from .config import atomic_write_json, atomic_write_text, read_json
from .sanitize import clean

RUNNER = Path(__file__).resolve().parent / "runner.py"
ACTIVE = ("queued", "running")
QUEUED_GRACE_S = 30          # queued with no runner pid for longer → the launch never happened
POLL_S = 0.5
DEFAULT_LIST = 20

META_KEYS = ("job_id", "name", "created_at", "provider", "model", "effort", "tier", "host", "cwd", "sandbox",
             "schema", "timeout_s", "stall_min", "bare", "need", "argv", "launcher", "unit", "runner_pid",
             "child_pid", "thread_id", "session_id", "forced", "retry_of", "retried_as", "parent_job",
             "started_at", "finished_at", "exit_code", "error_class")


class JobNotFound(LookupError):
    pass


@dataclass
class JobSpec:
    provider: str
    model: str
    effort: str | None
    tier: str
    cwd: str
    sandbox: str
    argv: list[str]
    prompt: str
    timeout_s: float
    stall_min: float
    bare: bool = True
    schema: str | None = None
    host: str | None = None
    name: str | None = None
    need: list[str] = field(default_factory=list)
    forced: bool = False
    retry_of: str | None = None
    parent_job: str | None = None


# --- paths and files ---------------------------------------------------------------

def jobs_root(state: Path) -> Path:
    d = state / "jobs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def job_dir(state: Path, job_id: str) -> Path:
    if not job_id or "/" in job_id or job_id.startswith("."):
        raise JobNotFound(job_id)
    d = jobs_root(state) / job_id
    if not d.is_dir():
        raise JobNotFound(job_id)
    return d


def new_job_id(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)


def read_meta(d: Path) -> dict[str, Any]:
    meta = read_json(d / "meta.json")
    if not isinstance(meta, dict):
        raise JobNotFound(d.name)
    return meta


def write_meta(d: Path, meta: dict[str, Any]) -> None:
    atomic_write_json(d / "meta.json", meta)


def update_meta(d: Path, **fields: Any) -> dict[str, Any]:
    meta = read_meta(d)
    meta.update(fields)
    write_meta(d, meta)
    return meta


def write_status(d: Path, status: str) -> None:
    atomic_write_text(d / "status", status + "\n")


def raw_status(d: Path) -> str:
    try:
        return (d / "status").read_text(encoding="utf-8").strip() or "queued"
    except OSError:
        return "queued"


def is_terminal(status: str) -> bool:
    return status not in ACTIVE


def pid_alive(pid: int | None) -> bool:
    """True for a live process; a zombie (exited, not yet reaped by its parent) counts as dead."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def status_of(d: Path, meta: dict[str, Any] | None = None, now: float | None = None) -> str:
    """Status, repaired when the runner died without writing a terminal state."""
    s = raw_status(d)
    if is_terminal(s):
        return s
    meta = meta or read_meta(d)
    t = time.time() if now is None else now
    pid = meta.get("runner_pid")
    dead = (pid is not None and not pid_alive(pid)) or \
           (pid is None and t - float(meta.get("created_at") or t) > QUEUED_GRACE_S)
    if dead:
        s = "failed:error"
        write_status(d, s)
        update_meta(d, error_class="error", finished_at=int(t),
                    exit_code=meta.get("exit_code"), runner_note="runner exited without a terminal status")
    return s


# --- create / launch ---------------------------------------------------------------

def create_job(state: Path, spec: JobSpec, *, now: float) -> tuple[str, Path]:
    job_id = new_job_id(now)
    d = jobs_root(state) / job_id
    d.mkdir(mode=0o700)
    atomic_write_text(d / "prompt.md", spec.prompt)
    meta: dict[str, Any] = {k: None for k in META_KEYS}
    meta.update({
        # Sub-second: job ids carry one-second resolution plus random hex, so a
        # retry created in the same second as its parent would otherwise sort
        # randomly against it (seen as an intermittent test failure, 2026-09-14).
        "job_id": job_id, "name": spec.name, "created_at": round(float(now), 6), "provider": spec.provider,
        "model": spec.model, "effort": spec.effort, "tier": spec.tier, "host": spec.host, "cwd": spec.cwd,
        "sandbox": spec.sandbox, "schema": spec.schema, "timeout_s": spec.timeout_s, "stall_min": spec.stall_min,
        "bare": spec.bare, "need": list(spec.need), "argv": list(spec.argv), "forced": spec.forced,
        "retry_of": spec.retry_of, "parent_job": spec.parent_job,
    })
    write_meta(d, meta)
    write_status(d, "queued")
    return job_id, d


def _router_env(env: dict[str, str]) -> list[str]:
    out = [f"--setenv=PATH={env.get('PATH', os.defpath)}", f"--setenv=HOME={env.get('HOME', os.path.expanduser('~'))}"]
    out += [f"--setenv={k}={v}" for k, v in sorted(env.items()) if k.startswith("AI_ROUTER_")]
    return out


def launch(d: Path, *, env: dict[str, str] | None = None, launcher: str = "auto",
           run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Start runner.py for job dir ``d`` detached; returns {launcher, unit, runner_pid}."""
    e = dict(os.environ if env is None else env)
    meta = read_meta(d)
    python = sys.executable
    cmd = [python, str(RUNNER), str(d)]
    unit = f"ai-router-{d.name}"
    if launcher == "auto" and shutil.which("systemd-run", path=e.get("PATH")):
        sd = [shutil.which("systemd-run", path=e.get("PATH")), "--user", "--collect", "--quiet", f"--unit={unit}",
              f"--property=WorkingDirectory={meta['cwd']}", *_router_env(e), *cmd]
        try:
            rc = run(sd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = -1
        if rc == 0:
            update_meta(d, launcher="systemd-run", unit=unit)
            return {"launcher": "systemd-run", "unit": unit, "runner_pid": None}
    log = open(d / "runner.log", "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=meta["cwd"], env=e, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    finally:
        log.close()
    update_meta(d, launcher="setsid", unit=None, runner_pid=proc.pid)
    return {"launcher": "setsid", "unit": None, "runner_pid": proc.pid}


def wait_runner_pid(d: Path, timeout_s: float = 3.0, sleep: Callable[[float], None] = time.sleep) -> int | None:
    deadline = time.monotonic() + timeout_s
    while True:
        pid = read_meta(d).get("runner_pid")
        if pid or time.monotonic() >= deadline:
            return pid
        sleep(0.05)


# --- wait / status / list ---------------------------------------------------------------

def follow(state: Path, job_id: str) -> str:
    """Newest job in the retry chain starting at job_id."""
    seen = set()
    while job_id not in seen:
        seen.add(job_id)
        nxt = read_meta(job_dir(state, job_id)).get("retried_as")
        if not nxt:
            break
        job_id = nxt
    return job_id


def wait_for(state: Path, job_id: str, *, timeout_s: float, poll_s: float = POLL_S,
             sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:
    """Block until the job (following retries) is terminal → (final_job_id, status). TimeoutError otherwise."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = follow(state, job_id)
        d = job_dir(state, current)
        s = status_of(d)
        if is_terminal(s):
            # a quota failure may still be spawning its retry: give the runner a moment
            if s == "failed:quota" and read_meta(d).get("retry_of") is None and time.monotonic() < deadline:
                sleep(poll_s)
                if follow(state, current) != current:
                    continue
            return current, s
        if time.monotonic() >= deadline:
            raise TimeoutError(current)
        sleep(poll_s)


def status_obj(state: Path, job_id: str, now: float | None = None) -> dict[str, Any]:
    d = job_dir(state, job_id)
    meta = read_meta(d)
    t = time.time() if now is None else now
    return {"job_id": job_id, "status": status_of(d, meta, t), "provider": meta.get("provider"),
            "model": meta.get("model"), "created_at": meta.get("created_at"),
            "age_s": int(t - float(meta.get("created_at") or t)), "name": meta.get("name"),
            "retry_of": meta.get("retry_of"), "retried_as": meta.get("retried_as")}


def list_jobs(state: Path, *, all_jobs: bool = False, now: float | None = None) -> list[dict[str, Any]]:
    """Newest first, by `created_at` (sub-second) then id; never by the random id suffix alone."""
    entries = []
    for p in jobs_root(state).iterdir():
        if not (p / "meta.json").exists():
            continue
        try:
            created = float(read_meta(p).get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        entries.append((created, p.name))
    entries.sort(reverse=True)
    ids = [name for _, name in entries]
    if not all_jobs:
        ids = ids[:DEFAULT_LIST]
    out = []
    for i in ids:
        try:
            out.append(status_obj(state, i, now))
        except JobNotFound:
            continue
    return out


# --- inspection ---------------------------------------------------------------

def peek(d: Path) -> str | None:
    meta = read_meta(d)
    events = adapters.read_events(d / "events.jsonl")
    text = adapters.latest_agent_text(meta["provider"], events)
    if text is None:
        try:
            text = clean((d / "result.txt").read_text(encoding="utf-8")).strip() or None
        except OSError:
            text = None
    return text


def log_lines(d: Path, tail: int) -> list[str]:
    meta = read_meta(d)
    lines = [adapters.summarize_event(meta["provider"], e) for e in adapters.read_events(d / "events.jsonl")]
    try:
        err = (d / "stderr.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        err = ""
    lines += ["stderr: " + clean(l) for l in err.splitlines() if l.strip()]
    return lines[-tail:] if tail > 0 else lines


def result_obj(d: Path, now: float | None = None) -> dict[str, Any]:
    meta = read_meta(d)
    status = status_of(d, meta, now)
    try:
        text: str | None = clean((d / "result.txt").read_text(encoding="utf-8")).rstrip("\n")
    except OSError:
        text = None
    started, finished = meta.get("started_at"), meta.get("finished_at")
    events = adapters.read_events(d / "events.jsonl")
    obj: dict[str, Any] = {
        "ok": status == "done", "job_id": d.name, "status": status, "exit_code": meta.get("exit_code"),
        "provider": meta["provider"], "model": meta["model"], "result_text": text,
        "result_json": read_json(d / "result.json"), "usage": adapters.usage(meta["provider"], events),
        "duration_s": (int(finished) - int(started)) if started and finished else None,
        "error_class": meta.get("error_class"),
    }
    key = "thread_id" if meta["provider"] == "codex" else "session_id"
    obj[key] = meta.get(key)
    if meta.get("retried_as"):
        obj["retried_as"] = meta["retried_as"]
    return obj


def cancel(state: Path, job_id: str, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    d = job_dir(state, job_id)
    meta = read_meta(d)
    status = status_of(d, meta)
    if is_terminal(status):
        return {"job_id": job_id, "status": status, "cancelled": False}
    if meta.get("launcher") == "systemd-run" and meta.get("unit"):
        run(["systemctl", "--user", "stop", meta["unit"]], stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    for pid in (meta.get("child_pid"), meta.get("runner_pid")):
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM) if pid == meta.get("child_pid") else os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue  # already gone, or not ours to signal
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (pid_alive(meta.get("child_pid")) or pid_alive(meta.get("runner_pid"))):
        time.sleep(0.1)
    for pid in (meta.get("child_pid"), meta.get("runner_pid")):
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue  # already gone, or not ours to signal
    write_status(d, "cancelled")
    update_meta(d, finished_at=int(time.time()), error_class="cancelled")
    return {"job_id": job_id, "status": "cancelled", "cancelled": True}

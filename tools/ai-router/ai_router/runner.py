"""Detached job runner (contract §Jobs: runner.py).

``python3 runner.py <job_dir>`` — started by ``jobs.launch`` under
``systemd-run --user`` or ``setsid``. Records its pid, spawns the provider
CLI with stdout → ``events.jsonl`` (one flushed line per event) and stderr →
``stderr.log``, watches for stalls (no stdout bytes for ``stall_min``) and
the wall clock (``timeout_s``), classifies the exit via ``adapters.classify``
and writes ``result.txt`` / ``result.json`` / ``exit_code`` / ``status``.

On a ``quota`` failure it writes ``quota/<provider>.rejected.json`` and, for
tier-picked jobs that are not themselves retries, picks the next eligible
model in the same tier and launches exactly one retry. Explicitly pinned
models never change model/provider. ``rate_limit`` failures do not reject the
provider or trigger a cross-model retry.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_router import adapters, jobs, quota  # noqa: E402
from ai_router.config import atomic_write_text, load_config, read_json  # noqa: E402
from ai_router.selection import pick  # noqa: E402

KILL_GRACE_S = 10.0
TICK_S = 0.25


class Pump:
    """Copy child stdout to events.jsonl line by line; remember when bytes last arrived."""

    def __init__(self, stream: Any, path: Path, on_event: Any) -> None:
        self.stream = stream
        self.path = path
        self.on_event = on_event
        self.last = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with open(self.path, "ab") as fh:
            for raw in self.stream:
                self.last = time.monotonic()
                fh.write(raw if raw.endswith(b"\n") else raw + b"\n")
                fh.flush()
                self.on_event(raw)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> None:
        self.thread.join(timeout)


def _kill(child: subprocess.Popen[bytes]) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 5.0)):
        if child.poll() is not None:
            return
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            child.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _write_results(d: Path, provider: str, events: list[dict[str, Any]], schema: str | None) -> str | None:
    text = adapters.final_message(provider, events, d / "last_message.md" if provider == "codex" else None)
    if text is None:
        return None
    atomic_write_text(d / "result.txt", text + "\n")
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if parsed is not None and (schema or isinstance(parsed, (dict, list))):
        atomic_write_text(d / "result.json", json.dumps(parsed, indent=1, sort_keys=True) + "\n")
    return text


def _retry_after_quota(state: Path, d: Path, meta: dict[str, Any], diag: str, now: float) -> None:
    if meta.get("dev_gates"):
        jobs.update_meta(d, retry_note="no retry: --dev-gates must not fall back to a different sandbox")
        return
    cfg = load_config()
    provider, model_id = meta["provider"], meta["model"]
    model = cfg.model(model_id)
    family = model.family if model else model_id
    env = dict(os.environ)
    snapshots = quota.all_snapshots(cfg, state, now=now, codex_bin=adapters.resolve_binary("codex", env),
                                    home=Path(env.get("HOME") or Path.home()))
    until = quota.blocking_reset(snapshots.get(provider) or {}, model_id, family)
    if until is not None and until <= now:
        until = None
    rejected = quota.mark_rejected(state, provider, until=until, reason=diag.strip()[:300] or "quota",
                                   source=f"job:{d.name}", now=now)
    if meta.get("model_pinned"):
        jobs.update_meta(d, retry_note="no retry: model pinned (--model); retry the same model later")
        return
    if meta.get("retry_of"):
        jobs.update_meta(d, retry_note="already a retry; not retried again")
        return
    if provider in snapshots:
        snapshots[provider].update(status="rejected", rejected_until=rejected["until"], reason=rejected["reason"])
    choice = pick(cfg, snapshots, meta["tier"], need=tuple(meta.get("need") or ()),
                  provider=meta.get("provider_constraint"), host=meta.get("host"), now=now)
    if not choice["ok"]:
        jobs.update_meta(d, retry_note=f"no retry: {choice['reason']}")
        return
    binary = adapters.resolve_binary(choice["provider"], env)
    if binary is None:
        jobs.update_meta(d, retry_note=f"no retry: {choice['provider']} binary not found")
        return
    effort = meta.get("effort") if meta.get("effort") in cfg.efforts.get(choice["provider"], ()) else choice["effort"]
    prompt = (d / "prompt.md").read_text(encoding="utf-8")
    new_id, new_dir = jobs.create_job(state, jobs.JobSpec(
        provider=choice["provider"], model=choice["model"], effort=effort, tier=choice["tier"], cwd=meta["cwd"],
        sandbox=meta["sandbox"], prompt=prompt, timeout_s=meta["timeout_s"], stall_min=meta["stall_min"],
        bare=bool(meta.get("bare", True)), schema=meta.get("schema"), host=meta.get("host"), name=meta.get("name"),
        need=list(meta.get("need") or []), forced=False, retry_of=d.name, parent_job=meta.get("parent_job"),
        provider_constraint=meta.get("provider_constraint"), argv=[]), now=now)
    argv = adapters.build_argv(choice["provider"], binary, model=choice["model"], effort=effort,
                               sandbox=meta["sandbox"], cwd=meta["cwd"], schema=meta.get("schema"),
                               bare=bool(meta.get("bare", True)), job_dir=new_dir)
    jobs.update_meta(new_dir, argv=argv)
    jobs.update_meta(d, retried_as=new_id)
    # the retry follows the parent's launch path (setsid stays setsid; systemd stays auto)
    jobs.launch(new_dir, env=env, launcher="setsid" if meta.get("launcher") == "setsid" else "auto")


def run(d: Path) -> int:
    state = d.parent.parent
    meta = jobs.update_meta(d, runner_pid=os.getpid(), started_at=int(time.time()))
    jobs.write_status(d, "running")
    provider = meta["provider"]
    prompt = (d / "prompt.md").read_text(encoding="utf-8")
    try:
        argv = adapters.materialize(meta["argv"], prompt)
        adapters.refuse_dangerous(argv)
    except adapters.DangerousFlag as exc:
        atomic_write_text(d / "stderr.log", f"{exc}\n")
        jobs.write_status(d, "failed:error")
        jobs.update_meta(d, finished_at=int(time.time()), error_class="error", exit_code=None)
        return 2

    stderr_f = open(d / "stderr.log", "ab")
    try:
        child = subprocess.Popen(argv, cwd=meta["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=stderr_f, start_new_session=True)
    except OSError as exc:
        stderr_f.write(f"launch failed: {exc}\n".encode())
        stderr_f.close()
        jobs.write_status(d, "failed:error")
        jobs.update_meta(d, finished_at=int(time.time()), error_class="error", exit_code=None)
        return 2
    jobs.update_meta(d, child_pid=child.pid)

    conv: dict[str, str | None] = {"id": None}

    def on_event(raw: bytes) -> None:
        if conv["id"] is not None:
            return
        ev = adapters.parse_event_line(raw.decode("utf-8", "replace"))
        cid = adapters.conversation_id(provider, [ev]) if ev else None
        if cid:
            conv["id"] = cid
            jobs.update_meta(d, **{"thread_id" if provider == "codex" else "session_id": cid})

    assert child.stdout is not None
    pump = Pump(child.stdout, d / "events.jsonl", on_event)
    pump.start()
    outcome: dict[str, str | None] = {"forced": None}

    def on_term(signum: int, frame: Any) -> None:
        outcome["forced"] = "cancelled"
        _kill(child)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    started = time.monotonic()
    stall_s = float(meta["stall_min"]) * 60
    timeout_s = float(meta["timeout_s"])
    while child.poll() is None:
        time.sleep(TICK_S)
        if outcome["forced"]:
            break
        if time.monotonic() - pump.last > stall_s:
            outcome["forced"] = "stalled"
            _kill(child)
        elif time.monotonic() - started > timeout_s:
            outcome["forced"] = "timeout"
            _kill(child)
    child.wait()
    pump.join(5.0)
    stderr_f.close()

    exit_code = child.returncode
    events = adapters.read_events(d / "events.jsonl")
    stderr_text = (d / "stderr.log").read_text(encoding="utf-8", errors="replace")
    text = _write_results(d, provider, events, meta.get("schema"))
    atomic_write_text(d / "exit_code", f"{exit_code}\n")

    if outcome["forced"]:
        status, error_class = outcome["forced"], outcome["forced"]
    else:
        cls = adapters.classify(provider, exit_code, events, stderr_text,
                                d / "last_message.md" if provider == "codex" else None)
        status = "done" if cls == "done" else f"failed:{cls}"
        error_class = None if cls == "done" else cls
    fields: dict[str, Any] = {"finished_at": int(time.time()), "exit_code": exit_code, "error_class": error_class}
    if conv["id"] is None:
        cid = adapters.conversation_id(provider, events)
        if cid:
            fields["thread_id" if provider == "codex" else "session_id"] = cid
    jobs.update_meta(d, **fields)

    # The retry is linked (retried_as / retry_note) BEFORE the terminal status
    # lands, so a waiter that sees `failed:quota` sees the retry with it. The
    # other order let `delegate --wait` return failed:quota while the retry
    # was still being decided (quota re-probe up to 8 s) — observed as an
    # intermittent test failure under load, 2026-09-14.
    if status == "failed:quota":
        diag = stderr_text or (text or "")
        for e in events:
            if e.get("type") in ("error", "turn.failed", "result"):
                diag = json.dumps(e) + "\n" + diag
        try:
            _retry_after_quota(state, d, jobs.read_meta(d), diag, time.time())
        except Exception as exc:  # noqa: BLE001 — the retry is best effort; the failure is already recorded
            jobs.update_meta(d, retry_note=f"retry failed: {type(exc).__name__}: {exc}")
    jobs.write_status(d, status)
    return 0 if status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: runner.py <job_dir>\n")
        return 2
    d = Path(args[0]).resolve()
    if not (d / "meta.json").exists():
        sys.stderr.write(f"runner: no meta.json in {d}\n")
        return 2
    return run(d)


if __name__ == "__main__":
    sys.exit(main())

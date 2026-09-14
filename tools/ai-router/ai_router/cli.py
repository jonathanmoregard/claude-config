"""The ``ai-router`` command line (contract §CLI).

``main(argv) -> int``. Every subcommand accepts ``--json`` (one object on
stdout) and otherwise prints a short human line. Errors are
``{"ok": false, "error_class", "message"}`` on stdout under ``--json``, a
message on stderr otherwise. Exit codes: 0 ok · 2 usage/setup · 3 quota ·
4 rejected/unavailable · 5 stalled/timeout · 6 unparseable · 7 job not
found / not finished · 1 other failure (job error, cancelled).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import VERSION, adapters, jobs, quota
from .config import PROVIDERS, SANDBOXES, TIERS, Config, ConfigError, load_config, state_dir
from .sanitize import clean
from .selection import detect_host, pick

EXIT_BY_CLASS = {"ok": 0, "usage": 2, "setup": 2, "quota": 3, "rejected": 4, "stalled": 5, "timeout": 5,
                 "overloaded": 5, "unparseable": 6, "not_found": 7, "not_finished": 7, "error": 1, "cancelled": 1}


def exit_for_status(status: str) -> int:
    if status == "done":
        return 0
    cls = status.split(":", 1)[1] if status.startswith("failed:") else status
    return EXIT_BY_CLASS.get(cls, 1)


class CliError(Exception):
    def __init__(self, error_class: str, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.extra = extra or {}


class Out:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def emit(self, obj: dict[str, Any], human: str) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps(obj, indent=1, sort_keys=True) + "\n")
        else:
            sys.stdout.write(human + "\n")
        sys.stdout.flush()

    def error(self, err: CliError) -> int:
        code = EXIT_BY_CLASS.get(err.error_class, 1)
        if self.as_json:
            sys.stdout.write(json.dumps({"ok": False, "error_class": err.error_class, "message": err.message,
                                         **err.extra}, indent=1, sort_keys=True) + "\n")
        else:
            sys.stderr.write(f"ai-router: {err.message}\n")
        return code


# --- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-router", description="Cross-provider subagent routing (Claude ⇄ Codex).")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    sp = cmd("quota", "usage-cap snapshots for both providers")
    sp.add_argument("--fresh", action="store_true")
    sp.add_argument("--provider", choices=PROVIDERS)

    sp = cmd("pick", "choose a model for a tier")
    sp.add_argument("--tier", choices=TIERS, required=True)
    sp.add_argument("--need", action="append", default=[], metavar="CAP")
    sp.add_argument("--provider", choices=PROVIDERS)
    sp.add_argument("--host", choices=PROVIDERS)
    sp.add_argument("--allow-downgrade", action="store_true")
    sp.add_argument("--fresh", action="store_true")

    sp = cmd("delegate", "start a headless job on the picked model")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--tier", choices=TIERS)
    g.add_argument("--model", metavar="M")
    sp.add_argument("--prompt-file", required=True, metavar="F")
    sp.add_argument("--cwd", default=None, metavar="D")
    sp.add_argument("--sandbox", choices=SANDBOXES, default="read-only")
    sp.add_argument("--schema", metavar="S.json")
    sp.add_argument("--timeout-s", type=float, default=None)
    sp.add_argument("--effort", metavar="E")
    sp.add_argument("--wait", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-bare", action="store_true")
    sp.add_argument("--host", choices=PROVIDERS)
    sp.add_argument("--provider", choices=PROVIDERS, help="constrain a --tier pick to one provider")
    sp.add_argument("--name", metavar="NAME")
    sp.add_argument("--allow-downgrade", action="store_true")
    sp.add_argument("--need", action="append", default=[], metavar="CAP")
    sp.add_argument("--fresh", action="store_true")

    sp = cmd("wait", "block until a job is terminal")
    sp.add_argument("job_id")
    sp.add_argument("--timeout-s", type=float, default=None)

    sp = cmd("status", "one job or all jobs")
    sp.add_argument("job_id", nargs="?")

    sp = cmd("peek", "newest agent message")
    sp.add_argument("job_id")

    sp = cmd("log", "summarised events + stderr")
    sp.add_argument("job_id")
    sp.add_argument("--tail", type=int, default=50)

    sp = cmd("result", "final result of a finished job")
    sp.add_argument("job_id")

    sp = cmd("send", "resume a finished job with a new prompt")
    sp.add_argument("job_id")
    sp.add_argument("--prompt-file", required=True, metavar="F")
    sp.add_argument("--wait", action="store_true")
    sp.add_argument("--timeout-s", type=float, default=None)

    sp = cmd("cancel", "stop a running job")
    sp.add_argument("job_id")

    sp = cmd("list", "recent jobs")
    sp.add_argument("--all", action="store_true")

    cmd("version", "print version")
    return p


# --- helpers ---------------------------------------------------------------

class Ctx:
    def __init__(self, cfg: Config, state: Path, env: dict[str, str], now: float) -> None:
        self.cfg, self.state, self.env, self.now = cfg, state, env, now

    def snapshots(self, fresh: bool, provider: str | None = None) -> dict[str, dict[str, Any]]:
        return quota.all_snapshots(self.cfg, self.state, now=self.now, fresh=fresh, provider=provider,
                                   codex_bin=adapters.resolve_binary("codex", self.env),
                                   home=Path(self.env.get("HOME") or os.path.expanduser("~")))

    def job(self, job_id: str) -> Path:
        try:
            return jobs.job_dir(self.state, job_id)
        except jobs.JobNotFound:
            raise CliError("not_found", f"job not found: {job_id}") from None


def _read_prompt(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError("usage", f"cannot read prompt file: {exc}") from None
    if not text.strip():
        raise CliError("usage", f"prompt file is empty: {path}")
    return text


def _abs_dir(path: str | None) -> str:
    d = os.path.abspath(path or os.getcwd())
    if not os.path.isdir(d):
        raise CliError("usage", f"cwd is not a directory: {d}")
    return d


def _check_effort(ctx: Ctx, provider: str, effort: str | None) -> None:
    if effort is not None and effort not in ctx.cfg.efforts.get(provider, ()):
        raise CliError("usage", f"effort {effort!r} not valid for {provider}: "
                                f"{'|'.join(ctx.cfg.efforts.get(provider, ()))}")


def _human_quota(snap: dict[str, Any]) -> str:
    parts = [f"{snap['provider']}: {snap['status']}"]
    if snap.get("source") != "none":
        parts.append(f"src={snap['source']} age={snap.get('age_s')}s" + (" STALE" if snap.get("stale") else ""))
    for b in snap.get("buckets", []):
        if b.get("applies_to"):
            parts.append(f"{b['id']}={b['used_pct']:g}%")
    if snap.get("rejected_until"):
        parts.append(f"rejected_until={snap['rejected_until']}")
    if snap.get("error"):
        parts.append(f"error={snap['error']}")
    return " ".join(parts)


def _start_job(ctx: Ctx, spec: jobs.JobSpec, launcher: str) -> dict[str, Any]:
    job_id, d = jobs.create_job(ctx.state, spec, now=ctx.now)
    info = jobs.launch(d, env=ctx.env, launcher=launcher)
    runner_pid = info["runner_pid"] or jobs.wait_runner_pid(d)
    return {"ok": True, "job_id": job_id, "provider": spec.provider, "model": spec.model, "effort": spec.effort,
            "job_dir": str(d), "events": str(d / "events.jsonl"), "result_path": str(d / "result.txt"),
            "status_path": str(d / "status"), "launcher": info["launcher"], "unit": info["unit"],
            "runner_pid": runner_pid}


def _wait_and_report(ctx: Ctx, out: Out, job_id: str, timeout_s: float) -> int:
    try:
        final_id, _ = jobs.wait_for(ctx.state, job_id, timeout_s=timeout_s)
    except TimeoutError as exc:
        raise CliError("timeout", f"job {exc} still running after {timeout_s:g}s", {"job_id": str(exc)}) from None
    res = jobs.result_obj(ctx.job(final_id))
    out.emit(res, _human_result(res))
    return exit_for_status(res["status"])


def _human_result(res: dict[str, Any]) -> str:
    head = f"{res['job_id']} {res['status']} {res['provider']}/{res['model']}"
    if res.get("usage") and res["usage"].get("total_cost_usd") is not None:
        head += f" cost=${res['usage']['total_cost_usd']:.4f}"
    if res.get("duration_s") is not None:
        head += f" {res['duration_s']}s"
    body = res.get("result_text") or ""
    return head + ("\n" + body if body else "")


# --- commands ---------------------------------------------------------------

def cmd_quota(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    snaps = ctx.snapshots(args.fresh, args.provider)
    out.emit({"ok": True, "observed_at": int(ctx.now), "providers": snaps},
             "\n".join(_human_quota(s) for s in snaps.values()))
    return 0


def cmd_pick(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    host = args.host or detect_host(ctx.env)
    r = pick(ctx.cfg, ctx.snapshots(args.fresh), args.tier, need=tuple(args.need), provider=args.provider,
             host=host, allow_downgrade=args.allow_downgrade, now=ctx.now)
    if not r["ok"]:
        raise CliError("quota", r["reason"], {"candidates": r["candidates"]})
    out.emit(r, f"{r['provider']}/{r['model']} effort={r['effort']} tier={r['tier']} native={str(r['native']).lower()}"
                f" headroom={r['headroom']} — {r['reason']}")
    return 0


def _choose_for_delegate(ctx: Ctx, args: argparse.Namespace, host: str | None) -> tuple[str, str, str | None, str, bool]:
    """→ (provider, model, effort, tier, forced)."""
    snaps = ctx.snapshots(args.fresh)
    if args.model:
        m = ctx.cfg.model(args.model)
        if m is None:
            raise CliError("usage", f"unknown model or alias: {args.model}")
        if args.provider and args.provider != m.provider:
            raise CliError("usage", f"{m.id} belongs to {m.provider}, not --provider {args.provider}")
        if not args.force:
            rows = pick(ctx.cfg, snaps, m.tier, need=tuple(args.need), provider=m.provider, host=host,
                        now=ctx.now)["candidates"]
            row = next((r for r in rows if r["model"] == m.id), None)
            if row is None or not row["eligible"]:
                why = row["why"] if row else "not a candidate"
                cls = "rejected" if "rejected" in why or "unavailable" in why else "quota"
                raise CliError(cls, f"{m.id} not eligible: {why} (use --force to bypass)", {"candidates": rows})
        _check_effort(ctx, m.provider, args.effort)
        return m.provider, m.id, args.effort or m.default_effort, m.tier, bool(args.force)
    r = pick(ctx.cfg, snaps, args.tier, need=tuple(args.need), provider=args.provider, host=host,
             allow_downgrade=args.allow_downgrade, now=ctx.now)
    if not r["ok"]:
        raise CliError("quota", r["reason"], {"candidates": r["candidates"]})
    _check_effort(ctx, r["provider"], args.effort)
    return r["provider"], r["model"], args.effort or r["effort"], r["tier"], False


def cmd_delegate(ctx: Ctx, args: argparse.Namespace, out: Out, launcher: str) -> int:
    prompt = _read_prompt(args.prompt_file)
    cwd = _abs_dir(args.cwd)
    schema = None
    if args.schema:
        schema = os.path.abspath(args.schema)
        if not os.path.isfile(schema):
            raise CliError("usage", f"schema file not found: {schema}")
    host = args.host or detect_host(ctx.env)
    provider, model, effort, tier, forced = _choose_for_delegate(ctx, args, host)
    binary = adapters.resolve_binary(provider, ctx.env)
    if binary is None:
        raise CliError("setup", f"{provider} binary not found (set AI_ROUTER_{provider.upper()}_BIN or PATH)")
    timeout_s = args.timeout_s if args.timeout_s is not None else ctx.cfg.settings.job_timeout_s
    spec = jobs.JobSpec(provider=provider, model=model, effort=effort, tier=tier, cwd=cwd, sandbox=args.sandbox,
                        prompt=prompt, timeout_s=timeout_s, stall_min=ctx.cfg.settings.job_stall_min,
                        bare=not args.no_bare, schema=schema, host=host, name=args.name, need=list(args.need),
                        forced=forced, argv=[])
    job_id, d = jobs.create_job(ctx.state, spec, now=ctx.now)
    # argv needs the job dir (codex `-o <job_dir>/last_message.md`), so it is built after create
    spec.argv = adapters.build_argv(provider, binary, model=model, effort=effort, sandbox=args.sandbox, cwd=cwd,
                                    schema=schema, bare=spec.bare, job_dir=d)
    jobs.update_meta(d, argv=spec.argv)
    info = jobs.launch(d, env=ctx.env, launcher=launcher)
    started = {"ok": True, "job_id": job_id, "provider": provider, "model": model, "effort": effort,
               "job_dir": str(d), "events": str(d / "events.jsonl"), "result_path": str(d / "result.txt"),
               "status_path": str(d / "status"), "launcher": info["launcher"], "unit": info["unit"],
               "runner_pid": info["runner_pid"] or jobs.wait_runner_pid(d)}
    if args.wait:
        return _wait_and_report(ctx, out, job_id, timeout_s)
    out.emit(started, f"{job_id} started {provider}/{model} effort={effort} via {info['launcher']} → {d}")
    return 0


def cmd_wait(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    d = ctx.job(args.job_id)
    timeout_s = args.timeout_s if args.timeout_s is not None else float(jobs.read_meta(d).get("timeout_s") or
                                                                        ctx.cfg.settings.job_timeout_s)
    return _wait_and_report(ctx, out, args.job_id, timeout_s)


def cmd_status(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    if args.job_id:
        ctx.job(args.job_id)
        s = jobs.status_obj(ctx.state, args.job_id, ctx.now)
        out.emit(s, f"{s['job_id']} {s['status']} {s['provider']}/{s['model']} age={s['age_s']}s"
                    + (f" retried_as={s['retried_as']}" if s.get("retried_as") else ""))
        return 0
    rows = jobs.list_jobs(ctx.state, all_jobs=True, now=ctx.now)
    out.emit({"ok": True, "jobs": rows}, "\n".join(f"{r['job_id']} {r['status']} {r['provider']}/{r['model']}"
                                                  for r in rows) or "(no jobs)")
    return 0


def cmd_peek(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    d = ctx.job(args.job_id)
    text = jobs.peek(d)
    out.emit({"ok": True, "job_id": args.job_id, "status": jobs.status_of(d), "text": text}, text or "(no message yet)")
    return 0


def cmd_log(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    d = ctx.job(args.job_id)
    lines = [clean(l) for l in jobs.log_lines(d, args.tail)]
    out.emit({"ok": True, "job_id": args.job_id, "status": jobs.status_of(d), "lines": lines},
             "\n".join(lines) or "(empty)")
    return 0


def cmd_result(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    d = ctx.job(args.job_id)
    status = jobs.status_of(d)
    if not jobs.is_terminal(status):
        raise CliError("not_finished", f"job {args.job_id} is {status}", {"job_id": args.job_id, "status": status})
    res = jobs.result_obj(d, ctx.now)
    out.emit(res, _human_result(res))
    return exit_for_status(status)


def cmd_send(ctx: Ctx, args: argparse.Namespace, out: Out, launcher: str) -> int:
    d = ctx.job(args.job_id)
    meta = jobs.read_meta(d)
    status = jobs.status_of(d, meta)
    if not jobs.is_terminal(status):
        raise CliError("not_finished", f"job {args.job_id} is {status}; send is post-hoc only")
    key = "thread_id" if meta["provider"] == "codex" else "session_id"
    conv = meta.get(key)
    if not conv:
        raise CliError("error", f"job {args.job_id} has no {key} to resume")
    prompt = _read_prompt(args.prompt_file)
    binary = adapters.resolve_binary(meta["provider"], ctx.env)
    if binary is None:
        raise CliError("setup", f"{meta['provider']} binary not found")
    argv = adapters.build_resume_argv(meta["provider"], binary, thread_id=conv, model=meta["model"],
                                      effort=meta.get("effort"), sandbox=meta["sandbox"], cwd=meta["cwd"],
                                      bare=bool(meta.get("bare", True)))
    timeout_s = args.timeout_s if args.timeout_s is not None else float(meta.get("timeout_s") or
                                                                        ctx.cfg.settings.job_timeout_s)
    spec = jobs.JobSpec(provider=meta["provider"], model=meta["model"], effort=meta.get("effort"), tier=meta["tier"],
                        cwd=meta["cwd"], sandbox=meta["sandbox"], prompt=prompt, timeout_s=timeout_s,
                        stall_min=meta["stall_min"], bare=bool(meta.get("bare", True)), host=meta.get("host"),
                        name=meta.get("name"), parent_job=args.job_id, argv=argv)
    started = _start_job(ctx, spec, launcher)
    if args.wait:
        return _wait_and_report(ctx, out, started["job_id"], timeout_s)
    out.emit(started, f"{started['job_id']} started (resume of {args.job_id}) via {started['launcher']}")
    return 0


def cmd_cancel(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    ctx.job(args.job_id)
    r = jobs.cancel(ctx.state, args.job_id)
    out.emit({"ok": True, **r}, f"{r['job_id']} {r['status']}")
    return 0


def cmd_list(ctx: Ctx, args: argparse.Namespace, out: Out) -> int:
    rows = jobs.list_jobs(ctx.state, all_jobs=args.all, now=ctx.now)
    out.emit({"ok": True, "jobs": rows},
             "\n".join(f"{r['job_id']} {r['status']:<20} {r['provider']}/{r['model']} age={r['age_s']}s"
                       + (f" {r['name']}" if r.get("name") else "") for r in rows) or "(no jobs)")
    return 0


# --- entry ---------------------------------------------------------------

def main(argv: list[str] | None = None, *, config_path: Path | None = None, env: dict[str, str] | None = None,
         now: float | None = None, launcher: str = "auto") -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    out = Out(bool(getattr(args, "json", False)))
    if args.cmd == "version":
        out.emit({"ok": True, "version": VERSION}, f"ai-router {VERSION}")
        return 0
    e = dict(os.environ if env is None else env)
    try:
        ctx = Ctx(load_config(config_path), state_dir(e), e, time.time() if now is None else now)
        if args.cmd == "quota":
            return cmd_quota(ctx, args, out)
        if args.cmd == "pick":
            return cmd_pick(ctx, args, out)
        if args.cmd == "delegate":
            return cmd_delegate(ctx, args, out, launcher)
        if args.cmd == "wait":
            return cmd_wait(ctx, args, out)
        if args.cmd == "status":
            return cmd_status(ctx, args, out)
        if args.cmd == "peek":
            return cmd_peek(ctx, args, out)
        if args.cmd == "log":
            return cmd_log(ctx, args, out)
        if args.cmd == "result":
            return cmd_result(ctx, args, out)
        if args.cmd == "send":
            return cmd_send(ctx, args, out, launcher)
        if args.cmd == "cancel":
            return cmd_cancel(ctx, args, out)
        if args.cmd == "list":
            return cmd_list(ctx, args, out)
        raise CliError("usage", f"unknown command {args.cmd}")
    except CliError as err:
        return out.error(err)
    except ConfigError as exc:
        return out.error(CliError("setup", str(exc)))
    except adapters.DangerousFlag as exc:
        return out.error(CliError("usage", str(exc)))

#!/usr/bin/env python3
"""Structured agent-config e2e broker over stdio MCP.

Codex runs inside a sandbox with no network, so the real headless agent that
scripts/agent-e2e/agent_e2e.py drives cannot reach the model API from there
(getaddrinfo EAI_AGAIN). This server runs outside that sandbox and exposes one
``run`` tool: an old/new campaign of one scenario, nothing else.

- The harness is the merged one in the live ~/.claude checkout, which must be
  on master with the harness unmodified. A candidate ref cannot replace it.
- Both arms are refs on origin (``origin/<branch>`` or a commit reachable from
  one), resolved to full SHAs before the run: only pushed, reviewable config is
  ever staged.
- The scenario comes from the new ref's tests/agent-e2e tree, by name.
- Model, sample count, parallelism, per-run budget, per-run timeout and the
  whole call's worst-case wall time are bounded by config.json.
- One run at a time; no caller-chosen paths, commands or environment.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
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
CONFIG_PATH = str(HERE / "config.json")
TAG = "[agent-e2e-broker]"

_POLICY_KEYS = frozenset({
    "version", "repo", "harness", "scenarioRoot", "claudeExecutable",
    "outputRoot", "path", "models", "maxSamples", "maxJobs", "maxBudgetUsd",
    "maxTimeoutSec", "maxCallSec", "outputLimitChars",
})
_POSITIVE_INTS = ("maxSamples", "maxJobs", "maxTimeoutSec", "maxCallSec", "outputLimitChars")
_ARGS = frozenset({"scenario", "old_ref", "new_ref", "model", "n", "jobs",
                   "budget_usd", "timeout_sec"})
_SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BRANCH_RE = re.compile(r"^origin/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_FILES = ("prompt.md", "setup.sh", "check.sh")


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
    policy = dict(raw)
    policy["repo"] = _abs(raw["repo"], "repo")
    policy["claudeExecutable"] = _abs(raw["claudeExecutable"], "claudeExecutable")
    policy["outputRoot"] = _abs(raw["outputRoot"], "outputRoot")
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


def _git(policy: dict, *argv: str, timeout: int = 60, env: Optional[dict] = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(policy["repo"]), *argv], capture_output=True,
                          text=True, timeout=timeout, env=env or _env(policy),
                          stdin=subprocess.DEVNULL)


def _check_harness(policy: dict) -> Path:
    """The harness runs from the live checkout: master, harness as merged on origin/master."""
    head = _git(policy, "symbolic-ref", "--short", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != "master":
        raise RuntimeError(f"BROKEN safety boundary: {policy['repo']} is not on master")
    rel = policy["harness"]
    if _git(policy, "ls-files", "--error-unmatch", "--", rel).returncode != 0 \
            or _git(policy, "diff", "--quiet", "origin/master", "--", rel).returncode != 0:
        raise RuntimeError(f"BROKEN safety boundary: harness {rel} is untracked or differs "
                           "from origin/master")
    return policy["repo"] / rel


def _resolve(policy: dict, ref: str) -> str:
    if _SHA_RE.fullmatch(ref):
        if _git(policy, "cat-file", "-e", f"{ref}^{{commit}}").returncode != 0:
            raise RuntimeError(f"{ref} is not a commit in {policy['repo']}")
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


def _extract_scenarios(policy: dict, sha: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(policy["repo"]), "archive", sha, "--",
                                policy["scenarioRoot"]], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=_env(policy), stdin=subprocess.DEVNULL)
    tar = subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=archive.stdout,
                         capture_output=True, env=_env(policy), timeout=120)
    archive.stdout.close()
    if archive.wait(timeout=120) != 0 or tar.returncode != 0:
        raise RuntimeError(f"could not extract {policy['scenarioRoot']} from {sha}")


# The harness runs under a supervisor that the kernel signals when the broker
# dies (pdeathsig), and that then kills its whole process group: harness,
# bwrap and the agent under test. The supervisor and harness also hold the
# campaign lock, so it frees only once nothing is left spending.
_SUPERVISOR = 'trap "kill -KILL 0" TERM; "$@" & wait $!'


def _supervised(argv: list[str], policy: dict) -> list[str]:
    path = ":".join(policy["path"])
    tools = {name: shutil.which(name, path=path) for name in ("setpriv", "bash")}
    missing = [name for name, found in tools.items() if not found]
    if missing:
        raise RuntimeError(f"BROKEN safety boundary: not on policy path: {', '.join(missing)}")
    return [tools["setpriv"], "--pdeathsig", "TERM", "--", tools["bash"], "-c", _SUPERVISOR,
            "agent-e2e-supervisor", *argv]


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


def _report(out: Path, scenario: str) -> str:
    lines = []
    for verdict in sorted((out / scenario).glob("*/*/verdict.json")):
        try:
            v = json.loads(verdict.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        final = verdict.parent / "final.txt"
        text = final.read_text(encoding="utf-8", errors="replace").strip() if final.is_file() else ""
        lines.append(f"- {v.get('arm')}#{v.get('run')} {v.get('verdict')} "
                     f"(${v.get('cost_usd')}, {v.get('seconds')}s): {v.get('why', '')}\n"
                     f"  run dir: {verdict.parent}\n  final.txt: {text[:600]!r}")
    return "\n".join(lines)


def _tool_run(args: dict, rec: Optional[_InFlight] = None) -> str:
    policy = _load_policy(_load_config())
    req = _validate_request(args, policy)
    exe = policy["claudeExecutable"]
    if not os.access(exe, os.X_OK):
        raise RuntimeError(f"BROKEN safety boundary: claude is not executable: {exe}")
    root = policy["outputRoot"]
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(root / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another agent-e2e campaign is running; one at a time") from None
        # The fetch alone gets the SSH agent (origin is an ssh remote); the
        # harness and the agent under test never do.
        fetch_env = dict(_env(policy), GIT_TERMINAL_PROMPT="0")
        if os.environ.get("SSH_AUTH_SOCK"):
            fetch_env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        try:
            fetch = _git(policy, "fetch", "--quiet", "--prune", "origin", timeout=120,
                         env=fetch_env)
        except subprocess.TimeoutExpired:
            fetch = subprocess.CompletedProcess([], 124, "", "timed out")
        # Fail closed: stale or planted remote-tracking refs must never stand
        # in for what origin holds now.
        if fetch.returncode != 0:
            raise RuntimeError(f"git fetch origin failed: {fetch.stderr.strip()[:300]}")
        harness = _check_harness(policy)
        old = _resolve(policy, req["old_ref"])
        new = _resolve(policy, req["new_ref"])
        scen_rel = f"{policy['scenarioRoot']}/{req['scenario']}"
        for name in _RUN_FILES:
            if _git(policy, "cat-file", "-e", f"{new}:{scen_rel}/{name}").returncode != 0:
                raise RuntimeError(f"{req['new_ref']} has no {scen_rel}/{name}")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = root / f"{stamp}-{req['scenario']}-{new[:8]}-{os.getpid()}"
        _extract_scenarios(policy, new, out / "scenarios")
        argv = [sys.executable, "-I", str(harness), "run", str(out / "scenarios" / scen_rel),
                "--arm", f"old={old}", "--arm", f"new={new}",
                "--n", str(req["n"]), "--jobs", str(req["jobs"]), "--model", req["model"],
                "--budget", f"{req['budget']:g}", "--timeout", str(req["timeout"]),
                "--out", str(out / "runs"), "--claude", str(exe)]
        rc, stdout, stderr = _run_argv(_supervised(argv, policy), _env(policy),
                                       req["call_timeout"], rec, lock.fileno())
    header = (f"agent-e2e campaign `{req['scenario']}` model={req['model']} n={req['n']} "
              f"jobs={req['jobs']} budget=${req['budget']:g} timeout={req['timeout']}s\n"
              f"old = {req['old_ref']} = {old}\nnew = {req['new_ref']} = {new}\n"
              f"output: {out}\n")
    body = "\n".join([header, stdout.rstrip(), "--- per run ---",
                      _report(out / "runs", req["scenario"]),
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
        "old=old_ref vs arm new=new_ref, outside the Codex sandbox. Refs must be on origin "
        "(origin/<branch> or a pushed 40-hex commit). One campaign at a time; it blocks "
        "until done (up to an hour). Returns the harness summary table, every run's "
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

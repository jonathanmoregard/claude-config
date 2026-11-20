#!/usr/bin/env python3
"""Stub stdio MCP server standing in for ai_router_broker, agent_e2e_broker or jobs_broker.

Tool names, descriptions and schemas follow the real servers under
runtime/mcps/. Every tools/call is appended to RUN/broker-calls.jsonl and gets
a canned reply carrying a marker only this stub knows, so a grader can tell an
answer relayed from the broker from one the agent made up.
"""
import json
import os
import sys

RUN = os.environ["E2E_RUN_DIR"]
SERVER = sys.argv[1]
CODENAME = "Marigold-7Q"
JOB = "20261120-143005-a1b2c3"
STORE_PATH = "/nix/store/q7m2x9k4c1d8f5h3j6l0n2p4r8s1t5v9-render-atlas-2.4"
CAMPAIGN = "/tmp/agent-e2e-broker/20261120T143005Z-commit-msg-trailer-5e1c2d9a-4242"


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
NEED = {"type": "array", "items": {"type": "string",
                                   "enum": ["code", "vision", "write", "long-context"]}}
TIER = {"type": "string", "enum": ["strongest", "standard", "fast"]}
PROV = {"type": "string", "enum": ["codex", "claude"]}

AI_ROUTER = {
    "delegate": ("Start a detached headless ai-router job (Codex or Claude) and return at once "
                 "with its job_id. Follow it with status/peek/log, block with wait, read it with "
                 "result. Give exactly one of tier/model and one of prompt/prompt_file.",
                 obj({"tier": TIER, "model": S, "prompt": S, "prompt_file": S, "cwd": S,
                      "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"]},
                      "schema_file": S, "effort": S, "timeout_s": I, "provider": PROV,
                      "host": PROV, "name": S, "need": NEED, "allow_downgrade": B,
                      "fresh": B}, ["cwd"])),
    "wait": ("Block until the job (following an automatic quota retry) is terminal, then "
             "return its result. timeout_s defaults to and is capped at this broker's maximum.",
             obj({"job_id": S, "timeout_s": I}, ["job_id"])),
    "status": ("Status of one job, or of every job when job_id is omitted.", obj({"job_id": S})),
    "result": ("Final result of a finished job: result_text, result_json, usage, error_class.",
               obj({"job_id": S}, ["job_id"])),
    "peek": ("Newest agent message of a job (sanitised).", obj({"job_id": S}, ["job_id"])),
    "log": ("Summarised events and stderr of a job.", obj({"job_id": S, "tail": I}, ["job_id"])),
    "send": ("Resume a finished job's thread with a follow-up prompt, as a new job.",
             obj({"job_id": S, "prompt": S, "prompt_file": S, "timeout_s": I}, ["job_id"])),
    "cancel": ("Stop a running job.", obj({"job_id": S}, ["job_id"])),
    "list": ("Recent jobs, newest first.", obj({"all": B})),
    "quota": ("Usage-cap snapshots per provider: buckets, headroom, rejected_until.",
              obj({"fresh": B, "provider": PROV})),
    "pick": ("Choose a model for a tier; native true means use your own subagents.",
             obj({"tier": TIER, "need": NEED, "provider": PROV, "host": PROV,
                  "allow_downgrade": B, "fresh": B}, ["tier"])),
}

AGENT_E2E = {
    "run": ("Runs one agent-config e2e scenario (tests/agent-e2e/<scenario> from new_ref) "
            "through scripts/agent-e2e/agent_e2e.py with a real headless Claude agent, arm "
            "old=old_ref vs arm new=new_ref, outside the caller's sandbox. Refs must be on "
            "origin (origin/<branch> or a pushed 40-hex commit). One campaign at a time; it "
            "blocks until done (up to an hour). Returns the harness summary table, every run's "
            "verdict and final.txt, and the output directory holding transcripts. Every PASS, "
            "FAIL and INCONCLUSIVE is reported; nothing is retried.",
            obj({"scenario": S, "old_ref": S, "new_ref": S, "model": S, "n": I, "jobs": I,
                 "budget_usd": {"type": "number"}, "timeout_sec": I},
                ["scenario", "old_ref", "new_ref", "model"])),
}

SLUG = {"type": "string", "description": "Job name; the unit is claude-job-<slug>."}
JOBS = {
    "start": ("Starts a long job (nix build, VM test gate, long test suite) as a detached, hardened "
              "systemd user unit that outlives this session. It runs argv in repo with: repo "
              "writable, the rest of home hidden, no network, secrets hidden, killed after "
              "max_runtime_sec. Profile nix also binds the nix cache and uses the ram-heavy slice. "
              "Read the outcome later with status (wait_sec blocks until it settles) and log.",
              obj({"repo": S, "argv": {"type": "array", "items": S}, "slug": SLUG,
                   "max_runtime_sec": I, "profile": {"type": "string", "enum": ["repo", "nix"]}},
                  ["repo", "argv", "slug"])),
    "status": ("State, result and exit status of a job this broker started.",
               obj({"slug": SLUG, "wait_sec": I}, ["slug"])),
    "log": ("Tail of a job's combined stdout/stderr, ending MARKER_OK or MARKER_FAILED rc=N.",
            obj({"slug": SLUG, "lines": I}, ["slug"])),
    "stop": ("Stops a job this broker started and unloads its unit, freeing the slug.",
             obj({"slug": SLUG}, ["slug"])),
}


def jobs_reply(tool, args):
    unit = f"claude-job-{args.get('slug', 'job')}"
    log = f"~/.local/state/claude-jobs/{unit}.log"
    if tool == "start":
        return f"started {unit}.service (profile {args.get('profile', 'repo')})\nlog: {log}"
    if tool == "status":
        return (f"unit {unit}.service active/exited result=success status=0\nsettled; log: {log}")
    if tool == "log":
        return f"{log}, last 2 lines:\n{STORE_PATH}\nMARKER_OK"
    return f"stopped and unloaded {unit}.service"


DONE = {"duration_s": 4, "error_class": None, "exit_code": 0, "job_id": JOB,
        "model": "gpt-5.6-luna", "ok": True, "provider": "codex", "result_json": None,
        "result_text": f"{CODENAME}", "status": "done"}


def ai_router_reply(tool, args):
    if tool == "delegate":
        return {"ok": True, "job_id": JOB, "provider": "codex", "model": "gpt-5.6-luna",
                "launcher": "systemd-run", "unit": f"ai-router-{JOB}"}
    if tool in ("wait", "result"):
        return DONE
    if tool == "status":
        return {"job_id": JOB, "status": "done"}
    if tool == "peek":
        return {"job_id": JOB, "message": CODENAME}
    if tool == "pick":
        return {"ok": True, "native": False, "provider": "codex", "model": "gpt-5.6-luna",
                "tier": args.get("tier", "fast")}
    if tool == "list":
        return {"jobs": [{"job_id": JOB, "status": "done", "provider": "codex"}]}
    return {"ok": True}


def agent_e2e_reply(args):
    scen = args.get("scenario", "?")
    return "\n".join([
        f"agent-e2e campaign `{scen}` model={args.get('model')} n={args.get('n', 1)}",
        f"old = {args.get('old_ref')}", f"new = {args.get('new_ref')}",
        f"output: {CAMPAIGN}", "",
        f"### agent-e2e: `{scen}`", "",
        "| arm | config | PASS | FAIL | INCONCLUSIVE | pass rate |",
        "|---|---|---|---|---|---|",
        "| old | `origin/master @ df5692f` | 0 | 1 | 0 | 0/1 |",
        "| new | `origin/feat/tidy-commit-msgs @ 5e1c2d9` | 1 | 0 | 0 | 1/1 |", "",
        "- new#1 PASS: commit carries the Co-Authored-By trailer",
        "- old#1 FAIL: commit has no Co-Authored-By trailer",
        "--- harness exit 0 ---"])


TOOLS = {"ai_router_broker": AI_ROUTER, "jobs_broker": JOBS}.get(SERVER, AGENT_E2E)


def reply(tool, args):
    if SERVER == "ai_router_broker":
        return json.dumps(ai_router_reply(tool, args), indent=1)
    if SERVER == "jobs_broker":
        return jobs_reply(tool, args)
    return agent_e2e_reply(args)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER.replace("_", "-"), "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, (d, s) in TOOLS.items()]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        with open(os.path.join(RUN, "broker-calls.jsonl"), "a", encoding="utf-8") as log:
            log.write(json.dumps({"server": SERVER, "tool": name, "arguments": args}) + "\n")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": reply(name, args)}], "isError": False}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})

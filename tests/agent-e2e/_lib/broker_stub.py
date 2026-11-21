#!/usr/bin/env python3
"""Stub stdio MCP server standing in for agent_e2e_broker or jobs_broker.

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
STORE_PATH = "/nix/store/q7m2x9k4c1d8f5h3j6l0n2p4r8s1t5v9-render-atlas-2.4"
CAMPAIGN = "/home/user/.local/state/agent-e2e-broker/campaigns/20261120T143005Z-commit-msg-trailer-5e1c2d9a-4242"


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
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
    "start": ("Starts a long job (nix build, VM test gate, long test suite) as a detached systemd "
              "user unit that outlives this session. argv runs in repo under the session's own "
              "sandbox, network allowlist and the write protections a session has in repo "
              "included, with writes limited to repo; the rest of home and secrets are hidden, and "
              "it is killed after max_runtime_sec. Profile nix also lets it write the nix cache "
              "and uses the ram-heavy slice. Read the outcome later with status (wait_sec blocks "
              "until it settles) and log.",
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


TOOLS = JOBS if SERVER == "jobs_broker" else AGENT_E2E


def reply(tool, args):
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

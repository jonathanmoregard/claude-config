#!/usr/bin/env python3
"""ARTL verdict intake over stdio MCP: one tool, `submit_verdict`.

The `advisor` subagent ends a review by calling it. Its inputSchema is
skills/advice-refine-test-loop/verdict-schema.json, loaded, and the server
checks every submission with hooks/artl_verdict.py (the schema plus the
verdict/findings agreement). A bad one is refused with isError and the reasons,
so the advisor fixes it and resubmits; nothing about it is kept.

A good one is saved to ~/.local/state/artl-verdicts/<id>.json for the parent to
Read, and the ack hands the advisor the one line to reply with, naming that
file. The ARTL evidence is not written here: hooks/artl-evidence.py records the
call from its PostToolUse, which carries the advisor's agent_id.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "hooks"))
import artl_verdict  # noqa: E402

SERVER_INFO = {"name": "artl-verdict", "version": "1.0.0"}
RETAIN_SEC = 7 * 86400
TOOL = {
    "name": "submit_verdict",
    "description": ("Submit your review verdict (advisor, verdict dispatches only). Call it once; "
                    "if it returns an error, fix what it names and call it again. On success, "
                    "reply with exactly the line it gives you."),
    "inputSchema": artl_verdict.load_schema(),
}


def verdict_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".local" / "state" / "artl-verdicts"


def save(verdict: dict) -> Path:
    directory = verdict_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in directory.glob("*.json"):
        try:
            if now - old.stat().st_mtime > RETAIN_SEC:
                old.unlink()
        except OSError:
            pass
    path = directory / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}-{secrets.token_hex(4)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(verdict, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def submit(arguments: object) -> tuple[str, bool]:
    """(text, is_error)"""
    found = artl_verdict.problems(arguments)
    if found:
        return ("Verdict refused; nothing was recorded. Fix these and call submit_verdict "
                "again:\n- " + "\n- ".join(found)), True
    n = len(arguments["findings"])
    path = save(arguments)
    line = f"{arguments['verdict']}, {n} finding{'' if n == 1 else 's'}: {path}"
    return f"Verdict recorded. Reply with exactly this line and nothing else:\n{line}", False


def respond(msg_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        respond(msg_id, {"protocolVersion": "2024-11-05",
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": SERVER_INFO})
    elif method == "tools/list":
        respond(msg_id, {"tools": [TOOL]})
    elif method == "tools/call" and msg_id is not None:
        params = msg.get("params") or {}
        if params.get("name") != TOOL["name"]:
            respond(msg_id, error={"code": -32601, "message": f"Unknown tool: {params.get('name')}"})
            return
        try:
            text, is_error = submit(params.get("arguments"))
        except OSError as exc:
            text, is_error = f"ERROR: could not save the verdict: {exc}", True
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        respond(msg_id, result)
    elif msg_id is not None and not str(method).startswith("notifications/"):
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    main()

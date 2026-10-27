#!/usr/bin/env python3
"""Stub `codex` for the unit suite: speaks `codex exec --json` and the
`codex app-server` JSON-RPC handshake. No network, no quota.

Behaviour is chosen by a `MODE:<mode>` token in the prompt (so it survives a
detached launch without env plumbing):
  ok (default) · quota · quota@<model> (quota only for that -m) · rejected ·
  stall (one event then silence) · slow (events for ~20 s) · unparseable ·
  crash (exit 1, generic stderr) · overloaded
Env FAKE_CODEX_APPSERVER=hang makes app-server never answer.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def app_server() -> int:
    if os.environ.get("FAKE_CODEX_APPSERVER") == "hang":
        time.sleep(60)
        return 0
    rl = json.load(open(os.path.join(HERE, "codex-app-server-rate-limits.json")))["result"]
    ml = json.load(open(os.path.join(HERE, "codex-app-server-model-list.json")))["result"]
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        method = req.get("method")
        if method == "initialize":
            emit({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "initialized":
            emit({"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        elif method == "account/rateLimits/read":
            emit({"id": rid, "result": rl})
        elif method == "model/list":
            emit({"id": rid, "result": ml})
        elif rid is not None:
            emit({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
    return 0


def exec_cmd(argv: list[str]) -> int:
    for a in argv:
        # arftl-allow: permission-widened — this fixture REFUSES these flags; the tests prove the adapter never passes them
        if a.startswith("--dangerously") or a in ("--full-auto", "--yolo"):
            sys.stderr.write(f"fake_codex: dangerous flag {a}\n")
            return 99
    resume_id = None
    if argv and argv[0] == "resume":
        resume_id = argv[1]
        argv = argv[2:]
    prompt = argv[argv.index("--") + 1] if "--" in argv else ""
    model = argv[argv.index("-m") + 1] if "-m" in argv else "default"
    out = argv[argv.index("-o") + 1] if "-o" in argv else None
    m = re.search(r"MODE:([\w@.\-]+)", prompt)
    mode = m.group(1) if m else "ok"
    if mode.startswith("quota@"):
        mode = "quota" if mode.split("@", 1)[1] == model else "ok"

    thread = resume_id or "thread-fake-0001"
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if mode == "quota":
        emit({"type": "error", "message": "You've hit your usage limit. Try again later."})
        emit({"type": "turn.failed", "error": {"message": "usage limit reached"}})
        return 1
    if mode == "rejected":
        sys.stderr.write('ERROR: unexpected status 400 Bad Request: {"error":{"message":"model_not_found"}}\n')
        return 1
    if mode == "overloaded":
        sys.stderr.write("ERROR: upstream overloaded (529)\n")
        return 1
    if mode == "crash":
        sys.stderr.write("fake_codex: something broke\n")
        return 1
    if mode == "stall":
        time.sleep(120)
        return 0
    if mode == "slow":
        for i in range(100):
            emit({"type": "item.completed", "item": {"id": f"r{i}", "type": "reasoning", "text": f"step {i}"}})
            time.sleep(0.2)
        return 0
    if mode == "unparseable":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        return 0
    emit({"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "thinking"}})
    prefix = "RESUMED: " if resume_id else ""
    text = prefix + "OK \x1b[32m" + prompt.splitlines()[0][:60] + "\x1b[0m" if prompt else "OK"
    if mode == "json":
        text = json.dumps({"answer": 42, "model": model})
    emit({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 3}})
    if out:
        with open(out, "w") as fh:
            fh.write(text + "\n")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 2
    if argv[0] == "app-server":
        return app_server()
    if argv[0] == "exec":
        return exec_cmd(argv[1:])
    sys.stderr.write(f"fake_codex: unknown subcommand {argv[0]}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())

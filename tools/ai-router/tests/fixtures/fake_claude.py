#!/usr/bin/env python3
"""Stub `claude -p --output-format stream-json` for the unit suite.

Mode via `MODE:<mode>` in the prompt: ok (default) · quota · quota@<model> ·
overloaded · max_turns (result error) · stall · unparseable · crash.
"""

from __future__ import annotations

import json
import re
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    for a in argv:
        if a.startswith("--dangerously") or a.startswith("--allow-dangerously") or a == "--bare":
            sys.stderr.write(f"fake_claude: refused flag {a}\n")
            return 99
    if "-p" not in argv:
        sys.stderr.write("fake_claude: expected -p\n")
        return 2

    def opt(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv else None

    model = opt("--model") or "default"
    resume = opt("--resume")
    prompt = argv[-1]
    m = re.search(r"MODE:([\w@.\-]+)", prompt)
    mode = m.group(1) if m else "ok"
    if mode.startswith("quota@"):
        mode = "quota" if mode.split("@", 1)[1] == model else "ok"

    session = resume or "session-fake-0001"
    emit({"type": "system", "subtype": "init", "session_id": session, "model": model,
          "tools": opt("--tools"), "permissionMode": opt("--permission-mode")})
    if mode == "quota":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "You've hit your limit · resets 3pm (Europe/Stockholm)", "session_id": session})
        return 1
    if mode == "overloaded":
        sys.stderr.write("API Error: 529 overloaded_error\n")
        return 1
    if mode == "max_turns":
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "hit max turns",
              "session_id": session})
        return 1
    if mode == "crash":
        sys.stderr.write("fake_claude: crashed\n")
        return 1
    if mode == "stall":
        time.sleep(120)
        return 0
    if mode == "unparseable":
        sys.stdout.write("plain text, no result event\n")
        sys.stdout.flush()
        return 0
    text = ("RESUMED: " if resume else "") + "OK " + prompt.splitlines()[0][:60]
    if mode == "json":
        text = json.dumps({"answer": 42, "model": model})
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
          "session_id": session})
    emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 12, "num_turns": 1,
          "result": text, "session_id": session, "total_cost_usd": 0.0007,
          "usage": {"input_tokens": 9, "output_tokens": 4}})
    return 0


if __name__ == "__main__":
    sys.exit(main())

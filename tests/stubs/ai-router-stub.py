#!/usr/bin/env python3
"""Stand-in for `bin/ai-router` used by tests/test_ai_router_hooks.py.

The gate hook shells out to `${AI_ROUTER_BIN} pick ...`; pointing
AI_ROUTER_BIN at this file makes the hook testable without the real router
(a sibling track owns that). Behaviour is driven entirely by environment:

  AI_ROUTER_STUB_LOG              append one JSON line per call: argv[1:]
  AI_ROUTER_STUB_RESPONSE         text printed on stdout (default: a native
                                  strongest pick of claude-fable-5-1)
  AI_ROUTER_STUB_RESPONSE_CLAUDE  overrides RESPONSE when argv carries
                                  `--provider claude`
  AI_ROUTER_STUB_RESPONSE_ANY     overrides RESPONSE when argv does NOT
  AI_ROUTER_STUB_EXIT             exit code (default 0)
  AI_ROUTER_STUB_SLEEP_S          sleep before answering (default 0)

Anything printed is verbatim, so a test can hand it garbage.
"""
import json
import os
import sys
import time

DEFAULT = {
    "ok": True,
    "provider": "claude",
    "model": "claude-fable-5-1",
    "effort": "high",
    "tier": "strongest",
    "native": True,
    "host": "claude",
    "headroom": 62,
    "reason": "rank-first: first eligible in pins order",
    "candidates": [
        {"model": "gpt-5.6-sol", "provider": "codex", "eligible": False,
         "headroom": 10, "why": "headroom 10 < reserve 15"},
        {"model": "claude-fable-5-1", "provider": "claude", "eligible": True,
         "headroom": 62, "why": "eligible"},
    ],
}


def main() -> int:
    argv = sys.argv[1:]
    log = os.environ.get("AI_ROUTER_STUB_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")

    sleep_s = float(os.environ.get("AI_ROUTER_STUB_SLEEP_S") or 0)
    if sleep_s > 0:
        time.sleep(sleep_s)

    claude_only = "--provider" in argv and argv[argv.index("--provider") + 1] == "claude"
    key = "AI_ROUTER_STUB_RESPONSE_CLAUDE" if claude_only else "AI_ROUTER_STUB_RESPONSE_ANY"
    response = os.environ.get(key)
    if response is None:
        response = os.environ.get("AI_ROUTER_STUB_RESPONSE")
    if response is None:
        response = json.dumps(DEFAULT)
    sys.stdout.write(response)
    sys.stdout.flush()
    return int(os.environ.get("AI_ROUTER_STUB_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())

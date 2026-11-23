#!/usr/bin/env python3
"""After a background Agent dispatch, tell the parent to arm the idle alarm.

PostToolUse on Agent fires at LAUNCH for a background dispatch, with
``tool_response = {"status": "async_launched", "agentId": ...}`` (measured,
see artl-evidence.py). This hook adds one line of context naming the
``wait-for agent <id>`` call to run in the background. That call exits 5 when
the agent's transcript has been quiet for the idle limit, which wakes the
parent; it has no "finished" exit, so the parent stops it with the result.

Main session only: a subagent's background Bash is not promised to wake it.
Prints nothing for any other call, and fails open on every error.
"""
from __future__ import annotations

import json
import os
import re
import sys

AGENT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def context_for(payload: object) -> str | None:
    if not isinstance(payload, dict) or payload.get("tool_name") != "Agent":
        return None
    if payload.get("agent_id"):
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("status") != "async_launched":
        return None
    agent = response.get("agentId")
    if not isinstance(agent, str) or not AGENT_ID.fullmatch(agent):
        return None
    limit = os.environ.get("WAIT_FOR_AGENT_IDLE", "")
    valid = limit.isascii() and limit.isdigit() and int(limit) > 0
    quiet = f"{int(limit)} seconds" if valid and int(limit) != 7200 else "2 hours"
    return (
        f"Background agent {agent} has no idle alarm yet. Arm "
        f"`~/.claude/scripts/wait-for agent {agent}` in the background "
        "(Bash, run_in_background: true). It exits 5 when the agent's "
        f"transcript has been quiet for {quiet}, which usually means it "
        "stalled or ended its turn waiting on something: then read the "
        "transcript tail and unblock it, replace it, or re-arm with a longer "
        "--idle. This alarm fires once: an agent that carries on after it "
        "(unblocked, or its replacement) needs a new waiter. Stop the waiter (TaskStop) once you have the agent's result. "
        "Exit 124 means the wait timed out; arm it again."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        text = context_for(payload)
    except Exception:
        return 0
    if text:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}},
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

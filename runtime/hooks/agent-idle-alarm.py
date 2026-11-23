#!/usr/bin/env python3
"""After a background Agent dispatch, tell the parent to arm the idle alarm.

PostToolUse on Agent fires at LAUNCH for a background dispatch, with
``tool_response = {"status": "async_launched", "agentId": ...}`` (measured,
see artl-evidence.py). This hook adds one line of context naming the
``wait-for agent <id>`` call to run in the background. That call exits 0 when
the agent finishes and 5 when it stalls (unfinished, transcript quiet for the
idle limit, no unit it launched still running), which wakes the parent.

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
    if not isinstance(agent, str) or not AGENT_ID.match(agent):
        return None
    limit = os.environ.get("WAIT_FOR_AGENT_IDLE", "")
    quiet = f"{limit} seconds" if limit.isdigit() and limit != "7200" else "2 hours"
    return (
        f"Background agent {agent} has no idle alarm yet. Arm "
        f"`~/.claude/scripts/wait-for agent {agent}` in the background now "
        "(Bash, run_in_background: true). It exits 0 when the agent finishes "
        f"and 5 when the agent has stalled: quiet for {quiet} with no unit of "
        "its own still running. On 5, inspect the agent and unblock or "
        "replace it."
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

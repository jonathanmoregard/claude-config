#!/usr/bin/env python3
"""Record the model an Agent call actually resolved to, without prompt data.

PostToolUse exposes ``resolvedModel``; unlike PreToolUse, this is evidence of
what started rather than an attempted rewrite. The hook writes metadata only,
prints nothing, never calls a model, and fails open.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time


ROLE_FAMILY = {
    "Explore": "haiku",
    "explore": "haiku",
    "worker-easy": "sonnet",
    "worker-hard": "opus",
    "advisor": "fable",
    "general-purpose": "sonnet",
}


def family(model: object) -> str | None:
    if not isinstance(model, str):
        return None
    lowered = model.lower()
    return next((name for name in ("haiku", "sonnet", "opus", "fable") if name in lowered), None)


def expected_family(agent_type: object, requested_model: object) -> str | None:
    if not isinstance(agent_type, str) or agent_type == "fork":
        return None
    if agent_type in ROLE_FAMILY:
        return ROLE_FAMILY[agent_type]
    return family(requested_model)


def record_from(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict) or payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return None
    resolved = response.get("resolvedModel")
    if not isinstance(resolved, str) or not resolved:
        return None
    agent_type = tool_input.get("subagent_type")
    requested = tool_input.get("model")
    expected = expected_family(agent_type, requested)
    resolved_family = family(resolved)
    models_used = response.get("modelsUsed")
    if not isinstance(models_used, list):
        models_used = None
    return {
        "observed_at": int(time.time()),
        "session_id": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "agent_id": response.get("agentId"),
        "agent_type": agent_type,
        "status": response.get("status"),
        "requested_model": requested if isinstance(requested, str) and requested else None,
        "resolved_model": resolved,
        "models_used": models_used,
        "expected_family": expected,
        "resolved_family": resolved_family,
        "mismatch": (resolved_family != expected) if expected is not None else None,
    }


def append_record(record: dict[str, object]) -> None:
    state = os.environ.get("AI_ROUTER_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "ai-router"
    )
    os.makedirs(state, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(state, 0o700)
    path = os.path.join(state, "native-agent-routes.jsonl")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        os.close(fd)


def main() -> None:
    payload = json.loads(sys.stdin.read())
    record = record_from(payload)
    if record is not None:
        append_record(record)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    raise SystemExit(0)

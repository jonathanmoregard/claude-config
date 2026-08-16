#!/usr/bin/env python3
"""Stdio MCP server: the global egress gate.

Every outbound request against a rate-sensitive resource takes a slot here
first. The gate lives in MCP server code and not in a PreToolUse hook on
purpose: hooks do not reliably propagate to subagents, and a gate that must
hold cannot be advisory. Hooks may observe and report the running total; they
may not be the thing that says no.

Tools:
  `mcp__egress-guard__acquire(resource, cost?, min_interval_s?, description?)`
  `mcp__egress-guard__declare(resource, min_interval_s, description?)`
  `mcp__egress-guard__status(resource)`
  `mcp__egress-guard__record_block(resource, reason, cooldown_s?)`

The enforced cadence is a FLOOR: the strictest interval any participant has
declared, never the caller's own. A fast client cannot speed the shared
schedule up. Budgets are cost-weighted and raise on exhaustion; they never
sleep and never degrade to a warning. Cooldowns refuse with time remaining.

There is no override argument on any tool. Widening a limit means editing
`egress-policy.json` next to this file.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from egress_guard.limiter import (  # noqa: E402
    EgressGuard,
    EgressGuardError,
)

POLICY_PATH = os.environ.get(
    "EGRESS_GUARD_POLICY", os.path.join(_HERE, "egress-policy.json")
)


class _InFlight:
    """Per-request state for the threaded dispatcher.

    `cancelled` flips to True when `notifications/cancelled` arrives for this
    id. The check and the response write happen under the same locks in
    `_send_response`, so a cancel landing just before the write is observed.

    An in-flight `acquire` may be parked in `time.sleep` waiting for its slot.
    We deliberately do NOT interrupt that sleep on cancel: the slot is already
    reserved and the schedule is shared, so tearing the wait down early would
    only desynchronise other participants. We suppress the response instead.
    """

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False


# Stdout writes from the main thread and N workers MUST be serialized, else
# JSON-RPC frames interleave mid-line and corrupt the protocol.
_write_lock = threading.Lock()
_in_flight_lock = threading.Lock()
_in_flight: dict[Any, _InFlight] = {}


def _guard() -> EgressGuard:
    """Fresh guard per call so `EGRESS_GUARD_*` env and policy edits apply."""
    return EgressGuard(
        state_dir=os.environ.get("EGRESS_GUARD_STATE"),
        policy_path=os.environ.get("EGRESS_GUARD_POLICY", POLICY_PATH),
    )


def _require_resource(args: dict) -> str:
    resource = args.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise ValueError(
            "argument `resource` is required and must be a non-empty string "
            "(a host, API name or other blast-radius unit)."
        )
    return resource.strip()


def _optional_number(args: dict, key: str) -> Optional[float]:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"argument `{key}` must be a number if given.")
    return float(value)


# ----------------------------------------------------------------------
# Tool bodies
# ----------------------------------------------------------------------


def _tool_acquire(args: dict) -> str:
    resource = _require_resource(args)
    cost = _optional_number(args, "cost")
    min_interval_s = _optional_number(args, "min_interval_s")
    grant = _guard().acquire(
        resource,
        cost=1.0 if cost is None else cost,
        min_interval_s=min_interval_s,
    )
    payload = grant.as_dict()
    payload["granted"] = True
    return json.dumps(payload, indent=2)


def _tool_declare(args: dict) -> str:
    resource = _require_resource(args)
    min_interval_s = _optional_number(args, "min_interval_s")
    if min_interval_s is None:
        raise ValueError("argument `min_interval_s` is required for declare.")
    expected_requests = _optional_number(args, "expected_requests")
    expected_cost = _optional_number(args, "expected_cost")
    ceiling_requests = _optional_number(args, "ceiling_requests")
    pushback = args.get("pushback")
    if pushback is not None and not isinstance(pushback, str):
        raise ValueError("argument `pushback` must be a string if given.")
    floor = _guard().declare(
        resource,
        min_interval_s,
        expected_requests=None if expected_requests is None else int(expected_requests),
        expected_cost=expected_cost,
        ceiling_requests=None if ceiling_requests is None else int(ceiling_requests),
        pushback=pushback,
    )
    return json.dumps(
        {
            "resource": resource,
            "declared_s": min_interval_s,
            "floor_s": floor,
            "note": (
                "floor is the strictest interval declared by any participant; "
                "it can be raised here but never lowered"
            ),
        },
        indent=2,
    )


def _tool_status(args: dict) -> str:
    return json.dumps(_guard().status(_require_resource(args)), indent=2)


def _tool_record_block(args: dict) -> str:
    resource = _require_resource(args)
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("argument `reason` is required for record_block.")
    cooldown_s = _optional_number(args, "cooldown_s")
    guard = _guard()
    until = guard.record_block(resource, reason.strip(), cooldown_s)
    status = guard.status(resource)
    return json.dumps(
        {
            "resource": resource,
            "cooldown_until": until,
            "cooldown_remaining_s": status["cooldown_remaining_s"],
            "reason": reason.strip(),
            "note": (
                "every caller is refused until the cooldown lapses. Recovery is "
                "a retry scheduled for after that timestamp — not an escalating "
                "ladder, not a slower retry now, and never an identity change."
            ),
        },
        indent=2,
    )


def _tool_record_preflight(args: dict) -> str:
    resource = _require_resource(args)
    checks = args.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(
            "argument `checks` must be a non-empty list of {name, passed} objects — "
            "name the invariants you actually proved on the bounded sample."
        )
    sample_requests = _optional_number(args, "sample_requests")
    record = _guard().record_preflight(
        resource,
        checks=checks,
        sample_requests=int(sample_requests or 0),
    )
    return json.dumps(
        {
            "resource": resource,
            "preflight": record,
            "note": (
                "recorded. A preflight with any failing check is refused rather "
                "than stored — fix the invariant, then record it."
            ),
        },
        indent=2,
    )


_TOOL_BODIES = {
    "acquire": _tool_acquire,
    "declare": _tool_declare,
    "status": _tool_status,
    "record_block": _tool_record_block,
    "record_preflight": _tool_record_preflight,
}


TOOLS = [
    {
        "name": "acquire",
        "description": (
            "Take one slot against a rate-sensitive resource. Call this "
            "IMMEDIATELY BEFORE every outbound request — including one-off "
            "diagnostic probes, retries and health checks, not just the main "
            "crawl. The aggregate is what gets you blocked, so nothing is "
            "exempt.\n\n"
            "Blocks until the shared cadence allows the request, then returns "
            "the grant. The cadence is a FLOOR: the strictest interval any "
            "participant has declared, enforced across every process on this "
            "machine. Passing a smaller `min_interval_s` than the current floor "
            "does not speed anything up.\n\n"
            "Returns an error instead of a grant when:\n"
            " - the resource is in cooldown (a host pushed back) — wait it out, "
            "do not retry sooner, do not change User-Agent or route around it;\n"
            " - the rolling-window request or cost budget is spent;\n"
            " - the queue is deeper than the policy's max_wait_s.\n\n"
            "There is no override. If a limit is genuinely wrong, edit "
            "egress-policy.json."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": (
                        "Blast-radius unit, normally the host "
                        "(e.g. 'forum.effectivealtruism.org'). Everything "
                        "sharing a rate limit must share one id."
                    ),
                },
                "cost": {
                    "type": "number",
                    "description": (
                        "Weight of this request in budget units, default 1. "
                        "Roughly one unit per 50 rows requested: a `limit: 50` "
                        "query is 1, a `limit: 5000` query is 100. Estimate up "
                        "when unsure."
                    ),
                },
                "min_interval_s": {
                    "type": "number",
                    "description": (
                        "Seconds between requests this caller considers safe. "
                        "Only ever raises the shared floor; a value below the "
                        "current floor is ignored."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "What this request is for. Recorded for humans.",
                },
            },
            "required": ["resource"],
        },
    },
    {
        "name": "declare",
        "description": (
            "Register a resource and ratchet its floor cadence up before first "
            "contact. Returns the effective floor, which may be stricter than "
            "what you declared — a slower participant or the policy file wins. "
            "Declaring can never make the shared schedule faster."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Blast-radius unit."},
                "min_interval_s": {
                    "type": "number",
                    "description": (
                        "Minimum seconds between requests. Start from the host's "
                        "published crawl-delay and go slower, not faster."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "What this resource is and why we are touching it.",
                },
                "expected_requests": {
                    "type": "integer",
                    "description": (
                        "Blast radius: total requests this job expects to make. "
                        "Estimate it before starting — 'more than I said' is only "
                        "observable if you said a number."
                    ),
                },
                "expected_cost": {
                    "type": "number",
                    "description": (
                        "Total cost units expected. One unit is roughly 50 rows, so "
                        "count expensive queries at their real weight."
                    ),
                },
                "ceiling_requests": {
                    "type": "integer",
                    "description": "The number this job must not exceed.",
                },
                "pushback": {
                    "type": "string",
                    "description": (
                        "What this resource's pushback looks like (403 interstitial, "
                        "429, quota mail, bounce), so it is recognised rather than "
                        "retried through."
                    ),
                },
            },
            "required": ["resource", "min_interval_s"],
        },
    },
    {
        "name": "record_preflight",
        "description": (
            "Record that bounded-sample invariant checks PASSED for a resource, "
            "unlocking a wider run. Prove the things that silently corrupt a bulk "
            "job on a small sample first: a short page mid-run is not end-of-data, "
            "offset caps, server totals that over-count, empty windows, duplicates "
            "at boundaries. A preflight containing any failing check is refused "
            "rather than stored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Blast-radius unit."},
                "checks": {
                    "type": "array",
                    "description": "The invariants proved, as {name, passed} objects.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "passed": {"type": "boolean"},
                        },
                        "required": ["name", "passed"],
                    },
                },
                "sample_requests": {
                    "type": "integer",
                    "description": "How many requests the bounded sample cost.",
                },
            },
            "required": ["resource", "checks"],
        },
    },
    {
        "name": "status",
        "description": (
            "Report the current floor, cooldown and rolling-window budget "
            "consumption for a resource, without taking a slot. Safe to poll."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Blast-radius unit."},
            },
            "required": ["resource"],
        },
    },
    {
        "name": "record_block",
        "description": (
            "Record that a host pushed back (bot challenge, 403, 429, ban). "
            "Every caller is refused until the cooldown lapses. Call this the "
            "moment pushback is observed, before doing anything else.\n\n"
            "Default cooldown is a next-day retry. Cooldowns only extend, never "
            "shorten. This is the sanctioned response to a block; changing "
            "User-Agent, proxying around it or solving the challenge is not."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Blast-radius unit."},
                "reason": {
                    "type": "string",
                    "description": "What the host did, verbatim where possible.",
                },
                "cooldown_s": {
                    "type": "number",
                    "description": (
                        "Seconds to refuse for. Omit for the policy default "
                        "(next-day retry). A shorter value than the cooldown "
                        "already in force is ignored."
                    ),
                },
            },
            "required": ["resource", "reason"],
        },
    },
]

SERVER_INFO = {"name": "egress-guard", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}


# ----------------------------------------------------------------------
# JSON-RPC plumbing (mirrors mcps/safe-bash/server.py)
# ----------------------------------------------------------------------


def _write_msg(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    """Response on the synchronous main-thread path (init, tools/list, errors)."""
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    _write_msg(out)


def _send_response(msg_id, result=None, error=None) -> None:
    """Response for an in-flight tools/call, or a silent drop if cancelled.

    Per MCP cancellation spec: a receiver SHOULD NOT send a response for a
    cancelled request. The flag check and the write are held under the same
    locks so a cancel cannot slip in between them. Writing inline rather than
    via `_write_msg` is deliberate — `_write_lock` is not reentrant.
    """
    with _write_lock:
        with _in_flight_lock:
            rec = _in_flight.pop(msg_id, None)
            if rec is not None and rec.cancelled:
                return
        out: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _on_cancelled(req_id) -> None:
    with _in_flight_lock:
        rec = _in_flight.get(req_id)
        if rec is not None:
            rec.cancelled = True


def _refusal(text: str) -> dict:
    """A refusal is an error result, never a plain-text note.

    A gate whose "no" looks like ordinary output invites the model to read
    past it. isError makes the refusal structurally distinct from a grant.
    """
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _dispatch_tools_call(msg_id, params, rec) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    body = _TOOL_BODIES.get(name)
    if body is None:
        _send_response(msg_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
        return
    try:
        text = body(arguments)
    except EgressGuardError as e:
        _send_response(msg_id, result=_refusal(f"[egress-guard] REFUSED: {e}"))
    except (ValueError, TypeError) as e:
        _send_response(msg_id, result=_refusal(f"[egress-guard] bad request: {e}"))
    except Exception as e:  # noqa: BLE001 — fail closed on anything unexpected
        _send_response(
            msg_id,
            result=_refusal(
                f"[egress-guard] REFUSED: gate malfunctioned ({type(e).__name__}: {e}). "
                "Treat this as a refusal; do not proceed with the request."
            ),
        )
    else:
        _send_response(msg_id, result={"content": [{"type": "text", "text": text}]})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _respond(
            msg_id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        req_id = params.get("requestId")
        if req_id is not None:
            _on_cancelled(req_id)
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        if msg_id is None:
            sys.stderr.write("[egress-guard] tools/call missing id; dropping\n")
            sys.stderr.flush()
            return
        with _in_flight_lock:
            already = msg_id in _in_flight
        if already:
            _respond(
                msg_id,
                error={"code": -32600, "message": f"Request id {msg_id} already in flight"},
            )
            return
        # Dispatch to a worker so the read loop stays responsive while an
        # `acquire` is parked waiting for its slot — otherwise we could not
        # receive the cancellation for the very request we are waiting on.
        rec = _InFlight()
        with _in_flight_lock:
            _in_flight[msg_id] = rec
        threading.Thread(
            target=_dispatch_tools_call,
            args=(msg_id, params, rec),
            daemon=True,
            name=f"egress-guard-tool-{msg_id}",
        ).start()
        return
    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def _drain_in_flight(timeout_s: float = 120.0) -> None:
    """Let in-flight calls finish after stdin closes.

    Workers are daemon threads, so exiting immediately on EOF would strand a
    caller that writes `init + tools/call` and closes stdin. The cap exceeds
    the largest sane `max_wait_s` so a legitimately parked `acquire` still
    gets to answer.
    """
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        with _in_flight_lock:
            if not _in_flight:
                return
        _time.sleep(0.02)


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
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[egress-guard] unhandled error: {e}\n")
            sys.stderr.flush()
    _drain_in_flight()


if __name__ == "__main__":
    main()

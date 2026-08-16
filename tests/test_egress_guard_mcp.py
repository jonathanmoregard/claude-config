"""End-to-end tests for the egress-guard MCP server.

The gate lives in MCP server code, not in a hook, so these tests drive the
real JSON-RPC stdio protocol exactly as Claude Code would. No network: the
server is pointed at a temp state dir and a temp policy file, and the tools
only ever hand out permission — they never make a request themselves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

from conftest import REPO_ROOT  # noqa: E402

SERVER = os.path.join(REPO_ROOT, "mcps", "egress-guard", "server.py")
RESOURCE = "forum.example.test"


def mcp_session(calls, state_dir, policy, timeout=60):
    """Spawn the server once, run N tools/call requests, return their results.

    STRICTLY SEQUENTIAL: each request is written only after the previous one's
    response has been read. The server dispatches tools/call on worker threads,
    so writing every request up front lets them execute in any order — and the
    behaviour under test here (a floor that ratchets, a budget that accumulates)
    is order-dependent by definition. A real client interleaves request and
    response the same way.
    """
    env = dict(os.environ)
    env["EGRESS_GUARD_STATE"] = str(state_dir)
    env["EGRESS_GUARD_POLICY"] = str(policy)

    stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        text=True,
        env=env,
        bufsize=1,
    )
    responses: dict = {}

    def _stderr() -> str:
        stderr_file.seek(0)
        return stderr_file.read()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read_until(target_id: int) -> dict:
        """Drain stdout until the response for `target_id` arrives."""
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise AssertionError(f"timeout waiting for id={target_id}. stderr={_stderr()}")
            line = proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"server closed stdout before id={target_id}. stderr={_stderr()}"
                )
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                responses[msg["id"]] = msg
                if msg["id"] == target_id:
                    return msg

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "egress-guard-test", "version": "1.0"},
            },
        })
        read_until(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        read_until(2)

        for i, (name, arguments) in enumerate(calls, start=3):
            send({
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            read_until(i)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        stderr_file.close()

    assert 1 in responses, "no initialize response"
    return responses, proc


def result_text(response) -> str:
    return "".join(
        part.get("text", "") for part in response["result"].get("content", [])
    )


def result_json(response) -> dict:
    return json.loads(result_text(response))


def is_error(response) -> bool:
    return bool(response["result"].get("isError"))


# ---------------------------------------------------------------------------


def test_server_advertises_the_gate_tools(state_dir, policy_file):
    responses, _ = mcp_session([], state_dir, policy_file())
    tools = {t["name"]: t for t in responses[2]["result"]["tools"]}
    assert {"acquire", "declare", "status", "record_block"} <= set(tools)


def test_tool_schemas_expose_no_override_knob(state_dir, policy_file):
    """No force/bypass parameter anywhere. Widening a limit is a policy edit."""
    banned = {
        "force", "override", "bypass", "skip", "skip_floor", "ignore",
        "ignore_floor", "no_wait", "nowait", "allow", "unsafe", "emergency",
        "urgent", "priority",
    }
    responses, _ = mcp_session([], state_dir, policy_file())
    for tool in responses[2]["result"]["tools"]:
        props = set((tool["inputSchema"].get("properties") or {}).keys())
        assert not (props & banned), (tool["name"], props & banned)


def test_acquire_grants_and_reports_the_floor(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.05})
    responses, _ = mcp_session(
        [("acquire", {"resource": RESOURCE, "cost": 1})], state_dir, policy
    )
    payload = result_json(responses[3])
    assert is_error(responses[3]) is False
    assert payload["granted"] is True
    assert payload["floor_s"] == pytest.approx(0.05)


def test_acquire_enforces_the_floor_across_calls(state_dir, policy_file):
    """Second acquisition in the same session waits out the shared cadence."""
    policy = policy_file(defaults={"min_interval_s": 0.4, "max_wait_s": 30.0})
    responses, _ = mcp_session(
        [
            ("acquire", {"resource": RESOURCE, "min_interval_s": 5.0}),
            ("acquire", {"resource": RESOURCE, "min_interval_s": 0.0}),
        ],
        state_dir,
        policy,
    )
    first = result_json(responses[3])
    second = result_json(responses[4])
    # A caller declaring 0.0 inherits the 5.0s floor the first caller declared.
    assert second["floor_s"] == pytest.approx(5.0)
    # The guarantee is on the RESERVED slot: it is assigned under the lock, so
    # it is exact and is what spaces concurrent processes.
    assert second["slot_at"] - first["slot_at"] >= 5.0 - 1e-9
    # Wall-clock spacing inherits lock + IPC jitter on the un-waited first call
    # (its slot is stamped a few ms before it returns), so allow for that.
    assert second["granted_at"] - first["granted_at"] >= 5.0 - 0.05
    assert second["waited_s"] >= 5.0 - 0.05


def test_cooldown_makes_acquire_an_error_not_a_grant(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0})
    responses, _ = mcp_session(
        [
            ("record_block", {"resource": RESOURCE, "reason": "bot challenge"}),
            ("acquire", {"resource": RESOURCE}),
        ],
        state_dir,
        policy,
    )
    assert is_error(responses[4]) is True
    text = result_text(responses[4])
    assert "REFUSED" in text
    assert "cooldown" in text.lower()
    assert "bot challenge" in text


def test_budget_exhaustion_is_an_error_not_a_grant(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_requests": 2})
    calls = [("acquire", {"resource": RESOURCE}) for _ in range(3)]
    responses, _ = mcp_session(calls, state_dir, policy)

    assert is_error(responses[3]) is False
    assert is_error(responses[4]) is False
    assert is_error(responses[5]) is True
    assert "budget" in result_text(responses[5]).lower()


def test_cost_weighting_is_honoured_over_the_wire(state_dir, policy_file):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "max_requests": 1000, "max_cost": 100.0}
    )
    responses, _ = mcp_session(
        [
            ("acquire", {"resource": RESOURCE, "cost": 100}),
            ("acquire", {"resource": RESOURCE, "cost": 1}),
        ],
        state_dir,
        policy,
    )
    assert is_error(responses[3]) is False
    assert is_error(responses[4]) is True
    assert "cost" in result_text(responses[4]).lower()


def test_status_reports_budget_consumption(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_cost": 500.0})
    responses, _ = mcp_session(
        [
            ("acquire", {"resource": RESOURCE, "cost": 7}),
            ("status", {"resource": RESOURCE}),
        ],
        state_dir,
        policy,
    )
    st = result_json(responses[4])
    assert st["requests_used"] == 1
    assert st["cost_used"] == pytest.approx(7.0)
    assert st["max_cost"] == pytest.approx(500.0)


def test_declare_ratchets_the_floor_up_only(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0})
    responses, _ = mcp_session(
        [
            ("declare", {"resource": RESOURCE, "min_interval_s": 9.0}),
            ("declare", {"resource": RESOURCE, "min_interval_s": 0.1}),
            ("status", {"resource": RESOURCE}),
        ],
        state_dir,
        policy,
    )
    assert result_json(responses[4])["floor_s"] == pytest.approx(9.0)
    assert result_json(responses[5])["floor_s"] == pytest.approx(9.0)


def test_malformed_policy_refuses_egress(state_dir, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    responses, _ = mcp_session(
        [("acquire", {"resource": RESOURCE})], state_dir, str(bad)
    )
    assert is_error(responses[3]) is True
    assert "policy" in result_text(responses[3]).lower()


def test_unknown_tool_is_rejected(state_dir, policy_file):
    responses, _ = mcp_session(
        [("definitely_not_a_tool", {})], state_dir, policy_file()
    )
    assert "error" in responses[3]


def test_missing_resource_argument_is_an_error(state_dir, policy_file):
    responses, _ = mcp_session([("acquire", {})], state_dir, policy_file())
    assert is_error(responses[3]) is True
    assert "resource" in result_text(responses[3]).lower()


def test_shipped_policy_file_is_valid_json_and_conservative():
    """The policy that ships with the server must parse and must not be permissive."""
    path = os.path.join(REPO_ROOT, "mcps", "egress-guard", "egress-policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    defaults = doc["defaults"]
    assert defaults["min_interval_s"] >= 2.0
    assert defaults["max_requests"] <= 1000
    assert defaults["max_cost"] > 0
    for name, override in (doc.get("resources") or {}).items():
        assert override.get("min_interval_s", defaults["min_interval_s"]) > 0, name

#!/usr/bin/env python3
"""runtime/mcps/artl-verdict/server.py: the advisor's verdict arrives through a
schema-checked tool call, never as free text.

Each test names the production change that would make it fail.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SERVER = REPO / "runtime" / "mcps" / "artl-verdict" / "server.py"
SCHEMA = REPO / "runtime" / "skills" / "advice-refine-test-loop" / "verdict-schema.json"

FINDING = {"severity": "HIGH", "autofix_class": "manual", "file": "/abs/a.py", "line": 3,
           "rationale": "returns early on a missing token", "unverifiable": False}
BLOCKS = {"verdict": "BLOCKS", "verdict_reason": "fails open", "findings": [FINDING]}


def session(home: Path, *messages: dict) -> list[dict]:
    """Run the server over stdio with HOME=home; return its responses by id."""
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"}, *messages]
    out = subprocess.run([sys.executable, str(SERVER)],
                         input="".join(json.dumps(m) + "\n" for m in lines),
                         capture_output=True, text=True, timeout=30,
                         env={**os.environ, "HOME": str(home)}, check=True)
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def submit(home: Path, verdict: object) -> dict:
    responses = session(home, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "submit_verdict", "arguments": verdict}})
    return next(r for r in responses if r.get("id") == 1)["result"]


def text(result: dict) -> str:
    return "".join(b["text"] for b in result["content"] if b.get("type") == "text")


# Break: a tool schema copied into the server drifts from verdict-schema.json.
def test_tool_input_schema_is_the_verdict_schema(tmp_path):
    responses = session(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = next(r for r in responses if r.get("id") == 1)["result"]["tools"]
    assert [t["name"] for t in tools] == ["submit_verdict"]
    assert tools[0]["inputSchema"] == json.loads(SCHEMA.read_text())


# Break: an accepted verdict that leaves the parent nothing to read.
def test_valid_verdict_is_accepted_and_saved_where_the_ack_says(tmp_path):
    result = submit(tmp_path, BLOCKS)
    assert not result.get("isError"), text(result)
    saved = [p for p in (tmp_path / ".local/state/artl-verdicts").iterdir()]
    assert len(saved) == 1 and json.loads(saved[0].read_text()) == BLOCKS
    assert str(saved[0]) in text(result)
    assert "BLOCKS, 1 finding" in text(result)


def test_pass_with_no_findings_is_accepted(tmp_path):
    result = submit(tmp_path, {"verdict": "PASS", "verdict_reason": "", "findings": []})
    assert not result.get("isError"), text(result)


def _without(key):
    v = copy.deepcopy(BLOCKS)
    del v[key]
    return v


def _finding(**kw):
    v = copy.deepcopy(BLOCKS)
    v["findings"][0].update(kw)
    return v


# Break: any one schema constraint going unenforced; the error must say what to
# fix so the advisor can resubmit.
@pytest.mark.parametrize("verdict,needle", [
    (_without("verdict_reason"), "verdict_reason"),
    ({**BLOCKS, "verdict": "FAIL"}, "verdict"),
    ({**BLOCKS, "notes": "x"}, "notes"),
    (_finding(severity="critical"), "severity"),
    (_finding(line="3"), "line"),
    (_finding(line=-1), "line"),
    (_finding(line=True), "line"),
    (_finding(unverifiable="no"), "unverifiable"),
    (_finding(rationale="x" * 241), "rationale"),
    (_finding(extra=1), "extra"),
    ({"verdict": "PASS", "verdict_reason": "", "findings": [FINDING]}, "PASS"),
    ({"verdict": "BLOCKS", "verdict_reason": "x", "findings": []}, "BLOCKS"),
    ({**BLOCKS, "verdict": "PASS_WITH_FIXES"}, "PASS_WITH_FIXES"),
    ({"verdict": "BLOCKS", "verdict_reason": "x",
      "findings": [dict(FINDING, severity="LOW")]}, "BLOCKS"),
])
def test_invalid_verdict_is_rejected_with_the_reason(tmp_path, verdict, needle):
    result = submit(tmp_path, verdict)
    assert result.get("isError") is True
    assert needle in text(result)
    assert not (tmp_path / ".local/state/artl-verdicts").exists() or \
        not any((tmp_path / ".local/state/artl-verdicts").iterdir())


# Break: a schema keyword the hand validator does not know passes silently.
def test_unknown_schema_keyword_fails_loudly():
    sys.path.insert(0, str(REPO / "runtime" / "hooks"))
    import artl_verdict
    with pytest.raises(ValueError):
        artl_verdict.schema_problems({"x": 1}, {"type": "object", "patternProperties": {}})


# Break: the checker drifting from the schema it claims to interpret: every
# keyword the real schema uses must be one it enforces or ignores as prose.
def test_the_real_schema_is_fully_understood():
    sys.path.insert(0, str(REPO / "runtime" / "hooks"))
    import artl_verdict
    assert artl_verdict.problems(BLOCKS) == []

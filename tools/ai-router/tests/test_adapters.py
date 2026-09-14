"""Adapter argv builders, event parsers, error classification and sanitiser
(contract §Jobs: adapters.py, sanitize.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_router import adapters, sanitize
from ai_router.adapters import (PROMPT_PLACEHOLDER, DangerousFlag, build_argv, build_resume_argv,
                                classify, final_message, materialize, refuse_dangerous,
                                resolve_binary, summarize_event)


# --- sanitize -----------------------------------------------------------------

def test_sanitize_strips_csi_osc_c1_and_caps_lines():
    raw = "\x1b[31mred\x1b[0m \x1b]0;title\x07tail \x1b]8;;http://x\x1b\\link\x9bZ\x00nul"
    assert sanitize.clean(raw) == "red tail linknul"
    long = "a" * 5000
    out = sanitize.clean(long + "\nshort")
    first, second = out.split("\n")
    assert len(first) == 4000 and first.endswith("…") and second == "short"


# --- binaries -----------------------------------------------------------------

def test_resolve_binary_prefers_env_override(tmp_path: Path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert resolve_binary("codex", {"AI_ROUTER_CODEX_BIN": str(fake)}) == str(fake)
    assert resolve_binary("codex", {"AI_ROUTER_CODEX_BIN": str(tmp_path / "missing"), "PATH": ""}) is None
    assert resolve_binary("claude", {"HOME": str(tmp_path), "PATH": ""}) is None


# --- argv ---------------------------------------------------------------------

def test_codex_argv_shape(tmp_path: Path):
    argv = build_argv("codex", "/bin/codex", model="gpt-5.6-luna", effort="low",
                      sandbox="read-only", cwd="/work", schema=None, bare=True, job_dir=tmp_path)
    assert argv[:3] == ["/bin/codex", "exec", "--json"]
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=low" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == "/work"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-o") + 1] == str(tmp_path / "last_message.md")
    assert argv[-2:] == ["--", PROMPT_PLACEHOLDER]
    assert "--output-schema" not in argv


def test_codex_argv_with_schema(tmp_path: Path):
    schema = tmp_path / "s.json"
    schema.write_text("{}")
    argv = build_argv("codex", "/bin/codex", model="gpt-5.6-sol", effort="high",
                      sandbox="workspace-write", cwd="/w", schema=str(schema), bare=True, job_dir=tmp_path)
    assert argv[argv.index("--output-schema") + 1] == str(schema)
    assert argv[-2:] == ["--", PROMPT_PLACEHOLDER]


def test_claude_argv_read_only_and_bare(tmp_path: Path):
    argv = build_argv("claude", "/bin/claude", model="claude-haiku-4-5-20251001", effort="low",
                      sandbox="read-only", cwd="/w", schema=None, bare=True, job_dir=tmp_path)
    assert argv[:5] == ["/bin/claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    # OAuth-safe "bare": --bare itself never reads OAuth creds (claude --help),
    # so lean mode is expressed as no settings + no MCP.
    assert "--bare" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[-3:] == ["--max-turns", "60", PROMPT_PLACEHOLDER]


def test_claude_argv_workspace_write_no_bare_with_schema(tmp_path: Path):
    schema = tmp_path / "s.json"
    schema.write_text('{"type":"object"}')
    argv = build_argv("claude", "/bin/claude", model="claude-sonnet-5", effort=None,
                      sandbox="workspace-write", cwd="/w", schema=str(schema), bare=False, job_dir=tmp_path)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep,Edit,Write,MultiEdit,Bash"
    assert argv[argv.index("--disallowedTools") + 1] == "Agent"
    assert argv[argv.index("--json-schema") + 1] == '{"type":"object"}'
    assert "--effort" not in argv
    assert "--setting-sources" not in argv and "--strict-mcp-config" not in argv
    # arftl-allow: permission-widened — asserting these flags are ABSENT from the argv
    for bad in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--bare"):
        assert bad not in argv


def test_materialize_substitutes_prompt_once():
    argv = ["/bin/x", "--", PROMPT_PLACEHOLDER]
    assert materialize(argv, "hello\nworld") == ["/bin/x", "--", "hello\nworld"]
    assert argv[-1] == PROMPT_PLACEHOLDER  # input untouched


# arftl-allow: permission-widened — the flags the adapter must REFUSE; the test below asserts DangerousFlag is raised
DANGEROUS_FLAGS = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--full-auto", "--yolo"]


@pytest.mark.parametrize("flag", DANGEROUS_FLAGS)
def test_refuse_dangerous_flags(flag: str):
    with pytest.raises(DangerousFlag):
        refuse_dangerous(["/bin/codex", "exec", flag, "--", "p"])
    refuse_dangerous(["/bin/codex", "exec", "--", "prompt mentioning --yolo is fine"])


def test_resume_argv():
    c = build_resume_argv("codex", "/bin/codex", thread_id="t-1", model="gpt-5.6-luna", effort="low",
                          sandbox="read-only", cwd="/w", bare=True)
    assert c[:4] == ["/bin/codex", "exec", "resume", "t-1"] and c[-2:] == ["--", PROMPT_PLACEHOLDER]
    assert "--json" in c and "-m" in c
    a = build_resume_argv("claude", "/bin/claude", thread_id="s-1", model="claude-sonnet-5", effort=None,
                          sandbox="read-only", cwd="/w", bare=True)
    assert a[a.index("--resume") + 1] == "s-1" and a[-1] == PROMPT_PLACEHOLDER


# --- events -------------------------------------------------------------------

def codex_events() -> list[dict]:
    return [
        {"type": "thread.started", "thread_id": "th-123"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "\x1b[1mOK\x1b[0m"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]


def claude_events() -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "session_id": "sess-9", "model": "claude-haiku-4-5-20251001"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "data"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "OK",
         "session_id": "sess-9", "total_cost_usd": 0.001, "usage": {"input_tokens": 5}},
    ]


def test_codex_final_message_ids_usage():
    ev = codex_events()
    assert final_message("codex", ev, None) == "OK"
    assert adapters.conversation_id("codex", ev) == "th-123"
    assert adapters.usage("codex", ev) == {"input_tokens": 10, "output_tokens": 2}
    assert adapters.latest_agent_text("codex", ev) == "OK"


def test_codex_last_message_file_wins(tmp_path: Path):
    p = tmp_path / "last_message.md"
    p.write_text("from file\n")
    assert final_message("codex", codex_events(), p) == "from file"


def test_claude_final_message_ids_usage():
    ev = claude_events()
    assert final_message("claude", ev, None) == "OK"
    assert adapters.conversation_id("claude", ev) == "sess-9"
    assert adapters.usage("claude", ev) == {"input_tokens": 5, "total_cost_usd": 0.001}
    assert adapters.latest_agent_text("claude", ev) == "OK"


def test_summarize_event_lines():
    lines = [summarize_event("codex", e) for e in codex_events()]
    assert lines[0] == "thread.started th-123"
    assert "agent_message: OK" in lines[4]
    assert "\x1b" not in lines[4]
    cl = [summarize_event("claude", e) for e in claude_events()]
    assert cl[0] == "system init session=sess-9 model=claude-haiku-4-5-20251001"
    assert cl[2] == "tool_use Read"
    assert cl[-1].startswith("result success")


# --- classification ---------------------------------------------------------------

def test_classify_done_requires_zero_exit_and_message():
    assert classify("codex", 0, codex_events(), "") == "done"
    assert classify("claude", 0, claude_events(), "") == "done"


def test_classify_quota_on_either_stream():
    assert classify("codex", 1, [{"type": "error", "message": "You've hit your usage limit"}], "") == "quota"
    assert classify("codex", 1, [], "HTTP 429 Too Many Requests") == "quota"
    assert classify("claude", 1, [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                                   "result": "You've used all your weekly limit"}], "") == "quota"
    assert classify("claude", 1, [], "rate_limit_error: slow down") == "quota"
    # Quota beats reject when both match (429 is a quota error, not a rejection)
    assert classify("codex", 1, [], 'unexpected status 429 rate limit') == "quota"


def test_classify_rejected_only_on_nonzero_exit():
    stderr = 'unexpected status 400 Bad Request: {"error": {"message": "model_not_found"}}'
    assert classify("codex", 1, [], stderr) == "rejected"
    assert classify("codex", 1, [{"type": "turn.failed", "error": {"message": "gpt-5.6-sol is not supported when using Codex with a ChatGPT account"}}], "") == "rejected"
    # Successful run merely discussing a 4xx is not a rejection
    ok_run = [{"type": "item.completed", "item": {"type": "agent_message", "text": "saw HTTP 404 once"}},
              {"type": "turn.completed", "usage": {}}]
    assert classify("codex", 0, ok_run, "") == "done"


def test_classify_overloaded_unparseable_error():
    assert classify("claude", 1, [], "API overloaded (529)") == "overloaded"
    assert classify("codex", 0, [{"type": "turn.completed", "usage": {}}], "") == "unparseable"
    assert classify("codex", 0, [], "not json at all") == "unparseable"
    assert classify("claude", 1, [{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "x"}], "") == "error"
    assert classify("codex", 1, [], "boom") == "error"


def test_parse_event_line_tolerates_garbage():
    assert adapters.parse_event_line('{"type":"turn.started"}') == {"type": "turn.started"}
    assert adapters.parse_event_line("garbage") is None
    assert adapters.parse_event_line("") is None
    assert adapters.parse_event_line(json.dumps([1, 2])) is None

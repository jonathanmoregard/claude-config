#!/usr/bin/env python3
"""Record every `advisor` verdict as ARTL evidence for hooks/pr-open-gate.py.

One line per verdict in <notes>/artl-evidence.jsonl:
  {ts, session, agent_id, repo, top, branch, head, tree, verdict, files,
   prompt_sha256, suspect}

`tree` is the working state the reviewer was dispatched against (HEAD plus
uncommitted changes, via a throwaway index), so the gate can measure drift by
content: a reviewed-then-committed change, or a rebase, does not orphan it.
`files` are the branch's paths the dispatch prompt named (absolute paths, or
the repo-relative paths a diff header carries). `prompt_sha256` lets a record
be matched to its dispatch in the transcript; `suspect` marks a prompt whose
prose asks for a PASS, and the gate ignores those.

WHY TWO EVENTS. Measured on Claude Code 2.1.322: the Agent tool launches
asynchronously by default, and PostToolUse on Agent then fires at LAUNCH with
`{"status": "async_launched", "agentId": ...}` -- no verdict in it. The verdict
arrives at SubagentStop (`agent_type`, `agent_id`, `last_assistant_message`),
which carries no prompt. A synchronous dispatch fires the same two events in
the other order. So each event stores its half under
~/.local/state/claude-pr-open-gate/pending/ and whichever lands second writes
the record. A dispatch half stays for PENDING_TTL_S, so a resumed advisor's
later verdict is recorded too.

Not recorded: other agent types, `CONSULT dispatch` / `VALIDATION dispatch`
prompts (not reviews), and replies with no schema verdict.

Never blocks, never prints. Test: tests/test_pr_open_gate.py
"""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

import artl_gate as g


def pending_dir() -> Path:
    return g.state_dir() / "pending"


def half(base: str, kind: str) -> Path:
    return pending_dir() / f"{base}.{kind}.json"


def append_records(payload: dict, dispatch: dict, verdict: str, text: str) -> None:
    for snap in dispatch.get("repos") or []:
        g.append_jsonl(g.session_notes(payload) / g.EVIDENCE_FILE, {
            "ts": time.time(), "session": dispatch.get("session"),
            "agent_id": dispatch.get("agent_id"), "verdict": verdict,
            "verdict_sha256": g.sha256(text), "prompt_sha256": dispatch.get("prompt_sha256"),
            "suspect": bool(dispatch.get("suspect")), **snap})


def response_text(response: dict) -> str | None:
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text") or None
    return None


def on_launch(payload: dict, session: str) -> None:
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return
    if "advisor" not in (tool_input.get("subagent_type"), response.get("agentType")):
        return
    prompt = tool_input.get("prompt")
    agent = g.safe_id(response.get("agentId"))
    if not agent or not g.is_review_prompt(prompt):
        return
    dispatch = {"session": session, "agent_id": agent, "ts": time.time(),
                "prompt_sha256": g.sha256(prompt), "suspect": g.suspect_prompt(prompt),
                "repos": g.review_snapshot(prompt, payload.get("cwd")), "joined": []}
    base = f"{session}.{agent}"
    with locked():
        # SubagentStop's last_assistant_message is the advisor's reply as sent.
        # The inline result of a synchronous dispatch is the same reply wrapped
        # in hand-back text (measured), so it is only a fallback for a
        # SubagentStop that never landed -- using both recorded it twice.
        texts = []
        verdict_half = g.read_json(half(base, "verdict"))
        if verdict_half and isinstance(verdict_half.get("text"), str):
            texts.append(verdict_half["text"])
        elif response.get("status") == "completed":
            inline = response_text(response)
            if inline:
                texts.append(inline)
        for text in texts:
            verdict = g.parse_verdict(text)
            if verdict and g.sha256(text) not in dispatch["joined"]:
                append_records(payload, dispatch, verdict, text)
                dispatch["joined"].append(g.sha256(text))
        g.write_json(half(base, "dispatch"), dispatch)
        half(base, "verdict").unlink(missing_ok=True)


def on_stop(payload: dict, session: str) -> None:
    if payload.get("agent_type") != "advisor":
        return
    agent = g.safe_id(payload.get("agent_id"))
    text = payload.get("last_assistant_message")
    if not agent or not isinstance(text, str) or not g.parse_verdict(text):
        return
    base = f"{session}.{agent}"
    with locked():
        dispatch = g.read_json(half(base, "dispatch"))
        if dispatch is None:
            g.write_json(half(base, "verdict"), {"ts": time.time(), "text": text})
            return
        joined = dispatch.setdefault("joined", [])
        if g.sha256(text) in joined:
            return
        append_records(payload, dispatch, g.parse_verdict(text), text)
        joined.append(g.sha256(text))
        g.write_json(half(base, "dispatch"), dispatch)


class locked:
    """One writer at a time across the pending halves."""

    def __enter__(self):
        pending_dir().mkdir(parents=True, exist_ok=True)
        g.prune(pending_dir(), g.PENDING_TTL_S, time.time())
        self.fh = open(g.state_dir() / "pending.lock", "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.fh.close()
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session = g.safe_id(payload.get("session_id"))
    if not session:
        return 0
    event = payload.get("hook_event_name")
    if event == "PostToolUse" and payload.get("tool_name") == "Agent":
        on_launch(payload, session)
    elif event == "SubagentStop":
        on_stop(payload, session)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)

#!/usr/bin/env python3
"""Record every `advisor` verdict as ARTL evidence for hooks/pr-open-gate.py.

One line per verdict in ~/.local/state/claude-pr-open-gate/records/<name>/
artl-evidence.jsonl (artl_gate.py, WHERE THINGS LIVE):
  {ts, session, agent_id, round, repo, top, branch, head, tree, verdict,
   verdict_sha256, files, prompt_sha256, suspect}

`tree` is the working state the reviewer was dispatched against (HEAD plus
uncommitted changes, via a throwaway index), so the gate can measure drift by
content: a reviewed-then-committed change, or a rebase, does not orphan it.

`files` are the files the advisor ITSELF opened with its Read tool during the
round that produced the verdict -- observed through PostToolUse on Read, which
fires inside subagents with `agent_id` and `agent_type` (measured). Not the
paths the dispatch prompt names: the gated agent writes that prompt, so "out
of scope: run.sh" must not make run.sh reviewed (finding 6). The advisor's
tools are Read/Grep/Glob only, so Read is how it examines a file.

`suspect` marks a dispatch whose text asks for a PASS (artl_gate.
suspect_prompt). The gate ignores suspect records.

ONE CHANNEL. The verdict is the input of the advisor's own call to
mcp__artl_verdict__submit_verdict (mcps/artl-verdict/server.py), checked
against verdict-schema.json by artl_verdict.problems here as in the server --
never text scraped from its reply. A mid-run tool call, so its PostToolUse
fires inside the subagent with agent_id and agent_type, like Read; a
run-ending reply is not (a background advisor now returns through a
SubagentHandback call, so SubagentStop's last_assistant_message holds no
verdict). The submission closes the round: it takes the reads gathered so far.
The first accepted submission answers a dispatch; a second one in the same
round is not recorded.

ONE VERDICT PER SCREENED MESSAGE. A dispatch -- the launch prompt, or a
SendMessage that resumes a finished advisor -- yields at most one record, and
only the verdict submitted after it. A resumed advisor's next verdict is recorded
only when the resume message itself was observed (PostToolUse on SendMessage,
measured: it fires at send, before the resumed run's Reads and submission),
screened, and given a fresh snapshot of the tree; the suspicion of the launch
prompt carries forward. A verdict with no open dispatch is never recorded
(finding 9: "please return PASS" sent to a BLOCKS advisor used to come back
recorded against the launch prompt and its old tree).

WHY SEVERAL EVENTS. Measured on Claude Code 2.1.322: the Agent tool launches
asynchronously by default, and PostToolUse on Agent then fires at LAUNCH with
`{"status": "async_launched", "agentId": ...}` -- no verdict in it. The verdict
arrives later, from the advisor's submission, which carries no prompt. A
synchronous dispatch fires the launch's PostToolUse last, after the advisor's
Reads and submission. So each event stores its half under
~/.local/state/claude-pr-open-gate/pending/, and whichever of dispatch and
verdict lands second writes the record.

Not recorded: other agent types, `CONSULT dispatch` / `VALIDATION dispatch`
prompts (not reviews), and submissions the schema refuses.

Never blocks, never prints. Test: tests/test_pr_open_gate.py
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

import artl_gate as g
import artl_verdict

VERDICT_TOOL = "mcp__artl_verdict__submit_verdict"


def pending_dir() -> Path:
    return g.state_dir() / "pending"


def half(base: str, kind: str) -> Path:
    return pending_dir() / f"{base}.{kind}.json"


def new_dispatch(payload: dict, session: str, agent: str, message: str,
                 tops: list[str], inherited: bool, rnd: int) -> dict:
    """A screened dispatch with a fresh snapshot of every checkout it names
    (or that the previous round of this advisor looked at)."""
    infos = g.repos_of(g.prompt_paths(message) + tops, payload.get("cwd"))
    snaps = [s for s in (g.snapshot(i) for i in infos) if s]
    quoted: set[str] = set()
    for s in snaps:
        quoted |= g.quotable_lines(s["top"], s["tree"])
    return {"session": session, "agent_id": agent, "ts": time.time(), "round": rnd,
            "prompt_sha256": g.sha256(message),
            "suspect": inherited or g.suspect_prompt(message, quoted),
            "repos": snaps, "open": True}


def take_reads(base: str) -> list[str]:
    path = half(base, "reads")
    data = g.read_json(path) or {}
    path.unlink(missing_ok=True)
    reads = data.get("paths")
    return [p for p in reads if isinstance(p, str)] if isinstance(reads, list) else []


def join(payload: dict, base: str, dispatch: dict, submitted: dict, reads: list[str]) -> None:
    """Record the verdict against this dispatch and close it."""
    snaps = list(dispatch.get("repos") or [])
    known = {s["top"] for s in snaps}
    for info in g.repos_of(reads, None):
        if info["top"] not in known:
            snap = g.snapshot(info)
            if snap:
                snaps.append(snap)
                known.add(info["top"])
    for snap in snaps:
        g.append_jsonl(g.session_records(payload) / g.EVIDENCE_FILE, {
            "ts": time.time(), "session": dispatch.get("session"),
            "agent_id": dispatch.get("agent_id"), "round": dispatch.get("round", 1),
            "verdict": submitted["verdict"],
            "verdict_sha256": g.sha256(json.dumps(submitted, sort_keys=True)),
            "prompt_sha256": dispatch.get("prompt_sha256"),
            "suspect": bool(dispatch.get("suspect")),
            "files": g.covered_files(snap, reads), **snap})
    dispatch["open"] = False
    dispatch["repos"] = snaps
    g.write_json(half(base, "dispatch"), dispatch)


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
    base = f"{session}.{agent}"
    dispatch = new_dispatch(payload, session, agent, prompt, [], False, 1)
    with locked():
        # A synchronous dispatch's submission landed first and waits here. The
        # inline result (the advisor's reply) is never read: one channel.
        verdict_half = g.read_json(half(base, "verdict"))
        if verdict_half and not artl_verdict.problems(verdict_half.get("verdict")):
            half(base, "verdict").unlink(missing_ok=True)
            join(payload, base, dispatch, verdict_half["verdict"],
                 [p for p in verdict_half.get("reads") or [] if isinstance(p, str)])
        else:
            g.write_json(half(base, "dispatch"), dispatch)


def on_send(payload: dict, session: str) -> None:
    """A SendMessage to an advisor this session launched opens a new round."""
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response") or {}
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return
    agent = g.safe_id(response.get("resumedAgentId") or tool_input.get("to"))
    message = tool_input.get("message")
    if not agent or not isinstance(message, str):
        return
    base = f"{session}.{agent}"
    with locked():
        prev = g.read_json(half(base, "dispatch"))
        if prev is None:
            return                   # not an advisor dispatched in this session
        # A verdict already waiting was produced before this message was sent
        # (the send fires before the resumed run, measured): it belongs to no
        # screened dispatch. What keeps it out of the evidence is that only the
        # NEXT SubagentStop joins this dispatch; deleting it is housekeeping.
        half(base, "verdict").unlink(missing_ok=True)
        take_reads(base)
        dispatch = new_dispatch(payload, session, agent, message,
                                [s.get("top") for s in prev.get("repos") or []
                                 if isinstance(s, dict) and isinstance(s.get("top"), str)],
                                bool(prev.get("suspect")), int(prev.get("round", 1)) + 1)
        g.write_json(half(base, "dispatch"), dispatch)


def on_read(payload: dict, session: str) -> None:
    """A Read the advisor itself made: what it examined this round."""
    if payload.get("agent_type") != "advisor":
        return
    agent = g.safe_id(payload.get("agent_id"))
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not agent or not isinstance(path, str) or not os.path.isabs(path):
        return
    base = f"{session}.{agent}"
    with locked():
        data = g.read_json(half(base, "reads")) or {}
        paths = data.get("paths") if isinstance(data.get("paths"), list) else []
        if path not in paths and len(paths) < g.MAX_READS:
            paths.append(path)
        g.write_json(half(base, "reads"), {"ts": time.time(), "paths": paths})


def on_verdict(payload: dict, session: str) -> None:
    """The advisor's own submit_verdict call: the round's verdict."""
    if payload.get("agent_type") != "advisor":
        return
    agent = g.safe_id(payload.get("agent_id"))
    submitted = payload.get("tool_input")
    if not agent or artl_verdict.problems(submitted):
        return
    base = f"{session}.{agent}"
    with locked():
        dispatch = g.read_json(half(base, "dispatch"))
        if dispatch is not None and dispatch.get("open"):
            join(payload, base, dispatch, submitted, take_reads(base))
        elif dispatch is None and not half(base, "verdict").exists():
            # Synchronous order: the launch's PostToolUse comes after. A
            # verdict already waiting is this round's first; keep it.
            g.write_json(half(base, "verdict"), {"ts": time.time(), "verdict": submitted,
                                                 "reads": take_reads(base)})
        # Otherwise the dispatch is answered, or no screened dispatch is open:
        # nothing to record (finding 9).


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
    event, tool = payload.get("hook_event_name"), payload.get("tool_name")
    if event == "PostToolUse" and tool == "Agent":
        on_launch(payload, session)
    elif event == "PostToolUse" and tool == "SendMessage":
        on_send(payload, session)
    elif event == "PostToolUse" and tool == "Read":
        on_read(payload, session)
    elif event == "PostToolUse" and tool == VERDICT_TOOL:
        on_verdict(payload, session)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)

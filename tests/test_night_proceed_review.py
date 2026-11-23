"""Reproducers for review findings on night-proceed.

The round-1 detector reproducers (N3a/N3b/N7a) tested regex shapes; the regex
is gone (model classifier, decisions 2026-11-23), and its plumbing is covered in
test_night_proceed.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from test_night_proceed import HOOK, PYTHON, ask, fake_gdbus, run


def test_n4_headless_sdk_session_is_not_blocked(tmp_path: Path) -> None:
    # No lock seam (the seam marks an e2e run): a fake gdbus reports "locked".
    out = run(tmp_path, ask("Which one?"), lock=None,
              extra={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "PATH": fake_gdbus(tmp_path, "(true,)")})
    assert out is None, f"headless session was denied: {out}"
    assert run(tmp_path, ask("Which one?"), lock=None,
               extra={"CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": fake_gdbus(tmp_path, "(true,)")})


def parallel_asks(tmp_path: Path, sid: str, qs: list[str],
                  prompt_id: str | None) -> list[str]:
    """Two AskUserQuestion hooks for one session at once; their stdouts."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_NIGHT_PROCEED") and k != "CLAUDE_CODE_ENTRYPOINT"}
    env.update(HOME=str(tmp_path), CLAUDE_NIGHT_PROCEED_TEST_NOW="02:00",
               CLAUDE_NIGHT_PROCEED_TEST_LOCK="locked")
    procs = []
    for q in qs:
        payload = {"hook_event_name": "PreToolUse", "session_id": sid,
                   "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": q}]}}
        if prompt_id is not None:
            payload["prompt_id"] = prompt_id
        procs.append(subprocess.Popen([PYTHON, "-S", "-E", str(HOOK)], env=env, text=True,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE))
        procs[-1].stdin.write(json.dumps(payload)); procs[-1].stdin.close()
    outs = [p.stdout.read() for p in procs]
    for p in procs:
        p.wait()
    return outs


def test_n6_parallel_asks_in_one_turn_get_one_reply(tmp_path: Path) -> None:
    """Real payloads carry prompt_id: one night reply per user turn, so of two
    parallel asks exactly one is denied and the other reaches the user."""
    problems: list[str] = []
    for rnd in range(20):
        outs = parallel_asks(tmp_path, f"turn{rnd}", [f"one {rnd}?", f"two {rnd}?"], f"p{rnd}")
        denied = sum(1 for o in outs if o.strip())
        if denied != 1:
            problems.append(f"round {rnd}: {denied} denies")
    assert not problems, problems


def test_n6_concurrent_asks_same_session_share_tmp(tmp_path: Path) -> None:
    """Without prompt_id the per-question digests still survive concurrent writes."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_NIGHT_PROCEED") and k != "CLAUDE_CODE_ENTRYPOINT"}
    env.update(HOME=str(tmp_path), CLAUDE_NIGHT_PROCEED_TEST_NOW="02:00",
               CLAUDE_NIGHT_PROCEED_TEST_LOCK="locked")
    problems: list[str] = []
    for rnd in range(30):
        sid = f"sess{rnd}"
        qs = [f"question one round {rnd}?", f"question two round {rnd}?"]
        procs = []
        for q in qs:
            payload = {"hook_event_name": "PreToolUse", "session_id": sid,
                       "tool_name": "AskUserQuestion",
                       "tool_input": {"questions": [{"question": q}]}}
            procs.append(subprocess.Popen([PYTHON, "-S", "-E", str(HOOK)], env=env, text=True,
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE))
            procs[-1].stdin.write(json.dumps(payload)); procs[-1].stdin.close()
        outs = [(p.stdout.read(), p.wait()) for p in procs]
        for i, (o, _) in enumerate(outs):
            if not o.strip():
                problems.append(f"round {rnd}: run {i} no deny")
        f = tmp_path / ".claude" / "tmp" / "night-proceed" / f"{sid}.json"
        seen = json.loads(f.read_text()) if f.exists() else []
        for q in qs:
            key = "ask:" + json.dumps({"questions": [{"question": q}]}, sort_keys=True)
            if hashlib.sha256(key.encode()).hexdigest() not in seen:
                problems.append(f"round {rnd}: digest lost for {q!r}")
    assert not problems, problems

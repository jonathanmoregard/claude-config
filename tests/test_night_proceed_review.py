"""Reproducers for review findings on night-proceed."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from test_night_proceed import HOOK, PYTHON, ask, fake_gdbus, run, stop


def test_n3a_report_listing_considered_options_is_not_a_question(tmp_path: Path) -> None:
    msg = "Considered:\nA) cache\nB) none\n\nWent with A. Done, tests pass."
    assert run(tmp_path, stop(msg)) is None


def test_n3b_numbered_options_question_then_recommendation_blocks(tmp_path: Path) -> None:
    msg = "1. X\n2. Y\n\nWhich one?\n\nI recommend 1."
    out = run(tmp_path, stop(msg))
    assert out and out["decision"] == "block"


def test_n4_headless_sdk_session_is_not_blocked(tmp_path: Path) -> None:
    # No lock seam (the seam marks an e2e run): a fake gdbus reports "locked".
    out = run(tmp_path, ask("Which one?"), lock=None,
              extra={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "PATH": fake_gdbus(tmp_path, "(true,)")})
    assert out is None, f"headless session was denied: {out}"
    assert run(tmp_path, ask("Which one?"), lock=None,
               extra={"CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": fake_gdbus(tmp_path, "(true,)")})


def test_n4_hook_source_reads_entrypoint() -> None:
    assert "CLAUDE_CODE_ENTRYPOINT" in HOOK.read_text(encoding="utf-8")


def test_n6_concurrent_asks_same_session_share_tmp(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_NIGHT_PROCEED")}
    env.update(HOME=str(tmp_path), CLAUDE_NIGHT_PROCEED_TEST_NOW="02:00",
               CLAUDE_NIGHT_PROCEED_TEST_LOCK="locked")
    import hashlib
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


def test_n7a_single_line_start_option_is_not_a_block(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Note:\nA) only one option.")) is None

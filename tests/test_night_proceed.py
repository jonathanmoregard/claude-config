"""night-proceed: answer a night-time question once, only while the screen is locked."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "runtime" if (REPO / "runtime").is_dir() else REPO
HOOK = RUNTIME / "hooks" / "night-proceed.py"
PYTHON = shutil.which("python3") or "python3"


def run(tmp: Path, payload: object, *, now: str | None = "02:00", lock: str | None = "locked",
        extra: dict[str, str] | None = None, raw: str | None = None) -> dict | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_NIGHT_PROCEED")}
    env["HOME"] = str(tmp)
    if now is not None:
        env["CLAUDE_NIGHT_PROCEED_TEST_NOW"] = now
    if lock is not None:
        env["CLAUDE_NIGHT_PROCEED_TEST_LOCK"] = lock
    env.update(extra or {})
    p = subprocess.run([PYTHON, "-S", "-E", str(HOOK)], env=env, text=True,
                       input=raw if raw is not None else json.dumps(payload),
                       capture_output=True, timeout=15)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def stop(msg: str, **kw: object) -> dict:
    return {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": msg, **kw}


def ask(question: str) -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": question}]}}


AB = "Two ways to do it:\n\nA) keep the cache (recommended)\nB) drop it\n\nWhich one?"


def test_night_locked_question_blocks_the_stop(tmp_path: Path) -> None:
    out = run(tmp_path, stop(AB))
    assert out and out["decision"] == "block"
    assert out["reason"].startswith("The user is likely asleep")
    assert "force pushes" in out["reason"]


def test_lettered_options_without_question_mark(tmp_path: Path) -> None:
    msg = "Options:\n- **A.** rename the field\n- **B.** add an alias\n\nI recommend A."
    assert run(tmp_path, stop(msg))


def test_statement_is_left_alone(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Done. Tests pass and the PR is open.")) is None
    assert run(tmp_path, stop("A single item: A) only one option.")) is None


def test_trailing_markdown_after_question_mark(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Shall I go on with **step 2?**\n"))


def test_day_unlocked_unreadable_and_off_switch(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB), now="14:00") is None
    assert run(tmp_path, stop(AB), now="09:00") is None
    assert run(tmp_path, stop(AB), lock="unlocked") is None
    assert run(tmp_path, stop(AB), lock="unreadable") is None
    assert run(tmp_path, stop(AB), extra={"CLAUDE_NIGHT_PROCEED": "0"}) is None


def test_window_edges(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Edge 1?"), now="22:00")
    assert run(tmp_path, stop("Edge 2?"), now="23:59")
    assert run(tmp_path, stop("Edge 3?"), now="08:59")
    assert run(tmp_path, stop("Edge 4?"), now="21:59") is None


def test_same_question_twice_is_let_through(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB))
    assert run(tmp_path, stop(AB)) is None
    assert run(tmp_path, stop("A different question?"))


def test_stop_hook_active_stands_down(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB, stop_hook_active=True)) is None


def test_ask_user_question_denied_once(tmp_path: Path) -> None:
    out = run(tmp_path, ask("Which cache policy?"))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"].startswith("The user is likely asleep")
    assert run(tmp_path, ask("Which cache policy?")) is None


def test_subagents_other_tools_and_garbage(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB, agent_id="a1")) is None
    assert run(tmp_path, {**ask("x?"), "tool_name": "Bash"}) is None
    assert run(tmp_path, stop(AB, session_id="../etc")) is None
    assert run(tmp_path, None, raw="not json") is None


def test_real_lock_query_fails_open_or_answers(tmp_path: Path) -> None:
    # No seam: the hook asks the real desktop. Unlocked or unreachable both
    # mean "present"; this test machine's screen is not locked while it runs.
    assert run(tmp_path, stop(AB), lock=None) is None
    assert run(tmp_path, stop("x?"), lock=None, extra={"PATH": "/nonexistent"}) is None

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
    assert run(tmp_path, stop("Note:\nA) only one option.\n\nI recommend it.")) is None
    assert run(tmp_path, stop("Considered:\nA) cache\nB) none\n\nWent with A. Done, tests pass.")) is None


def test_question_in_last_paragraph_with_numbered_options(tmp_path: Path) -> None:
    # The shape the first e2e probe produced: numbered options, and the
    # question followed by one more sentence in the same paragraph.
    msg = ("1. **Transliterate**\n2. **Strip**\n\nI recommend 1.\n\n"
           "Which do you want: 1 or 2? If you pick 1, I'll also need to know the fallback.")
    assert run(tmp_path, stop(msg))


def test_question_earlier_than_last_paragraph_is_not_the_ending(tmp_path: Path) -> None:
    msg = "Why did it fail? The cache was stale.\n\nFixed it; tests pass."
    assert run(tmp_path, stop(msg)) is None


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


def fake_gdbus(tmp: Path, reply: str, rc: int = 0) -> str:
    """A PATH whose gdbus prints `reply` and exits `rc`, ahead of the real PATH."""
    bindir = tmp / f"gdbus-{abs(hash((reply, rc)))}"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gdbus"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{reply}'\nexit {rc}\n")
    stub.chmod(0o755)
    return f"{bindir}:{os.environ['PATH']}"


def test_desktop_lock_query_without_seam(tmp_path: Path) -> None:
    # The real query path, with gdbus faked: locked blocks, everything else
    # (unlocked, a bus error, no gdbus at all) means present.
    assert run(tmp_path, stop("Lock 1?"), lock=None, extra={"PATH": fake_gdbus(tmp_path, "(true,)")})
    assert run(tmp_path, stop("Lock 2?"), lock=None, extra={"PATH": fake_gdbus(tmp_path, "(false,)")}) is None
    assert run(tmp_path, stop("Lock 3?"), lock=None,
               extra={"PATH": fake_gdbus(tmp_path, "Error: no bus", rc=1)}) is None
    assert run(tmp_path, stop("Lock 4?"), lock=None, extra={"PATH": "/nonexistent"}) is None


def test_question_before_a_closing_recommendation(tmp_path: Path) -> None:
    assert run(tmp_path, stop("1. X\n2. Y\n\nWhich one?\n\nI recommend 1."))
    assert run(tmp_path, stop("A) keep\nB) drop\n\nI recommend A."))

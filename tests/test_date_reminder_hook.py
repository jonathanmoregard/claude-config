#!/usr/bin/env python3
"""date-reminder-hook.py fires rarely: on a new suggestion, then every 100th prompt.

The hook used to remember a single `last_nudge_session`. With two sessions
open, each prompt from one overwrote the slot, so the other re-fired on every
prompt. Now a prompt counter shared across sessions gates it. Checked here
against a sandboxed HOME:

  * a new suggestion fires on the next prompt, from any session;
  * the following 99 prompts, interleaved across sessions, stay silent;
  * the 100th prompt after the nudge fires again;
  * a different suggestion fires straight away;
  * the legacy single-slot key is dropped;
  * no pending suggestion -> silent.

Run: python3 tests/test_date_reminder_hook.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# `runtime/` once the layout migration has landed, the repository root before
# it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = Path(__file__).resolve().parents[1]
if (RUNTIME / "runtime").is_dir():
    RUNTIME = RUNTIME / "runtime"

HOOK = RUNTIME / "date-reminder-hook.py"
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def fire(home: Path, session: str) -> bool:
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": session}),
                          capture_output=True, text=True, env=env, timeout=30)
    return "additionalContext" in proc.stdout


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        dates = home / ".local" / "state" / "claude-dates"
        dates.mkdir(parents=True)
        state = dates / "state.json"
        idea = {"idea": "a walk", "created_at": "2026-10-01"}

        state.write_text(json.dumps({"pending_suggestion": idea, "last_nudge_session": "L"}))
        check("new suggestion fires on the next prompt", fire(home, "A"))
        check("legacy slot dropped", "last_nudge_session" not in json.loads(state.read_text()))
        fired = [fire(home, "AB"[i % 2]) for i in range(99)]
        check("next 99 prompts across two sessions silent", not any(fired), str(fired.count(True)))
        check("100th prompt after the nudge fires", fire(home, "B"))
        check("and the one after is silent again", not fire(home, "A"))

        s = json.loads(state.read_text())
        s["pending_suggestion"] = {"idea": "a picnic", "created_at": "2026-10-20"}
        state.write_text(json.dumps(s))
        check("different suggestion fires straight away", fire(home, "C"))

        state.write_text(json.dumps({"pending_suggestion": None}))
        check("no suggestion -> silent", not fire(home, "D"))

    print("all passed" if not FAILURES else f"{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

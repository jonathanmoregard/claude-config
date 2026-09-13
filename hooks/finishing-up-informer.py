#!/usr/bin/env python3
"""UserPromptSubmit hook: show active DoD targets without blocking."""

from __future__ import annotations

import json
import re
import sqlite3
import sys

from finishing_up_state import (
    SESSION_TOKEN,
    StateError,
    Store,
    render_informer,
    state_db,
)


RESET_PROMPT = re.compile(
    r"^\s*reset\s+dod(?:\s+skill)?\s+to\s+(?:a\s+)?new\s+task\s*:\s*\S[\s\S]*$",
    re.IGNORECASE,
)


def emit_context(message: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": message,
                }
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (TypeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session = payload.get("session_id")
    if not isinstance(session, str) or SESSION_TOKEN.fullmatch(session) is None:
        return 0
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and RESET_PROMPT.fullmatch(prompt):
        return 0
    path = state_db()
    if not path.exists() and not path.is_symlink():
        return 0
    try:
        with Store(readonly=True, timeout=0.05) as store:
            message = render_informer(store.active_targets(session))
    except (OSError, StateError, ValueError, sqlite3.Error):
        emit_context(
            "[dod informer] DoD state is unavailable; do not infer completion "
            "or clear delivery targets. Repair the tracker, then refresh state."
        )
        return 0
    if not message:
        return 0
    emit_context(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

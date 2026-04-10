#!/usr/bin/env python3
"""
UserPromptSubmit hook: surfaces a pending date-night suggestion at the start
of a Claude Code session (once per pending suggestion).

Config: ~/Dropbox/1. Exocortex/_Engine/automation/dates/config.json
State:  ~/Dropbox/1. Exocortex/_Engine/automation/dates/state.json
"""
import json
import os
import sys

DATES_DIR = os.path.expanduser("~/.claude/dates")
CONFIG_FILE = os.path.join(DATES_DIR, "config.json")
STATE_FILE  = os.path.join(DATES_DIR, "state.json")

DEFAULT_STATE = {"last_checked": None, "pending_suggestion": None}


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    os.makedirs(DATES_DIR, exist_ok=True)

    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w") as f:
            json.dump(DEFAULT_STATE, f, indent=2)
        sys.exit(0)

    state = load_json(STATE_FILE, DEFAULT_STATE)
    config = load_json(CONFIG_FILE, {})
    partner = config.get("partner_name", "your partner")

    suggestion = state.get("pending_suggestion")
    if not suggestion:
        sys.exit(0)

    if suggestion.get("shown"):
        sys.exit(0)

    idea = suggestion.get("idea", "a date")
    windows = suggestion.get("suggested_windows", [])

    parts = [
        f"Date reminder: no date with {partner} is planned in the next 2 weeks.",
        f"Suggested idea: {idea}.",
    ]
    if windows:
        parts.append("Good windows: " + "; ".join(windows) + ".")

    parts.append(
        "Mention this to the user when a task is done or just before getting started, "
        "or if the user has been away for some time and then writes a message (time gap)."
    )

    # Print first — if output fails, we haven't consumed the one-shot yet.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": " ".join(parts)
        }
    }))

    # Mark as shown so subsequent sessions don't re-surface the same suggestion.
    suggestion["shown"] = True
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    main()

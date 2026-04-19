#!/usr/bin/env python3
"""
UserPromptSubmit hook: if a pending step-back flag exists for this session,
print the nudge once, delete the flag, and start a cooldown.
"""
import json
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "step-back"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    pending_flag = STATE_DIR / f"pending-{session_id}.json"
    if not pending_flag.exists():
        sys.exit(0)

    try:
        payload = json.loads(pending_flag.read_text())
    except (OSError, json.JSONDecodeError):
        try:
            pending_flag.unlink()
        except OSError:
            pass
        sys.exit(0)

    reason = payload.get("reason", "shift detected")

    print(
        "[step-back] You may have drifted from saying WHAT you want into prescribing HOW. "
        f"Detected: {reason}\n"
        "Consider pausing to: (1) restate the outcome you want, (2) ask Claude what its current plan is and what's blocking it, "
        "(3) decide if a fresh approach is needed."
    )

    try:
        pending_flag.unlink()
    except OSError:
        pass

    cooldown_file = STATE_DIR / f"cooldown-{session_id}"
    try:
        cooldown_file.write_text(str(time.time()))
    except OSError:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SessionStart check: say when the local model's weekly smoke test failed or went quiet.

The offline-ai model is an emergency tool. nixos-config's offline-ai-smoke user timer
loads it about once a week at night and writes its verdict to
~/.local/state/offline-ai/smoke.json. A failure already raises a critical desktop
toast; this hook carries the same fact into the session, where an agent about to
recommend `offline-ai` for an outage can see that it does not work.

Silence is the budget (see aggregator-recall-health.py): healthy prints nothing.
A missing file is silence too — the timer has not had its first night, or this host
has no offline-ai. An unreadable file is announced: silence must mean "checked".
Every path exits 0; an announcement is systemMessage + additionalContext, never a
block.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STALE_DAYS = 10
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
STATE = Path(os.environ.get("OFFLINE_AI_SMOKE_STATE") or STATE_HOME / "offline-ai" / "smoke.json")


def parse(text):
    """ISO-8601 -> aware UTC datetime, or None. A naive stamp is taken as UTC so a
    writer that drops the suffix cannot push the comparison into a TypeError."""
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def announce(text):
    sys.stdout.write(json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
    }))
    sys.stdout.flush()
    return 0


def main():
    try:
        state = json.loads(STATE.read_text())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        return announce(f"offline-ai smoke state unreadable ({STATE}): {exc}")
    if not isinstance(state, dict):
        return announce(f"offline-ai smoke state unreadable ({STATE}): not an object")

    last_ok = parse(state.get("last_ok"))
    last_run = parse(state.get("last_run"))
    reason = state.get("reason") or "no reason recorded"
    if state.get("status") == "fail":
        return announce(
            f"offline-ai weekly smoke FAILED on {state.get('last_run', '?')}: {reason}. "
            "The local model may not load in an emergency. "
            "Check: journalctl --user -u offline-ai-smoke; offline-ai status")
    first_run = parse(state.get("first_run"))
    last_attempt = parse(state.get("last_attempt"))
    # Skips refresh last_run every night, so a host that never passed needs an anchor
    # that does not move: the first run, else the last attempt, else the last run.
    reference = last_ok or first_run or last_attempt or last_run
    if reference and datetime.now(timezone.utc) - reference > timedelta(days=STALE_DAYS):
        since = last_ok.date() if last_ok else f"never (first run {first_run.date() if first_run else 'unknown'})"
        skipped = f", last skip: {state['skip_reason']}" if state.get("skip_reason") else ""
        return announce(
            f"offline-ai weekly smoke has not passed since {since} "
            f"(last run {last_run.date() if last_run else 'never'}, "
            f"{state.get('status', 'no verdict yet')}: {reason}{skipped}). "
            "Check: systemctl --user list-timers offline-ai-smoke.timer")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # a broken health check must never break a session start
        sys.exit(0)

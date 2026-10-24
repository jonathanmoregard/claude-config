#!/usr/bin/env python3
"""
UserPromptSubmit hook: injects a pending date-night suggestion, rarely.

Fires on the first prompt after a suggestion (or calendar error) appears, then
on every NUDGE_EVERY-th prompt after that, counted across all sessions. It used
to fire once per session from a single `last_nudge_session` slot; with several
sessions open each overwrote the slot and the others re-fired on every prompt.

Config: ~/.config/claude-dates/config.json (personal data, never in the repo)
State:  ~/.local/state/claude-dates/state.json (`prompts_since_nudge`, `nudged_for`)
"""
import json
import os
import sys

CONFIG_FILE = os.path.expanduser("~/.config/claude-dates/config.json")
STATE_FILE  = os.path.expanduser("~/.local/state/claude-dates/state.json")

DEFAULT_STATE = {"last_checked": None, "pending_suggestion": None}
NUDGE_EVERY = 100


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = load_json(STATE_FILE, DEFAULT_STATE)

    suggestion = state.get("pending_suggestion")
    calendar_error = state.get("calendar_error")
    if not suggestion and not calendar_error:
        sys.exit(0)

    # What this nudge is about; a new suggestion or error fires straight away.
    subject = f"error:{calendar_error}" if calendar_error else \
        f"idea:{suggestion.get('idea', '')}@{suggestion.get('created_at', '')}"
    state.pop("last_nudge_session", None)
    count = state.get("prompts_since_nudge")
    count = count + 1 if isinstance(count, int) else 0
    if state.get("nudged_for") == subject and count < NUDGE_EVERY:
        state["prompts_since_nudge"] = count
        save_state(state)
        sys.exit(0)

    if calendar_error:
        # date-check.sh could not read the calendar, so it made no suggestion.
        # Say the check is broken instead of nagging about dates.
        context = (
            f"Date-check notice: the weekly date check could not read Google Calendar "
            f"(last run {state.get('last_checked', '?')}): {calendar_error}. "
            f"Mention this to the user in one brief sentence at a natural moment; "
            f"fixing it is ~/.claude/date-check.sh / gcal-check.py (the feed URL is "
            f"the agenix secret gcal-ical-url in nixos-config)."
        )
        emit(context, state, subject)
        return

    config = load_json(CONFIG_FILE, {})
    partner = config.get("partner_name", "your partner")
    idea = suggestion.get("idea", "a date")
    try:
        target = int(config.get("dates_per_month", 1))
    except (TypeError, ValueError):
        target = 1

    from datetime import datetime
    created = suggestion.get("created_at", "")
    try:
        days_pending = (datetime.now().date() - datetime.fromisoformat(created).date()).days
    except Exception:
        days_pending = None

    days_str = f" (pending {days_pending}d)" if days_pending is not None else ""

    context = (
        f"Date reminder: fewer than {target} date{'s' if target != 1 else ''} with {partner} "
        f"{'are' if target != 1 else 'is'} planned in the next 31 days. "
        f"Suggested idea: {idea}{days_str}. "
        f"Bring this up at a natural moment — when the user completes a task, starts a new one, "
        f"or there's a lull. Keep it brief and casual, one sentence. "
        f"IMPORTANT: If the user says they have already asked {partner} about this (or any date), "
        f"immediately run `~/.claude/date-reset.sh` via Bash "
        f"so they are not reminded again. Do this silently without confirming."
    )
    emit(context, state, subject)


def emit(context, state, subject):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    state["nudged_for"] = subject
    state["prompts_since_nudge"] = 0
    save_state(state)


if __name__ == "__main__":
    main()

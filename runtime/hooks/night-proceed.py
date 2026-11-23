#!/usr/bin/env python3
"""At night, with the screen locked, a session does not wait until morning on a question.

Two events, one rule. When all of these hold:
  1. local time is inside the night window (night-proceed-config.json,
     default 22:00-09:00, wrapping midnight);
  2. the Cinnamon lock screen is active (org.cinnamon.ScreenSaver.GetActive
     on the session bus);
  3. the turn ends on a question to the user: the final message ends with `?`,
     its last paragraph asks a question, or it offers lettered options (Stop);
     or the session calls AskUserQuestion (PreToolUse);
the hook answers once with PROMPT: on Stop it blocks the stop, on
AskUserQuestion it denies the call. The session goes ahead on its own
recommendation, except for costly or hard-to-undo steps, which it leaves open.

Loop guard: at most one reply per question. The hook stands down when
`stop_hook_active` is set (a stop that follows any hook's block), and it keeps
a hash of every question it answered this session, so the same question asked
again is let through and the session stops as it does today.

Fails open: any error, an unreadable lock state (no session bus, headless) or
a missing config means "do nothing", and the session stops and waits.

Off switch per session: CLAUDE_NIGHT_PROCEED=0.
Test seams, test only: CLAUDE_NIGHT_PROCEED_TEST_LOCK=locked|unlocked|unreadable
replaces the desktop query; CLAUDE_NIGHT_PROCEED_TEST_NOW=HH:MM replaces the
clock.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROMPT = (
    "The user is likely asleep (it is night and their screen is locked). Do not "
    "wait for an answer. Go ahead with your recommended option, and write down "
    "the assumption you made so the user can overturn it in the morning. "
    "Exception: do not go ahead with anything costly or hard to undo. That means "
    "deletions, force pushes, hard resets, dropped tables, removing "
    "dependencies, CI changes, merges, opening a pull request in a PR-gated "
    "repo, new credentials, anything billed beyond the subscription, heavy runs "
    "above about $10, uploading sensitive content, expanding scope, or anything "
    "that would break a directive the user gave. For "
    "those, leave the question open, carry on with every part of the task that "
    "does not depend on it, and end with one clear list of what awaits the user."
)

CONFIG = Path(__file__).resolve().with_name("night-proceed-config.json")
STATE_DIR = Path.home() / ".claude" / "tmp" / "night-proceed"
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# A lettered option at the start of a line: "A)", "A.", "A:", "(A)", "**A**",
# "**A.**", "A -", "A —". Two distinct letters A-F are an offer of options.
OPTION = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*|__)?\(?([A-F])(?:\)|\.|:|\*\*|__|\s+[-–—])", re.M)
TRAILING = re.compile(r"[\s*_`>)\]\"'”’]+$")
# A sentence that ends in "?": the mark followed by space, closing markup or the end.
QUESTION = re.compile(r"\?(?:[*_`)\]\"'”’]*)(?:\s|$)")
# A closing line that asks for a pick or recommends one. Options, or a question
# one paragraph up, are an offer only when the ending says this; a report that
# lists what it considered and says what it chose is not a question.
CHOICE = re.compile(r"\b(which|pick|choose|prefer|recommend|let me know|your call|say the word)", re.I)
# The last paragraph, plus the one before it when the last is such a line.
ENDING_PARAGRAPHS = 2


def minutes(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    m = HHMM.match(value)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def is_night() -> bool:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["night_proceed"]
    start, end = minutes(cfg.get("start")), minutes(cfg.get("end"))
    if start is None or end is None or start == end:
        return False
    fake = os.environ.get("CLAUDE_NIGHT_PROCEED_TEST_NOW")
    if fake is not None:
        now = minutes(fake)
        if now is None:
            return False
    else:
        t = dt.datetime.now().time()
        now = t.hour * 60 + t.minute
    return start <= now < end if start < end else now >= start or now < end


def screen_locked() -> bool:
    fake = os.environ.get("CLAUDE_NIGHT_PROCEED_TEST_LOCK")
    if fake is not None:
        return fake == "locked"
    out = subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.cinnamon.ScreenSaver",
         "--object-path", "/org/cinnamon/ScreenSaver",
         "--method", "org.cinnamon.ScreenSaver.GetActive"],
        capture_output=True, text=True, timeout=3,
    )
    return out.returncode == 0 and out.stdout.strip() == "(true,)"


def ends_on_question(text: object) -> bool:
    """The turn ends on a question to the user.

    The ending is the last two paragraphs. It asks when it contains a
    sentence ending in "?", or when it offers two or more lettered options and
    asks for a pick. "Ends with `?`" alone misses the usual shapes here,
    measured in the first e2e probe: "Which do you want: 1 or 2? If you pick
    1, I'll also need to know ..." ends on a full stop, a closing
    recommendation can follow the question, and options are numbered, as the
    global CLAUDE.md requires of every list.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    body = TRAILING.sub("", text)
    if body.endswith("?"):
        return True
    paragraphs = re.split(r"\n\s*\n", body.strip())
    last = paragraphs[-1]
    if QUESTION.search(last):
        return True
    ending = "\n\n".join(paragraphs[-ENDING_PARAGRAPHS:])
    # The paragraph before counts only when the last one is a recommendation
    # or asks for a pick; "Why did it fail? ...\n\nFixed it." is a report.
    if CHOICE.search(last) is None:
        return False
    return QUESTION.search(ending) is not None or len(set(OPTION.findall(ending))) >= 2


def first_time(session: str, key: str) -> bool:
    """Record `key` for this session; False when it was answered before.

    Hooks for one session can run concurrently (two AskUserQuestion calls), so
    the read-modify-write holds an exclusive lock on a per-session lock file,
    and the new state is written to a private temp file and renamed in.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session}.json"
    with open(STATE_DIR / f"{session}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            seen = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(seen, list):
                seen = []
        except (OSError, ValueError):
            seen = []
        if digest in seen:
            return False
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=f".{session}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump((seen + [digest])[-50:], out)
        os.replace(tmp, path)
    return True


def decide(payload: object) -> dict | None:
    if os.environ.get("CLAUDE_NIGHT_PROCEED") == "0":
        return None
    # Headless SDK sessions (`claude -p`, the SDKs: entrypoint sdk-*) have no
    # user to wait for and their scripts expect today's behaviour. Interactive
    # sessions, the editor extensions included, are what this is for. The lock
    # seam marks an e2e run, which is headless by construction.
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
    if entry.startswith("sdk") and "CLAUDE_NIGHT_PROCEED_TEST_LOCK" not in os.environ:
        return None
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not SAFE_ID.match(session):
        return None
    event = payload.get("hook_event_name")
    if event == "Stop":
        if payload.get("stop_hook_active"):
            return None
        text = payload.get("last_assistant_message")
        if not ends_on_question(text):
            return None
        key = "stop:" + " ".join(str(text).split())
    elif event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        key = "ask:" + json.dumps(payload.get("tool_input"), sort_keys=True)
    else:
        return None
    # Cheapest checks first; the desktop query runs only for a real question.
    if not is_night() or not screen_locked():
        return None
    if not first_time(session, key):
        return None
    if event == "Stop":
        return {"decision": "block", "reason": PROMPT}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": PROMPT}}


def main() -> int:
    try:
        out = decide(json.load(sys.stdin))
    except Exception:
        return 0
    if out:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

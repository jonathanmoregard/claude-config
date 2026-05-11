#!/usr/bin/env python3
"""
Background classifier: read recent transcript turns, ask Haiku whether the
user has shifted from describing WHAT/WHY to prescribing HOW with rising
frustration. If yes, write pending-<sid>.json with a one-line reason.

Invoked detached from step-back-detector.py. Never blocks Claude.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
COMMAND_TAG_RE = re.compile(r"<(?:command-name|command-message|command-args|local-command-stdout|local-command-stderr)>.*?</(?:command-name|command-message|command-args|local-command-stdout|local-command-stderr)>", re.DOTALL)
CLASSIFIER_COOLDOWN_SECONDS = 5 * 60

STATE_DIR = Path.home() / ".claude" / "step-back"
RECENT_TURNS = 10
MAX_TEXT_PER_TURN = 2000
HAIKU_TIMEOUT = 90
LOG = STATE_DIR / "classifier.log"

CLASSIFIER_PROMPT = """You are watching a Claude Code session for a specific failure mode.

The user prefers to describe WHAT they want and WHY, and let Claude figure out HOW. The failure mode happens when Claude can't deliver, so the user is dragged into prescribing HOW themselves — step by step, with rising frustration. It's time-consuming and irritating. They want a nudge to pause and reassess when this is happening.

TRIGGER when 2+ of these are present in the recent turns:
  (a) User is prescribing HOW (specific commands, exact code, exact words to use, exact files to edit, exact search terms to try)
  (b) Frustration markers — any of:
        - ALL CAPS, "WTF", profanity, exclamation marks ("!!", "????")
        - emphatic negation ("no", "NO", "stop", "wait")
        - "I already said", "as I told you", repetition of a point
        - terse single-word/phrase replies after Claude misses ("recheck", "again", "no", "try")
  (c) Claude visibly missing the goal — repeated user-interrupts of tool calls, same unresolved symptom reported 2+ times, user re-explaining the same constraint
  (d) Cold-frustration / user doing Claude's job — user provides a substantial diagnostic, root-cause analysis, or detailed fix plan that Claude should have produced. Calm tone but extensive content (e.g. "Issue: X. Root cause: Y. Fix: Z." after Claude only asked "what's going on?"). The act of the user doing Claude's work IS the signal, even with no overt anger.

SKIP when:
  - User explicitly DELEGATES HOW to Claude ("use your judgment", "go with your intuition", "your call", "your discretion", "be autonomous", "your choice", "your best intuition"). Terseness here = trust, NOT frustration.
  - User explicitly asks for analysis only, not action ("answer only, do nothing", "just explain", "don't do anything yet", "before you act"). Concrete steps inside such a turn are HYPOTHETICAL.
  - User answering Claude's clarifying question with a one-word direction ("yes", "B", "1y 2y 3n").
  - Calm collaborative iteration with no frustration markers, even if user is specific about what to do next.
  - Single correction in an otherwise smooth session (one "no, do X instead" without repetition).

You do NOT need to see a clean shift over time. The signal is the *current* presence of HOW-prescription + frustration + Claude missing. A session that opens prescriptive can still TRIGGER if frustration and missing-the-goal are evident.

Output EXACTLY one of:
  TRIGGER: <one short sentence naming what you saw>
  SKIP

Respond with ONLY that line, no preamble.

---
Recent conversation (oldest first):

{conversation}
"""


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    parts.append(f"[tool_use: {item.get('name', '?')}]")
                elif item.get("type") == "tool_result":
                    parts.append("[tool_result]")
        return " ".join(p for p in parts if p)
    return ""


def clean_text(text):
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = COMMAND_TAG_RE.sub("", text)
    return text.strip()


def is_real_user_entry(entry):
    """Match wellbeing-nudge.py filter for real human prompts."""
    if entry.get("isSidechain") or entry.get("isMeta"):
        return False
    if entry.get("entrypoint") == "sdk-cli":
        return False
    if "/homunculus/" in entry.get("cwd", ""):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result"
        for c in content
    ):
        return False
    return True


def build_conversation(transcript_path):
    turns = []
    try:
        with open(transcript_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj_type = obj.get("type")
                if obj_type not in ("user", "assistant"):
                    continue
                if obj.get("isSidechain") or obj.get("isMeta"):
                    continue
                if obj_type == "user" and not is_real_user_entry(obj):
                    continue
                msg = obj.get("message", {})
                content = msg.get("content")
                text = extract_text(content)
                text = clean_text(text)
                if not text:
                    continue
                text = text[:MAX_TEXT_PER_TURN]
                turns.append((obj_type, text))
    except OSError:
        return ""

    turns = turns[-RECENT_TURNS:]
    lines = []
    for role, text in turns:
        label = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"{label}: {text}")
    return "\n\n".join(lines)


def call_haiku(prompt):
    # Run claude from STATE_DIR so the subprocess's session jsonl lands in
    # ~/.claude/projects/-home-jonathan--claude-step-back/ instead of the
    # parent session's project dir. Otherwise every Stop tick drops a
    # classifier-prompt session into the user's project — and on resume,
    # Claude Code's latest-by-mtime pick can land on the classifier jsonl,
    # opening the next session with the watcher prompt as the first user
    # message.
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", prompt],
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT,
            cwd=str(STATE_DIR),
        )
    except subprocess.TimeoutExpired:
        log("classifier timeout")
        return None
    except FileNotFoundError:
        log("claude CLI not found")
        return None

    if result.returncode != 0:
        log(f"classifier rc={result.returncode} stderr={result.stderr[:200]}")
        return None

    return result.stdout.strip()


def main():
    if len(sys.argv) != 3:
        sys.exit(0)

    session_id = sys.argv[1]
    transcript_path = sys.argv[2]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    inflight = STATE_DIR / f"inflight-{session_id}"

    try:
        conversation = build_conversation(transcript_path)
        if not conversation:
            log(f"{session_id}: empty conversation")
            return

        prompt = CLASSIFIER_PROMPT.format(conversation=conversation)
        response = call_haiku(prompt)
        if not response:
            return

        verdict_line = ""
        for line in response.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("TRIGGER") or upper.startswith("SKIP"):
                verdict_line = stripped
                break
        log(f"{session_id}: verdict={verdict_line[:200]}")

        cooldown_file = STATE_DIR / f"classified-{session_id}"
        try:
            cooldown_file.write_text(str(time.time()))
        except OSError:
            pass

        if not verdict_line.upper().startswith("TRIGGER"):
            return

        reason = verdict_line.split(":", 1)[1].strip() if ":" in verdict_line else verdict_line
        if not reason:
            reason = "what/why -> how shift detected"

        pending_flag = STATE_DIR / f"pending-{session_id}.json"
        pending_flag.write_text(json.dumps({
            "reason": reason[:300],
            "ts": time.time(),
        }))
        log(f"{session_id}: TRIGGERED reason={reason[:120]}")
    finally:
        try:
            inflight.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()

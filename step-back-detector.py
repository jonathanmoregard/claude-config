#!/usr/bin/env python3
"""
Stop hook: fast gate, then spawn detached classifier subprocess.

Gates (all checked inline, no LLM cost):
  - session has >=4 user messages (no shift pattern possible earlier)
  - no pending flag for this session
  - cooldown timestamp <30 min ago -> skip

If gates pass, spawn step-back-classifier.py detached so this hook returns
immediately. Classifier runs Haiku and writes pending-<sid>.json on trigger.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "step-back"
COOLDOWN_SECONDS = 30 * 60
MIN_USER_MESSAGES = 4
CLASSIFIER = Path.home() / ".claude" / "step-back-classifier.py"


def is_real_user_message(entry):
    """Match wellbeing-nudge.py: real human prompts only.

    Excludes subagent sidechains, meta entries, SDK/headless sessions,
    homunculus background sessions, and synthetic tool_result messages.
    """
    if entry.get("type") != "user":
        return False
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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    if not session_id or not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    pending_flag = STATE_DIR / f"pending-{session_id}.json"
    if pending_flag.exists():
        sys.exit(0)

    cooldown_file = STATE_DIR / f"cooldown-{session_id}"
    if cooldown_file.exists():
        try:
            last = float(cooldown_file.read_text().strip())
            if time.time() - last < COOLDOWN_SECONDS:
                sys.exit(0)
        except (ValueError, OSError):
            pass

    classified_file = STATE_DIR / f"classified-{session_id}"
    if classified_file.exists():
        try:
            last = float(classified_file.read_text().strip())
            if time.time() - last < 5 * 60:
                sys.exit(0)
        except (ValueError, OSError):
            pass

    user_count = 0
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
                if not is_real_user_message(obj):
                    continue
                user_count += 1
                if user_count >= MIN_USER_MESSAGES:
                    break
    except OSError:
        sys.exit(0)

    if user_count < MIN_USER_MESSAGES:
        sys.exit(0)

    inflight = STATE_DIR / f"inflight-{session_id}"
    if inflight.exists():
        try:
            age = time.time() - inflight.stat().st_mtime
            if age < 120:
                sys.exit(0)
        except OSError:
            pass
    try:
        inflight.write_text(str(time.time()))
    except OSError:
        sys.exit(0)

    try:
        subprocess.Popen(
            ["python3", str(CLASSIFIER), session_id, transcript_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        try:
            inflight.unlink()
        except OSError:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()

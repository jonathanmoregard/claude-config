#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns when context is over 40% full with potentially
stale/off-topic content. Activates every 5 user messages once threshold is met.
"""
import json
import os
import sys

MAX_TOKENS = 200_000
THRESHOLD = 0.40
MESSAGE_INTERVAL = 5


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    file_size = os.path.getsize(transcript_path)

    user_msg_count = 0
    last_usage = None

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

            if obj_type == "user":
                user_msg_count += 1
            elif obj_type == "assistant":
                msg = obj.get("message", {})
                usage = msg.get("usage")
                if usage:
                    last_usage = usage

    # Fire every MESSAGE_INTERVAL messages
    if user_msg_count == 0 or user_msg_count % MESSAGE_INTERVAL != 0:
        sys.exit(0)

    # Calculate context fill
    if last_usage:
        total_tokens = (
            last_usage.get("input_tokens", 0)
            + last_usage.get("cache_creation_input_tokens", 0)
            + last_usage.get("cache_read_input_tokens", 0)
        )
        fill_pct = total_tokens / MAX_TOKENS
    else:
        # Fallback: file size heuristic (4 chars ≈ 1 token)
        fill_pct = file_size / (MAX_TOKENS * 4)

    if fill_pct < THRESHOLD:
        sys.exit(0)

    pct_display = int(fill_pct * 100)
    message = (
        f"Context is {pct_display}% full ({user_msg_count} messages in this session). "
        "Review the conversation history: identify any content that appears to be leftover "
        "from a previous task or is no longer relevant to the current task. "
        "Summarize what seems stale or off-topic, and warn the user clearly if you are "
        "carrying significant irrelevant baggage."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns when context is over THRESHOLD with potentially
stale/off-topic content. Fires every MESSAGE_INTERVAL user messages once
threshold is met.

Context window detection (in priority order):
    1. CLAUDE_CONTEXT_MAX env var (explicit override, in tokens)
    2. Model ID prefix — Opus/Sonnet 4.x run with 1M in this setup
    3. Observed usage — if any assistant turn has exceeded 200k tokens,
       the session is 1M, regardless of model name
    4. Default 200k
"""
import json
import os
import sys

BASE_MAX = 200_000
ONE_M = 1_000_000
THRESHOLD = 0.40
MESSAGE_INTERVAL = 5

# Claude Code model IDs that run at 1M context by default. Prefixes, so
# dated variants (claude-opus-4-1-20250805) match too.
ONE_M_MODEL_PREFIXES = (
    "claude-opus-4-",
    "claude-sonnet-4-",
    "claude-opus-5",
    "claude-sonnet-5",
)

# Explicit 1M marker in the model ID, e.g. `claude-opus-5[1m]`. Checked
# before the prefix list so it keeps working across model generations
# without another edit here — the prefix list going stale is exactly how
# this hook came to report 89% full during a session that was at 18% of a
# 1M window (every reading was fullness-of-200k).
ONE_M_MARKER = "[1m]"


def detect_max_tokens(model: str | None, observed_max: int) -> int:
    override = os.environ.get("CLAUDE_CONTEXT_MAX")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    if model:
        if ONE_M_MARKER in model.lower():
            return ONE_M
        if any(model.startswith(p) for p in ONE_M_MODEL_PREFIXES):
            return ONE_M
    if observed_max > BASE_MAX:
        return ONE_M
    return BASE_MAX


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
    last_model = None
    observed_max = 0

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
                model = msg.get("model")
                if model and model != "<synthetic>":
                    last_model = model
                usage = msg.get("usage")
                if usage:
                    last_usage = usage
                    total = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    if total > observed_max:
                        observed_max = total

    if user_msg_count == 0 or user_msg_count % MESSAGE_INTERVAL != 0:
        sys.exit(0)

    max_tokens = detect_max_tokens(last_model, observed_max)

    if last_usage:
        total_tokens = (
            last_usage.get("input_tokens", 0)
            + last_usage.get("cache_creation_input_tokens", 0)
            + last_usage.get("cache_read_input_tokens", 0)
        )
        fill_pct = total_tokens / max_tokens
    else:
        fill_pct = file_size / (max_tokens * 4)

    if fill_pct < THRESHOLD:
        sys.exit(0)

    pct_display = int(fill_pct * 100)
    message = (
        f"Context is {pct_display}% full ({user_msg_count} messages in this session; "
        f"window: {max_tokens // 1000}k tokens). "
        "Review the conversation history: identify any content that appears to be leftover "
        "from a previous task or is no longer relevant to the current task. "
        "Summarize what seems stale or off-topic, and warn the user clearly if you are "
        "carrying significant irrelevant baggage."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}))


if __name__ == "__main__":
    main()

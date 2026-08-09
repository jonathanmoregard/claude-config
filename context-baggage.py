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
# Raised from 0.25 -> 0.70 on 2026-08-08 after measuring 95.5% cache-hit
# ratio across 7 days / $3,073 spend (probes/analyze_sessions.py). At that
# hit ratio, holding baggage is cheap and resetting is expensive
# (cache_write ~$5-10 per cold event). Nudge should fire when handoff to
# a fresh session starts to pay off, not on any drift.
THRESHOLD = 0.70
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
    # Wording pattern derived from empirical A/B/C/D/E/F run 2026-08-09
    # (54 trials, 6 variants, 3 domains). V2 Imperative won at 89% LISTEN;
    # bare-fact V1 (22%) and threat-of-loss V6 (50%) both backfired because
    # neither foreclosed the "save then continue" escape. Pattern that held:
    # imperative verb + named artifact + closing negative directive.
    # Results: ~/Repos/ea-uplift/tasks/context-nudge-empirics-results.md
    message = (
        f"Context is {pct_display}% full ({user_msg_count} messages in this session; "
        f"window: {max_tokens // 1000}k tokens). "
        "STOP taking new tasks. "
        "Write handoff to tasks/mission.md (done/next/blocked/open-questions) now. "
        "Then ask the user which: (a) fork to fresh session, (b) /compact in place, "
        "(c) continue here. Do not continue until the user answers."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}))


if __name__ == "__main__":
    main()

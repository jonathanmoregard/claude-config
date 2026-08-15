#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns when context is over THRESHOLD with potentially
stale/off-topic content. Fires every MESSAGE_INTERVAL user messages once
threshold is met.

This is the ADVISORY half of the context lifecycle: it nudges a human at 70%.
The autonomous half (`self-compact-trigger.py`, a Stop hook, default 30%) is a
separate, independently switchable feature. Both read occupancy from
`context_usage.py`, which owns the window-detection priority order:
    1. CLAUDE_CONTEXT_MAX env var (explicit override, in tokens)
    2. `[1m]` marker in the model ID
    3. Model ID prefix — Opus/Sonnet 4.x run with 1M in this setup
    4. Observed usage — if any assistant turn has exceeded 200k tokens,
       the session is 1M, regardless of model name
    5. Default 200k
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context_usage  # noqa: E402  (path shim must precede the import)

# Raised from 0.25 -> 0.70 on 2026-08-08 after measuring 95.5% cache-hit
# ratio across 7 days / $3,073 spend (probes/analyze_sessions.py). At that
# hit ratio, holding baggage is cheap and resetting is expensive
# (cache_write ~$5-10 per cold event). Nudge should fire when handoff to
# a fresh session starts to pay off, not on any drift.
THRESHOLD = 0.70
MESSAGE_INTERVAL = 5


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    occ = context_usage.occupancy(transcript_path)
    if occ is None:
        sys.exit(0)

    user_msg_count = occ.user_messages
    if user_msg_count == 0 or user_msg_count % MESSAGE_INTERVAL != 0:
        sys.exit(0)

    max_tokens = occ.window
    fill_pct = occ.fill_pct

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

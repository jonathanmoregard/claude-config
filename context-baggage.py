#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns when context is over THRESHOLD with potentially
stale/off-topic content. Fires every MESSAGE_INTERVAL user messages once
threshold is met.

This is the ADVISORY half of the context lifecycle: it nudges a human at
THRESHOLD of usable capacity (the tokens Claude Code holds before it
auto-compacts). The autonomous half is `self-compact-trigger.py`, a Stop hook
that fires at FIRE_AT_TOKENS and runs UNCONDITIONALLY — there is no switch and
no config key for it (USER DECISION 2026-08-16; `components.self_compact` and
`SELF_COMPACT_CONFIG` were deleted). What bounds it is idle-handoff.sh's guard
chain, not an on/off setting.

The two halves are independent in the only sense that matters here: separate hooks,
separate thresholds (a fraction of usable capacity here, absolute tokens there),
and neither reads the other's state.
Both read occupancy from `context_usage.py`, which owns the window-detection
priority order:
    1. CLAUDE_CONTEXT_MAX env var (explicit override, in tokens)
    2. `[1m]` marker in the model ID
    3. Model ID prefix — Opus/Sonnet 4.x run with 1M in this setup
    4. Observed usage — if any assistant turn has exceeded 200k tokens,
       the session is 1M, regardless of model name
    5. Default 200k
then capped at Claude Code's auto-compact window (settings.json
`autoCompactWindow`, or CLAUDE_CODE_AUTO_COMPACT_WINDOW), read here at the edge
and passed in.
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
# 0.70 -> 0.90 on 2026-10-24 when the autoCompactWindow cap (420k) made this
# a fraction of 420k, not 1M, and self-compact moved to 350k.
# 0.90 of the WINDOW -> 0.95 of USABLE capacity (context_usage.usable_capacity:
# the window minus Claude Code's output reserve and compact buffer) the same
# day: a fraction of the window lands past Claude Code's auto-compact point on
# small windows (200k: nudge 180k, compaction 167k), so the nudge never fired
# there. A fraction of usable capacity is below compaction on every window;
# the 0.95 only sets how early. Order at the repo's 420k cap: self-compact
# 350k < nudge ~368k < auto-compact 387k. Tests pin both orders.
THRESHOLD = 0.95
MESSAGE_INTERVAL = 5


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    # THRESHOLD of the window Claude Code will actually let fill: with the
    # settings.json autoCompactWindow cap (420k) a 1M session compacts long
    # before 700k, so the uncapped window would make this nudge unreachable.
    cap = context_usage.auto_compact_window(context_usage.user_settings_path(), os.environ)
    occ = context_usage.occupancy(transcript_path, cap)
    if occ is None:
        sys.exit(0)

    user_msg_count = occ.user_messages
    if user_msg_count == 0 or user_msg_count % MESSAGE_INTERVAL != 0:
        sys.exit(0)

    # Fill of what Claude Code lets the session hold before it auto-compacts,
    # not of the raw window: that is the point the human has to act before.
    usable = context_usage.usable_capacity(occ.window)
    fill_of_usable = occ.total_tokens / usable

    if fill_of_usable < THRESHOLD:
        sys.exit(0)

    pct_display = int(fill_of_usable * 100)
    # Wording pattern derived from empirical A/B/C/D/E/F run 2026-08-09
    # (54 trials, 6 variants, 3 domains). V2 Imperative won at 89% LISTEN;
    # bare-fact V1 (22%) and threat-of-loss V6 (50%) both backfired because
    # neither foreclosed the "save then continue" escape. Pattern that held:
    # imperative verb + named artifact + closing negative directive.
    # Results: ~/Repos/ea-uplift/tasks/context-nudge-empirics-results.md
    message = (
        f"Context is {pct_display}% full: {occ.total_tokens // 1000}k of the "
        f"{usable // 1000}k tokens Claude Code holds before it auto-compacts "
        f"(window: {occ.window // 1000}k; {user_msg_count} messages in this session). "
        "STOP taking new tasks. "
        # NOT tasks/mission.md. That path is a machine-owned symlink to the
        # human-readable copy of the out-of-repo handoff
        # ($REFLECT_ROOT/sessions/<sid>/handoff/mission.md), so a write-through here
        # would clobber the copy — and the next mission run would silently overwrite
        # whatever the model had written. tasks/handoff.md is human-owned: no
        # automation reads it, no automation writes it, and it stays visible to
        # idle-handoff's freshness gate, which is correct — a human handoff IS the
        # world moving.
        "Write handoff to tasks/handoff.md (done/next/blocked/open-questions) now. "
        "Then ask the user which: (a) fork to fresh session, (b) /compact in place, "
        "(c) continue here. Do not continue until the user answers."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}))


if __name__ == "__main__":
    main()

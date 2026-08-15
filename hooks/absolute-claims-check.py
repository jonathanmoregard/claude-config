#!/usr/bin/env python3
"""Stop hook: catch absolute claims and send the turn back for empirics.

Fires when the last assistant message asserts impossibility or certainty
("impossible", "0% chance", "100% guaranteed", "no way to ...") instead of
producing evidence. Blocks the stop and hands the model the reason, so the
turn continues rather than shipping an unfalsifiable claim.

Silent on: anything it cannot parse, and any turn where stop_hook_active is
already set (Claude Code's own loop guard).
"""
from __future__ import annotations

import json
import re
import sys

# Each pattern targets a CLAIM, not a measurement. "100% of tests passed" and
# "0 errors" are facts; "100% certain" and "0% chance" are predictions wearing
# a fact's clothes.
PATTERNS = [
    r"\bimpossible\b",
    r"\bimpossibl[ey]\w*\b",
    r"\bno way to\b",
    r"\bcan(?:not|'t|not ever) be (?:done|automated|built|fixed|achieved)\b",
    r"\bthere is no way\b",
    r"\bnever (?:possible|works?|happens?|will work)\b",
    r"\b(?:0|zero)\s*%\s*(?:chance|probability|possib|likel)",
    r"\b100\s*%\s*(?:certain|sure|guaranteed|impossible|safe|reliable)",
    r"\bguaranteed (?:not )?to\b",
    r"\bwith certainty\b",
]

GUIDANCE = (
    "You asserted an absolute ({matched}). 0 is usually not on the doability "
    "scale: if you say something is 0 and it then happens, you are infinitely "
    "wrong, and there is no Bayesian updating possible from a prior of 0. The "
    "same holds at 100%.\n\n"
    "Revert to empirics, using the observability tools available. A deep "
    "research call is usually a good one, and infinitely better than simply "
    "claiming impossible.\n\n"
    "Do one of these before you stop:\n"
    "1. Run the research or the experiment that would settle it, then report "
    "what you found.\n"
    "2. Restate the claim as a probability with the evidence behind it, and "
    "name what observation would change it.\n"
    "3. If the absolute is a quoted measurement rather than a prediction "
    "(\"100% of tests passed\", \"0 errors\"), say so plainly and continue."
)


def last_assistant_text(transcript_path: str) -> str:
    """Return the text of the final assistant message, or '' if unreadable."""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return ""
    for row in reversed(rows):
        message = row.get("message") or {}
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts = [
            block.get("text", "")
            for block in (content or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return ""


def strip_code(text: str) -> str:
    """Drop fenced blocks and inline code — quoted output is not a claim."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", " ", text)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0

    # Claude Code's own re-entry guard: never block twice on one turn.
    if payload.get("stop_hook_active"):
        return 0

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0

    text = strip_code(last_assistant_text(transcript))
    if not text:
        return 0

    hits = []
    for pattern in PATTERNS:
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if found:
            hits.append(found.group(0).strip())
    if not hits:
        return 0

    matched = ", ".join(sorted({h.lower() for h in hits})[:3])
    print(json.dumps({
        "decision": "block",
        "reason": GUIDANCE.format(matched=matched),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

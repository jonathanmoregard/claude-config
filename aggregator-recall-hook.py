#!/usr/bin/env python3
"""UserPromptSubmit hook: route recall questions to the aggregator MCP.

Why this exists
---------------
The aggregator indexes every past Claude Code session, subagent run, PR,
research report and chat export. It still lost every "do you remember…"
question to two weaker paths: the built-in auto-memory directory (which the
client describes in its own system prompt) and raw grep over
~/.claude/projects/*.jsonl.

Server-side fixes (MCP ``instructions``, a tool named ``…_search_memory``,
richer descriptions) are advisory — the model can still ignore them. Hook
injection is deterministic: the client wraps this hook's ``additionalContext``
in a system-reminder and inserts it at the point the hook fired. So the
reliable layer lives here, and the persuasive layer lives in the server.

Scope discipline
----------------
Fires ONLY on recall-shaped prompts. The injected text persists in
conversation history as a system-reminder block, so an always-on version
would compound context cost on every single turn for no benefit.
"""
from __future__ import annotations

import json
import os
import re
import sys

TOOL = "mcp__aggregator__aggregator_search_memory"

# Recall intent. Deliberately conservative: a false positive costs context on
# an unrelated turn, so these all require past-tense/retrospective shape
# rather than merely mentioning memory.
RECALL_PATTERNS = [
    r"\bdo you (?:remember|recall)\b",
    r"\b(?:do|did) (?:we|you|i) (?:ever )?(?:discuss|decide|talk about|cover|try|look at)\b",
    r"\bwhat did (?:we|you|i) (?:decide|say|discuss|conclude|call|do|find|use)\b",
    r"\bwhat was (?:that|the) \w+",
    r"\bwhen did (?:we|i|you)\b",
    r"\blast time\b",
    r"\bprevious(?:ly)? (?:session|conversation|talk|chat|discussion|time)\b",
    r"\bearlier (?:session|conversation|talk|chat|you said|we)\b",
    r"\bour (?:earlier|previous|last|old) (?:talk|chat|conversation|session|discussion)\b",
    r"\bhave (?:i|we) (?:ever )?(?:done|tried|built|written|discussed|used|seen)\b",
    r"\bfind (?:that|the) (?:session|conversation|transcript|report|research|pr|thread)\b",
    r"\brecollection\b",
    r"\bremind me (?:what|when|why|how|about)\b",
    r"\bdig up\b",
    r"\bwe (?:talked|spoke) about\b",
    r"\byou (?:mentioned|said|told me)\b",
]

# "remember to call me" is an instruction, not a recall request.
NEGATIVE_PATTERNS = [
    r"\bremember to\b",
    r"\bremind me to\b",
]

RECALL_RE = re.compile("|".join(RECALL_PATTERNS), re.IGNORECASE)
NEGATIVE_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)

# Already naming the aggregator (or actively working on it) means the steer
# is redundant — injecting it would just burn context.
ALREADY_ON_TARGET_RE = re.compile(r"aggregator", re.IGNORECASE)

CONTEXT = f"""Recall-shaped question detected.

Search the aggregator FIRST: load `{TOOL}` (via ToolSearch if its schema is \
not loaded yet — deferred tools show name-only until then) and query it \
before anything else.

Do NOT grep ~/.claude/projects/*.jsonl and do NOT start from the auto-memory \
directory. Both are strict subsets of what the aggregator indexes, and the \
transcript files carry no ranking. Call `mcp__aggregator__aggregator_capabilities` \
if you need the live source list or DSL keys.

If the aggregator genuinely returns nothing relevant, say so before falling \
back to another path."""


def main() -> int:
    if os.environ.get("AGGREGATOR_RECALL_HOOK_OFF"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the turn on malformed input

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if ALREADY_ON_TARGET_RE.search(prompt):
        return 0
    if NEGATIVE_RE.search(prompt):
        return 0
    if not RECALL_RE.search(prompt):
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": CONTEXT,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

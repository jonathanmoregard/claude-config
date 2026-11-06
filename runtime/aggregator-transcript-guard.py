#!/usr/bin/env python3
"""PreToolUse guard: block raw transcript reads, redirect to the aggregator.

A PreToolUse ``deny`` is one of the few deterministic levers in the harness —
the tool call is cancelled and ``permissionDecisionReason`` is handed back to
the model, so it learns which tool to use instead of simply failing.

Scope is deliberately narrow: only Read/Grep aimed at
``~/.claude/projects/**/*.jsonl`` (the session transcripts the aggregator
already indexes, with ranking the raw files don't have). Everything else in
that tree — the auto-memory directory, todos, per-project state — is left
alone, and Bash is untouched, so ingest debugging and ordinary file work
still function.

Escape hatch: set AGGREGATOR_GUARD_OFF=1 to disable, e.g. when working on the
aggregator's own session parser and you need the raw bytes.
"""
from __future__ import annotations

import json
import os
import re
import sys

TOOL = "mcp__aggregator__aggregator_search_memory"

# Matches the transcript files only. Requires BOTH the projects dir and a
# .jsonl target, so `Read(~/.claude/projects/<slug>/memory/foo.md)` passes.
TRANSCRIPT_RE = re.compile(r"\.claude/projects/.*\.jsonl", re.IGNORECASE)
# A Grep whose path is the projects tree sweeps transcripts even without an
# explicit .jsonl in the path string.
PROJECTS_DIR_RE = re.compile(r"\.claude/projects(?:/|$)", re.IGNORECASE)

REASON = (
    f"Blocked: these are raw session transcripts. Use `{TOOL}` instead — it "
    "indexes every one of these files (plus subagent runs, PRs, research "
    "reports and chat exports) with full-text ranking, which grepping the "
    "JSONL cannot give you. Call it with a free-text query, or "
    "`mcp__aggregator__aggregator_capabilities` first for the DSL keys. "
    "If you need a specific session's raw turns, that is "
    f"`{TOOL}` with dsl=\"session:<id>\" and drilldown=True. Only if the "
    "aggregator has genuinely not indexed what you need, say so and use "
    "`Bash` to read the file directly."
)


def _targets(tool_name: str, tool_input: dict) -> list[str]:
    """Path-ish strings this call would touch, per tool shape."""
    if tool_name == "Read":
        return [str(tool_input.get("file_path") or "")]
    if tool_name == "Grep":
        return [str(tool_input.get("path") or ""), str(tool_input.get("glob") or "")]
    return []


def _is_transcript_sweep(tool_name: str, targets: list[str]) -> bool:
    for t in targets:
        if not t:
            continue
        if TRANSCRIPT_RE.search(t):
            return True
        # Grep scoped at the projects tree hits every transcript in it.
        if tool_name == "Grep" and PROJECTS_DIR_RE.search(t):
            return True
    return False


def main() -> int:
    if os.environ.get("AGGREGATOR_GUARD_OFF"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open: a parse bug must not wedge every tool call

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    if not _is_transcript_sweep(tool_name, _targets(tool_name, tool_input)):
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

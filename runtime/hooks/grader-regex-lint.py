#!/usr/bin/env python3
"""PostToolUse lint: warn when an agent-e2e grader gains a regex or grep.

An agent's free text (its final message, a dispatch prompt, a file it wrote)
is graded through the schema-enforced judge in
`tests/agent-e2e/_lib/structured_judge.py` (skill: parsing-subagent-output),
or by its actions. A regex over prose grows a bypass per patch and never
converges.

Scope: Write/Edit/MultiEdit of a .sh or .py file under a `tests/agent-e2e/`
directory. Only the text being written is scanned, so legacy lines elsewhere
in the file stay quiet. A line carrying `regex-ok: <reason>` is silenced: use
it when the regex reads an action record (commands.txt, git output, a ledger
column), not prose.

Advisory only: never blocks, exits 0 on any problem.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

PY_USE = re.compile(r"\bre\.(search|match|fullmatch|findall|finditer|compile|sub|split)\(")
SH_USE = re.compile(r"(^|[\s|;&(`$])(e|f)?grep\b")
MARKER = "regex-ok:"
SCOPE = "/tests/agent-e2e/"


def flagged(text: str) -> list[str]:
    """Lines of `text` that use regex/grep, comments and silenced lines excluded.

    Both patterns run on every file: a .sh grader often embeds Python in a
    heredoc. A trailing ` #` comment is cut before matching."""
    out = []
    for line in text.splitlines():
        code = line.strip()
        if not code or code.startswith("#") or MARKER in line:
            continue
        if PY_USE.search(code.split(" #", 1)[0]) or SH_USE.search(code.split(" #", 1)[0]):
            out.append(code)
    return out


def gained(tool_input: dict) -> list[str]:
    """Flagged lines the write adds: an Edit's old_string hits are subtracted."""
    if "content" in tool_input:
        return flagged(tool_input.get("content") or "")
    pairs = tool_input.get("edits") or [tool_input]
    out: list[str] = []
    for edit in pairs:
        before = Counter(flagged(edit.get("old_string") or ""))
        for line in flagged(edit.get("new_string") or ""):
            if before[line]:
                before[line] -= 1
            else:
                out.append(line)
    return out


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or ""
        suffix = path[path.rfind("."):] if "." in path else ""
        if SCOPE not in path or suffix not in (".sh", ".py"):
            return
        hits = gained(tool_input)
    except (ValueError, AttributeError, TypeError, OSError):
        return
    if not hits:
        return
    shown = "\n".join(f"  {h[:160]}" for h in hits[:8])
    msg = (f"grader-regex-lint: {path} gained {len(hits)} regex/grep line(s):\n{shown}\n"
           "In agent-e2e graders, an agent's free text is graded with "
           "tests/agent-e2e/_lib/structured_judge.py (skill: parsing-subagent-output), "
           "or the grader reads the agent's actions. A line whose regex reads an action "
           "record (commands.txt, git output, a ledger column) is silenced by a "
           "`regex-ok: <reason>` comment on that line.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": msg}}))


if __name__ == "__main__":
    main()

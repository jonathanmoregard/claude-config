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

PY_USE = re.compile(r"\bre\.(search|match|fullmatch|findall|finditer|compile|sub|split)\(")
SH_USE = re.compile(r"(^|[\s|;&(`$])(e|f)?grep\b")
MARKER = "regex-ok:"
SCOPE = "/tests/agent-e2e/"


def flagged(text: str, suffix: str) -> list[str]:
    """Lines of `text` that use regex/grep, comments and silenced lines excluded."""
    pat = PY_USE if suffix == ".py" else SH_USE
    out = []
    for line in text.splitlines():
        code = line.strip()
        if code and not code.startswith("#") and MARKER not in line and pat.search(code):
            out.append(code)
    return out


def written(tool_input: dict) -> str:
    if "content" in tool_input:
        return tool_input.get("content") or ""
    if "edits" in tool_input:
        return "\n".join(e.get("new_string") or "" for e in tool_input["edits"])
    return tool_input.get("new_string") or ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or ""
        suffix = path[path.rfind("."):] if "." in path else ""
        if SCOPE not in path or suffix not in (".sh", ".py"):
            return
        hits = flagged(written(tool_input), suffix)
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

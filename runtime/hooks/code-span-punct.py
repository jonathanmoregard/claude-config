#!/usr/bin/env python3
"""MessageDisplay hook: show a space instead of punctuation glued to a code span.

A copy of `FOO_KEY`. by double-click carries the dot into a secret name or
command. On screen, a . , ; or : directly after an inline code span (optionally
after closing ** * or _) becomes a space; the stored message and what the model
sees are unchanged (MessageDisplay is display-only).

Invariants:
- Text inside a fenced block is never touched. A fence opens on a run of 3+
  backticks or tildes (after any blockquote markers and indentation) and closes
  only on a run of the same character at least as long with nothing after it,
  so a ```` block may contain ``` lines. The open fence of each message is kept
  in a small per-message file across flushes until the final flush.
- Inline code spans are delimited by equal-length backtick runs, so text inside
  a ``span with `ticks`. inside`` is never touched.
- When in doubt nothing is rewritten. Any parse or I/O problem prints nothing,
  so the original text is displayed.
"""
import json
import os
import re
import sys
import tempfile

FENCE = re.compile(r"^(?:[ \t]*>)*[ \t]*(`{3,}|~{3,})(.*)$")
TICKS = re.compile(r"`+")
GLUED = re.compile(r"((?:\*\*|\*|_)?)[.,;:](?=\s|$)")


def fence_line(line: str, fence: str | None) -> tuple[bool, str | None]:
    """Return (is_fence_line, fence_after_line) for one line."""
    match = FENCE.match(line.rstrip("\n"))
    if not match:
        return False, fence
    run, rest = match.groups()
    if fence is None:
        if run[0] == "`" and "`" in rest:
            return False, None  # not a fence: a backtick info string has no backticks
        return True, run
    if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
        return True, None
    return False, fence


def rewrite_inline(line: str) -> str:
    out, pos = [], 0
    while True:
        opener = TICKS.search(line, pos)
        if not opener:
            break
        closer = opener
        while True:
            closer = TICKS.search(line, closer.end())
            if not closer or len(closer.group()) == len(opener.group()):
                break
        if not closer:
            break  # unmatched run: the rest of the line is literal text
        out.append(line[pos:closer.end()])
        pos = closer.end()
        glued = GLUED.match(line, pos)
        if glued:
            out.append(glued.group(1) + " ")
            pos = glued.end()
    out.append(line[pos:])
    return "".join(out)


def rewrite(delta: str, fence: str | None) -> tuple[str, str | None]:
    out = []
    for line in delta.splitlines(keepends=True):
        is_fence, after = fence_line(line, fence)
        if not is_fence and fence is None:
            line = rewrite_inline(line)
        fence = after
        out.append(line)
    return "".join(out), fence


def main() -> None:
    try:
        event = json.load(sys.stdin)
        delta = event["delta"]
        message_id = re.sub(r"[^A-Za-z0-9-]", "", str(event["message_id"]))
        final = bool(event.get("final"))
    except (ValueError, KeyError, TypeError):
        return
    state = os.path.join(tempfile.gettempdir(), f"code-span-punct-{os.getuid()}-{message_id}")
    try:
        fence = open(state).read() or None if os.path.exists(state) else None
        shown, fence = rewrite(delta, fence)
        if final or fence is None:
            if os.path.exists(state):
                os.remove(state)
        else:
            with open(state, "w") as handle:
                handle.write(fence)
    except OSError:
        return
    if shown != delta:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "MessageDisplay", "displayContent": shown}}))


if __name__ == "__main__":
    main()

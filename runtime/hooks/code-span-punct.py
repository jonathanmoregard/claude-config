#!/usr/bin/env python3
"""MessageDisplay hook: show a space instead of punctuation glued to a code span.

A copy of `FOO_KEY`. by double-click carries the dot into a secret name or
command. On screen, `X`. `X`, `X`; `X`: become `X` followed by a space; the
stored message and what the model sees are unchanged (MessageDisplay is
display-only). Lines inside fenced code blocks are never touched. Deltas arrive
as whole lines per flush, so the open-fence state of each message is kept in a
small per-message file until its final flush.

Any parse or I/O problem prints nothing, so the original text is displayed.
"""
import json
import os
import re
import sys
import tempfile

GLUED = re.compile(r"(`[^`\n]+`(?:\*\*|\*|_)?)[.,;:](?=\s|$)")
FENCE = re.compile(r"^\s*(```|~~~)")


def rewrite(delta: str, in_fence: bool) -> tuple[str, bool]:
    out = []
    for line in delta.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            line = GLUED.sub(r"\1 ", line)
        out.append(line)
    return "".join(out), in_fence


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
        in_fence = os.path.exists(state)
        shown, in_fence = rewrite(delta, in_fence)
        if final or not in_fence:
            if os.path.exists(state):
                os.remove(state)
        else:
            open(state, "w").close()
    except OSError:
        return
    if shown != delta:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "MessageDisplay", "displayContent": shown}}))


if __name__ == "__main__":
    main()

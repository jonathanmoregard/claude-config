#!/usr/bin/env python3
"""PreToolUse: refuse a file write that would put a redaction marker over a secret.

Secrets are redacted out of every tool result the model reads
(``tool-output-redact.py``, ``bash-output-guard.py``): the model sees
``[REDACTED:<rule>:<n>chars]`` where the file holds the real value. The file
on disk is untouched — until the model writes back what it saw. Read an
``.env``, change one line, Write the whole file: the secret on disk is now the
marker, and the credential is gone. Redaction made that mistake possible, so
this guard ships with it.

THE RULE. For Write, Edit, MultiEdit and NotebookEdit: compute how many times
each distinct marker would appear in the file after the call, and refuse if
any marker would appear MORE often than it does on disk now. A file that
already holds a marker (notes about redaction, a test fixture) can be edited
around it; a call that ADDS one is refused with a recovery hint.

  * Write:        count in ``content`` vs count in the existing file (0 if new).
  * Edit:         the edit applied to the file text (``replace_all`` honoured).
                  An ``old_string`` that is not in the file makes the Edit fail
                  on its own, so nothing is refused for it.
  * MultiEdit:    the edits applied in order, the same way.
  * NotebookEdit: count in ``new_source`` vs count anywhere in the notebook.

COST. The raw stdin is checked for the literal ``[REDACTED:`` before anything
is parsed; a call without it (all but ~0.01% of 80,273 past file-tool calls)
exits at once and the target file is never read.

FAIL-SAFE. Once a marker is in the input, every fault REFUSES: unparseable
input, an unreadable target, a target too large to read. Refusing a write is
recoverable (the model is told why and what to do instead); a write that lands
destroys a credential nobody can recover from the transcript, because the
transcript only ever held the marker.
"""
from __future__ import annotations

import json
import os
import re
import sys

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
NEEDLE = "[REDACTED:"
MARKER_RE = re.compile(r"\[REDACTED:[A-Za-z0-9_.\-]+:\d+chars\]")
MAX_READ_BYTES = 16 * 1024 * 1024

RECOVER = (
    "The value you saw is a redaction marker: the real secret is still on disk, "
    "and this write would replace it with the marker. Recover: leave the "
    "secret's line out of the edit — anchor Edit's old_string on the "
    "neighbouring lines, or change other lines with a line-addressed edit "
    "(e.g. `sed -i '<N>s/.../.../'` on a line that does not hold the secret). "
    "If you need the secret line itself changed, ask the user to do it. If the "
    "marker text is meant literally (docs, tests), write it so it does not form "
    "a complete marker, e.g. `[REDACTED:<rule>:<n>chars]`."
)


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"redaction-marker-write-guard: {reason} {RECOVER}",
            }
        },
        sys.stdout,
    )
    sys.stdout.flush()
    sys.exit(0)


def counts(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in MARKER_RE.findall(text):
        out[m] = out.get(m, 0) + 1
    return out


def read_target(path: str) -> str | None:
    """The file's text, ``None`` if it does not exist. Faults refuse."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        deny(f"the target {path!r} could not be examined ({type(exc).__name__}), so "
             "the guard cannot tell whether this write overwrites a secret.")
    if size > MAX_READ_BYTES:
        deny(f"the target {path!r} is {size} bytes, past the {MAX_READ_BYTES}-byte "
             "limit this guard reads.")
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError as exc:
        deny(f"the target {path!r} could not be read ({type(exc).__name__}), so the "
             "guard cannot tell whether this write overwrites a secret.")


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> str:
    if not old or old not in text:
        return text  # the Edit itself fails; nothing lands
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def check(before: dict[str, int], after: dict[str, int], path: str) -> None:
    added = sorted(m for m, n in after.items() if n > before.get(m, 0))
    if added:
        shown = ", ".join(added[:3]) + (f" (+{len(added) - 3} more)" if len(added) > 3 else "")
        deny(f"this call would write {shown} into {path!r}, which does not hold "
             "that marker there now.")


def main() -> int:
    raw = sys.stdin.read()
    if NEEDLE not in raw:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        deny("the hook input carries a redaction marker but is not parseable JSON.")
    if not isinstance(payload, dict) or payload.get("tool_name") not in FILE_TOOLS:
        return 0
    tool = payload["tool_name"]
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        deny("the tool input carries a redaction marker but is not an object.")
    if not MARKER_RE.search(json.dumps(ti)):
        return 0  # the needle without a complete marker: nothing to protect

    path = ti.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if not isinstance(path, str) or not path:
        deny("the call carries a redaction marker but names no target file.")
    path = os.path.expanduser(path)
    disk = read_target(path)
    before = counts(disk or "")

    if tool == "Write":
        content = ti.get("content")
        if not isinstance(content, str):
            deny("Write content is not a string.")
        check(before, counts(content), path)
    elif tool == "NotebookEdit":
        source = ti.get("new_source")
        added = counts(source) if isinstance(source, str) else {}
        check(before, added, path)
    else:
        edits = ti.get("edits") if tool == "MultiEdit" else [ti]
        if not isinstance(edits, list):
            deny("MultiEdit edits is not a list.")
        text = disk or ""
        for e in edits:
            if not isinstance(e, dict):
                deny("an edit is not an object.")
            old, new = e.get("old_string"), e.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                deny("an edit's old_string/new_string is not a string.")
            text = apply_edit(text, old, new, bool(e.get("replace_all")))
        check(before, counts(text), path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — refuse, never wave through
        deny(f"the guard failed ({type(exc).__name__}: {exc}).")

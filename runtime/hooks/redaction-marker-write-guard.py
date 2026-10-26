#!/usr/bin/env python3
"""PreToolUse: refuse a file write that would put a redaction marker over a secret.

Secrets are redacted out of every tool result the model reads
(``tool-output-redact.py``, ``bash-output-guard.py``): the model sees
``[REDACTED:<rule>:<n>chars]`` where the file holds the real value (the
aggregator's memory search still returns the older short ``[REDACTED:<rule>]``;
both spellings count). The file
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
  * NotebookEdit: the notebook's count with the edited cell's source taken out
                  and ``new_source`` put in (insert: only put in; delete adds
                  nothing). A cell the guard cannot find takes nothing out.

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
import sys
from collections import Counter
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redaction_marker import MARKER_RE, PREFIX as NEEDLE  # noqa: E402

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MAX_READ_BYTES = 16 * 1024 * 1024

RECOVER = (
    "The value you saw is a redaction marker: the real secret is still on disk, "
    "and this write would replace it with the marker. Recover: leave the "
    "secret's line out of the edit by anchoring Edit's old_string on the "
    "neighbouring lines. If the secret's line itself must change, ask the user "
    "to do it."
)


def deny(reason: str) -> NoReturn:
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


def counts(text: str) -> Counter:
    return Counter(MARKER_RE.findall(text))


def notebook_cell_source(notebook: str | None, cell_id) -> str:
    """The source of the cell NotebookEdit resolves ``cell_id`` to (its id, or the
    ``cell-<n>`` index form), ``""`` when there is none. An unparseable notebook refuses."""
    if notebook is None or not isinstance(cell_id, str):
        return ""
    try:
        cells = json.loads(notebook)["cells"]
    except (ValueError, KeyError, TypeError):
        deny("the notebook is not parseable, so the guard cannot tell which cell this edit replaces.")
    if not isinstance(cells, list):
        deny("the notebook's cells are not a list, so the guard cannot tell which cell this edit replaces.")
    cell = next((c for c in cells if isinstance(c, dict) and c.get("id") == cell_id), None)
    index = cell_id.removeprefix("cell-")
    if cell is None and index.isdigit() and int(index) < len(cells):
        cell = cells[int(index)]
    source = cell.get("source", "") if isinstance(cell, dict) else ""
    return "".join(source) if isinstance(source, list) else str(source)


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


def check(before: Counter, after: Counter, path: str) -> None:
    added = sorted(after - before)
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
        mode = ti.get("edit_mode") or "replace"
        if mode == "delete":
            return 0  # removes a cell, writes none
        source = ti.get("new_source")
        if not isinstance(source, str):
            deny("NotebookEdit new_source is not a string.")
        replaced = notebook_cell_source(disk, ti.get("cell_id")) if mode == "replace" else ""
        check(before, before - counts(replaced) + counts(source), path)
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

#!/usr/bin/env python3
"""SessionStart check: tell the session which units are recorded as failing.

System and user units write the same durable record shape. The system directory
is root-owned; the user directory is writable by the desktop user and therefore
has a stricter trust boundary.

Record (JSON object): unit, result, exit_status, failed_at, summary, inspect.

SILENCE IS THE BUDGET. No directory, no records: print nothing. Every record
present speaks; an unreadable record speaks too, because silence must mean
"nothing recorded as failing", never "could not tell". Dot-prefixed files are
the writer's in-flight temp files and are skipped.

Record content is collapsed to one line and length-capped. User records never
provide executable inspection guidance. Stdlib only, `python3 -S -E` safe,
fail-open but never silent.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys

DEFAULT_DIR = "/var/lib/unit-failures"
DEFAULT_USER_DIR = os.path.expanduser("~/.local/state/claude-unit-failures")
FIELD_MAX = 300
LINE_MAX = 900


# C0/C1 controls (terminal escapes, NUL, BEL, BS) and bidi overrides are not
# whitespace, so str.split() alone would pass them through verbatim.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def one_line(value: object, limit: int = FIELD_MAX) -> str:
    text = " ".join(_CONTROL.sub(" ", str(value)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(path: str, *, trusted_inspect: bool) -> str:
    name = os.path.basename(path)[: -len(".json")]
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"{name}: failure record unreadable ({type(exc).__name__}) — {path}; the unit is probably failing"
    if not isinstance(rec, dict):
        return f"{name}: failure record unreadable (not an object) — {path}; the unit is probably failing"
    unit = one_line(rec.get("unit") or name, 80)
    when = one_line(rec.get("failed_at") or "?", 40)
    result = one_line(rec.get("result") or "?", 40)
    status = one_line(rec.get("exit_status") or "?", 40)
    summary = one_line(rec.get("summary") or "")
    if trusted_inspect:
        inspect = one_line(rec.get("inspect") or f"journalctl -u {unit} -n 100", 120)
    else:
        record_name = os.path.basename(path)[: -len(".json")]
        if re.fullmatch(r"[A-Za-z0-9._-]+", record_name):
            safe_unit = record_name if record_name.endswith(".service") else record_name + ".service"
            inspect = f"journalctl --user -u {safe_unit} -n 100"
        else:
            inspect = "systemctl --user --failed"
    line = f"{unit} FAILED at {when} (result={result} status={status})"
    if summary:
        line += f": {summary}"
    line += f" — inspect: {inspect}"
    return line


def read_directory(directory: str, *, trusted_inspect: bool) -> list[str]:
    """A missing directory is empty; every other read failure must speak."""
    try:
        names = sorted(
            n for n in os.listdir(directory) if n.endswith(".json") and not n.startswith(".")
        )
    except FileNotFoundError:
        return []
    except Exception as exc:  # fail-open, but never silently
        return [f"could not read {directory}: {type(exc).__name__}"]
    return [
        describe(os.path.join(directory, name), trusted_inspect=trusted_inspect)
        for name in names
    ]


def main() -> int:
    with contextlib.suppress(OSError):  # hook stdin is informational only
        sys.stdin.read()
    system_directory = os.environ.get("UNIT_FAILURES_DIR") or DEFAULT_DIR
    lines = read_directory(system_directory, trusted_inspect=True)
    lines.extend(read_directory(DEFAULT_USER_DIR, trusted_inspect=False))
    for line in lines:
        print(f"[unit-failure] {one_line(line, LINE_MAX)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

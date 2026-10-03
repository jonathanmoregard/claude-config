#!/usr/bin/env python3
"""SessionStart check: tell the session which system units are failing.

A system unit's `OnFailure=` notifier reaches the journal and a desktop toast,
neither of which a Claude session reads (2026-08-31 constraint: failure
notifications must reach Claude). nixos-config therefore has failing system
units write a durable record, `/var/lib/unit-failures/<unit>.json`, and
delete it on the next successful run. First producer: nix-gc.service
(nixos-config modules/nixos/nix-gc.nix), whose keep-recent pin step fails
closed — so a broken `nix path-info` stops every scheduled GC.

Same shape as `claude-pull-health.py` / `ai-router-health.py`, but generic
over units: any system unit that writes the record shape below is covered
without a new hook.

Record (JSON object): unit, result, exit_status, failed_at, summary, inspect.

SILENCE IS THE BUDGET. No directory, no records: print nothing. Every record
present speaks; an unreadable record speaks too, because silence must mean
"nothing recorded as failing", never "could not tell". Dot-prefixed files are
the writer's in-flight temp files and are skipped.

Record content is untrusted text from a root service: each announcement is
collapsed to one line and length-capped. Stdlib only, `python3 -S -E` safe,
fail-open but never silent. Test: tests/test_unit_failure_health_hook.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

DEFAULT_DIR = "/var/lib/unit-failures"
FIELD_MAX = 300
LINE_MAX = 900


def one_line(value: object, limit: int = FIELD_MAX) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(path: str) -> str:
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
    inspect = one_line(rec.get("inspect") or f"journalctl -u {unit} -n 100", 120)
    line = f"{unit} FAILED at {when} (result={result} status={status})"
    if summary:
        line += f": {summary}"
    line += f" — inspect: {inspect}"
    return line


def main() -> int:
    with contextlib.suppress(OSError):  # hook stdin is informational only
        sys.stdin.read()
    directory = os.environ.get("UNIT_FAILURES_DIR") or DEFAULT_DIR
    try:
        if not os.path.isdir(directory):
            return 0
        names = sorted(
            n for n in os.listdir(directory) if n.endswith(".json") and not n.startswith(".")
        )
        lines = [describe(os.path.join(directory, n)) for n in names]
    except Exception as exc:  # fail-open, but never silently
        lines = [f"could not read {directory}: {type(exc).__name__}"]
    for line in lines:
        print(f"[unit-failure] {one_line(line, LINE_MAX)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

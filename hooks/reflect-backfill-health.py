#!/usr/bin/env python3
"""SessionStart check: tell the session when the reflection backfill drain broke.

`skills/session-reflect/backfill.sh` runs from a systemd user timer on Fridays
(nixos-config `home/session-reflect-backfill.nix`) and recovers SessionEnd
reflections a spend cap dropped. Its unit's OnFailure reaches the journal and
the desktop, which no Claude session reads; per the 2026-08-31 constraint the
drain also writes a durable outcome record, `<REFLECT_ROOT>/backfill/last-run.json`,
and this hook states it in the session's context. Same shape as
`hooks/ai-router-health.py`.

Silence is the budget: a healthy run, an empty backlog, a closed budget gate
and "no run yet" print nothing. What speaks:
  * status `error`            — the drain crashed mid-run;
  * status `gate_unavailable` — it failed closed because a budget reading
                                 (weekly or monthly) was missing;
  * status `started` for longer than the unit may run — killed or hung;
  * an unreadable record.
Stdlib only, fail-open, never raises. Test: `tests/test_reflect_backfill_health.py`.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone

HUNG_AFTER_S = 4 * 3600   # the unit's RuntimeMaxSec is 3h
LOG_HINT = "journalctl --user -u session-reflect-backfill"


def record_path() -> str:
    root = os.environ.get("REFLECT_ROOT") or os.path.join(
        os.path.expanduser("~"), ".claude", "reflections")
    return os.path.join(root, "backfill", "last-run.json")


def now() -> float:
    override = os.environ.get("REFLECT_BACKFILL_HEALTH_NOW")
    return float(override) if override else time.time()


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def check(path: str, at: float) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as exc:
        return [f"last-run record unreadable ({type(exc).__name__}) — {path}"]
    if not isinstance(rec, dict):
        return [f"last-run record is not an object — {path}"]
    status = rec.get("status")
    if status == "error":
        return [f"last drain CRASHED ({rec.get('reason')}, exit {rec.get('exit_code')}) "
                f"at {rec.get('finished_at')}; dropped reflections are not being recovered. {LOG_HINT}"]
    if status == "gate_unavailable":
        detail = (rec.get("gate") or {}).get("detail") or "budget reading missing"
        return [f"last drain failed closed at {rec.get('finished_at')}: {detail}. "
                f"Check `ai-router quota --fresh --json`. {LOG_HINT}"]
    if status == "started":
        started = parse_ts(rec.get("started_at"))
        if started is None or at - started > HUNG_AFTER_S:
            return [f"drain started {rec.get('started_at')} never finished (killed or hung). {LOG_HINT}"]
    return []


def main() -> int:
    with contextlib.suppress(OSError):
        sys.stdin.read()
    try:
        lines = check(record_path(), now())
    except Exception as exc:  # fail-open, but never silently
        lines = [f"health check could not evaluate: {type(exc).__name__}: {exc}"[:200]]
    for line in lines:
        print(f"[reflect-backfill] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

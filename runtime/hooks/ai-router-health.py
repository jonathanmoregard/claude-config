#!/usr/bin/env python3
"""SessionStart check: tell the session when ai-router's background pieces broke.

WHY A HOOK. The ranking-evidence updater (`tools/ai-router/update_ranking.py`)
runs from a systemd user timer declared in nixos-config `home/ai-router.nix`.
That unit's `OnFailure` notifier writes a journal line and a desktop toast —
channels no Claude session reads. The 2026-08-31 constraint ("failure
notifications must reach CLAUDE, not just the desktop") is met the way
`hooks/claude-pull-health.py` meets it: the unit wrapper writes a durable
outcome record, `ranking.last-run.json`, before and after every run, and this
hook states the fact in the session's own context. Same shape, second probe;
a third notification system would be its own bug.

SILENCE IS THE BUDGET. Healthy prints nothing. "Could not tell" — no record
where the tool has clearly been used, an unreadable record, a run that never
finished, a run older than the timer's cadence allows — speaks, so that
silence keeps meaning "checked and healthy".

WHAT ELSE SPEAKS. A provider that ai-router has marked rejected (quota error
or rate-limit Stop) is announced with its reset time, so a session that keeps
getting Claude picks knows Codex is parked rather than mis-ranked. A pending
ranking disagreement (evidence vs operator pins) is named once per session;
the proposal file carries the details.

Contract: `build-contract-ai-router.md` §State directory (record shapes),
`design-provider-router.md` §C (probes). Stdlib only, `python3 -S -E` safe,
fail-open: an exception in the check itself is reported as "could not
evaluate", never raised. Test: `tests/test_ai_router_health.py`.
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

STALE_AFTER_S = 3 * 86400          # timer is daily; three missed days is a fault
UNFINISHED_AFTER_S = 3600          # a run still "started" after an hour hung
REJECTED_DEFAULT_TTL_S = 3600      # mirrors models.toml rejected_default_ttl_s
LOG_HINT = "~/.local/share/ai-router/ranking.log; journalctl --user -u ai-router-ranking"


def state_dir() -> str:
    return os.environ.get("AI_ROUTER_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "ai-router"
    )


def now() -> float:
    override = os.environ.get("AI_ROUTER_HEALTH_NOW")
    return float(override) if override else time.time()


def parse_ts(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for parse in (float, _iso_epoch):
            try:
                return parse(text)
            except ValueError:
                continue  # not this format; try the next parser
    return None


def _iso_epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def load_json(path: str) -> tuple[object | None, str | None]:
    """Return (payload, error). Missing file → (None, None); bad file → (None, why)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:120]


def check_last_run(state: str, at: float) -> list[str]:
    path = os.path.join(state, "ranking.last-run.json")
    record, err = load_json(path)
    if err:
        return [f"ranking timer record unreadable ({err}) — {path}"]
    if record is None:
        return [
            "ranking timer has never run (unit ai-router-ranking from nixos-config "
            "home/ai-router.nix not installed or not yet fired); ranking evidence is absent"
        ]
    if not isinstance(record, dict):
        return [f"ranking timer record unreadable (not an object) — {path}"]
    started = parse_ts(record.get("started_at"))
    finished = parse_ts(record.get("finished_at"))
    exit_code = record.get("exit_code")
    lines: list[str] = []
    if finished is None:
        if started is None or at - started > UNFINISHED_AFTER_S:
            lines.append(
                f"ranking timer run started {fmt_ts(started) if started else '?'} did not finish — {LOG_HINT}"
            )
        return lines
    if record.get("skipped"):
        lines.append(
            "ranking timer ran but found no ~/.claude/tools/ai-router/update_ranking.py "
            "(skipped) — the .claude side is not deployed on this host"
        )
    elif isinstance(exit_code, int) and exit_code != 0:
        lines.append(f"ranking timer FAILED (exit {exit_code}) at {fmt_ts(finished)} — {LOG_HINT}")
    age = at - finished
    if age > STALE_AFTER_S:
        lines.append(
            f"ranking evidence stale: last timer run {age / 86400:.1f} days ago ({fmt_ts(finished)}) — {LOG_HINT}"
        )
    return lines


def check_ranking_auto(state: str) -> list[str]:
    path = os.path.join(state, "ranking.auto.json")
    record, err = load_json(path)
    if err:
        return [f"ranking.auto.json unreadable ({err}) — {path}"]
    if not isinstance(record, dict):
        return [] if record is None else [f"ranking.auto.json unreadable (not an object) — {path}"]
    lines: list[str] = []
    sources = record.get("sources") or []
    if sources and all(not s.get("ok") for s in sources if isinstance(s, dict)):
        failed = ", ".join(
            f"{s.get('name', '?')} ({str(s.get('error', ''))[:60]})" for s in sources if isinstance(s, dict)
        )
        lines.append(f"ranking evidence: every source failed on the last run — {failed}")
    if record.get("disagreements"):
        lines.append(
            f"ranking evidence disagrees with the operator pins for "
            f"{len(record['disagreements'])} model(s) — see {os.path.join(state, 'ranking.proposal.md')}"
        )
    return lines


def check_rejections(state: str, at: float) -> list[str]:
    lines: list[str] = []
    for path in sorted(glob.glob(os.path.join(state, "quota", "*.rejected.json"))):
        record, err = load_json(path)
        if err or not isinstance(record, dict):
            lines.append(f"rejection record unreadable — {path}")
            continue
        until = parse_ts(record.get("until"))
        if until is None:
            observed = parse_ts(record.get("observed_at"))
            until = (observed + REJECTED_DEFAULT_TTL_S) if observed is not None else None
        if until is None or until <= at:
            continue
        provider = record.get("provider") or os.path.basename(path).split(".")[0]
        reason = str(record.get("reason") or "")[:100]
        lines.append(
            f"picks avoid {provider}: marked rejected until {fmt_ts(until)}"
            + (f" ({reason})" if reason else "")
        )
    return lines


def main() -> int:
    with contextlib.suppress(OSError):  # hook stdin is informational only
        sys.stdin.read()
    try:
        state = state_dir()
        if not os.path.isdir(state):
            return 0
        at = now()
        lines = check_last_run(state, at) + check_ranking_auto(state) + check_rejections(state, at)
    except Exception as exc:  # fail-open, but never silently
        lines = [f"health check could not evaluate: {type(exc).__name__}: {exc}"[:200]]
    for line in lines:
        print(f"[ai-router] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

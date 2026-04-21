#!/usr/bin/env python3
"""
UserPromptSubmit hook: nudge Claude about pending instincts ready to evolve.

- Queries instinct-cli for a pending count.
- Rate-limits: fires at most once every NUDGE_EVERY_N prompt submissions
  so it doesn't spam.
- The Monday cron flag (~/.claude/homunculus/.evolve-reminder) forces the
  next fire immediately (bypasses the rate limit once), then resumes normal
  cadence.
- Message is addressed to Claude as context — Claude should relay it at a
  natural break point, not interrupt the current task.

State file: ~/.claude/tmp/.evolve-nudge-state.json
    {"counter": N, "last_fired_counter": M}

Pattern borrowed from wellbeing/habit-nudge.py.
"""
import json
import subprocess
from pathlib import Path

STATE = Path.home() / ".claude" / "tmp" / ".evolve-nudge-state.json"
FLAG = Path.home() / ".claude" / "homunculus" / ".evolve-reminder"
CLI = Path.home() / ".claude" / "skills" / "continuous-learning-v2" / "scripts" / "instinct-cli.py"

NUDGE_EVERY_N = 10
STATUS_TIMEOUT_S = 3


def _load_state() -> dict:
    if not STATE.exists():
        return {"counter": 0, "last_fired_counter": -NUDGE_EVERY_N}
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"counter": 0, "last_fired_counter": -NUDGE_EVERY_N}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def _pending_count() -> int | None:
    """Return total pending instinct count, or None if CLI unavailable."""
    if not CLI.exists():
        return None
    try:
        result = subprocess.run(
            ["python3", str(CLI), "status"],
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("INSTINCT STATUS"):
            parts = stripped.split("-")
            if len(parts) >= 2:
                try:
                    return int(parts[1].strip().split()[0])
                except (ValueError, IndexError):
                    return None
    return None


def main() -> int:
    state = _load_state()
    state["counter"] = state.get("counter", 0) + 1
    force = FLAG.exists()

    prompts_since_last = state["counter"] - state.get("last_fired_counter", 0)
    if not force and prompts_since_last < NUDGE_EVERY_N:
        _save_state(state)
        return 0

    count = _pending_count()
    if count is None or count <= 0:
        _save_state(state)
        return 0

    state["last_fired_counter"] = state["counter"]
    _save_state(state)
    if force:
        try:
            FLAG.unlink()
        except OSError:
            pass

    print(
        f"[instinct-nudge] There are {count} open instincts pending review. "
        "When it's a natural break point, remind the user to run /evolve "
        "(or /instinct-status to inspect first). Do not interrupt the current task to surface this."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

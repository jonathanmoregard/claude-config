"""SessionStart hook `hooks/reflect-backfill-health.py`: the backfill drain's
failures reach a Claude session; healthy and closed-gate runs stay silent."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, "hooks", "reflect-backfill-health.py")
NOW = 1790974800.0  # 2026-10-02T21:00:00Z


def run(root: str, record: object | None, raw: str | None = None) -> str:
    d = os.path.join(root, "backfill")
    os.makedirs(d, exist_ok=True)
    if raw is not None:
        with open(os.path.join(d, "last-run.json"), "w") as fh:
            fh.write(raw)
    elif record is not None:
        with open(os.path.join(d, "last-run.json"), "w") as fh:
            json.dump(record, fh)
    env = dict(os.environ, REFLECT_ROOT=root, REFLECT_BACKFILL_HEALTH_NOW=str(NOW))
    out = subprocess.run([sys.executable, HOOK], input="{}", capture_output=True,
                         text=True, env=env, check=True)
    return out.stdout


@pytest.mark.parametrize("record", [
    None,
    {"status": "ok", "reason": "drained", "recovered": 3},
    {"status": "ok", "reason": "budget_cap_hit"},
    {"status": "ok", "reason": "not_friday"},
    {"status": "started", "started_at": "2026-10-02T20:00:00Z"},
])
def test_silent_when_healthy(tmp_path, record):
    assert run(str(tmp_path), record) == ""


def test_crash_speaks(tmp_path):
    out = run(str(tmp_path), {"status": "error", "reason": "drain_crashed", "exit_code": 1})
    assert "CRASHED" in out


def test_fail_closed_speaks_with_detail(tmp_path):
    out = run(str(tmp_path), {"status": "gate_unavailable",
                              "gate": {"detail": "no machine-readable monthly spend reading"}})
    assert "monthly spend" in out


def test_hung_run_speaks(tmp_path):
    assert "never finished" in run(str(tmp_path), {"status": "started", "started_at": "2026-10-02T10:00:00Z"})


def test_unreadable_record_speaks(tmp_path):
    assert "unreadable" in run(str(tmp_path), None, raw="{nope")

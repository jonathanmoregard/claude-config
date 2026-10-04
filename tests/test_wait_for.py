"""wait-for must not report a run, PR or unit as settled while it is still running.

Each case replays an incident from 2026-10-04 against a scripted `gh` /
`systemctl` (tests/stubs/wait-for-fake.py) with `sleep` stubbed out, so a
whole wait finishes in well under a second.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WAIT_FOR = REPO / "scripts" / "wait-for"
FAKE = REPO / "tests" / "stubs" / "wait-for-fake.py"

RUN_DONE = {"status": "completed", "conclusion": "success", "name": "infra"}
RUN_BUSY = {"status": "in_progress", "conclusion": "", "name": "infra"}


def run_wait_for(tmp_path: Path, script: dict, *args: str) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gh", "systemctl", "sleep"):
        (bin_dir / name).symlink_to(FAKE)
    state = tmp_path / "script.json"
    state.write_text(json.dumps(script))
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SCRIPT": str(state),
        "FAKE_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(WAIT_FOR), *args, "--timeout", "600"],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines()


def test_run_keeps_waiting_when_watch_exits_early(tmp_path: Path) -> None:
    # 2026-10-04: `gh run watch` gave up on infra run 37186036691 while it was
    # in_progress; wait-for printed "in_progress" and returned.
    script = {"gh": {
        "run watch": [{"rc": 1, "stderr": "HTTP 403: API rate limit exceeded"},
                      {"rc": 0}],
        "run view": [{"stdout": RUN_BUSY}, {"stdout": RUN_DONE}],
    }}
    rc, out, calls = run_wait_for(tmp_path, script, "run", "37186036691", "-R", "o/r")
    assert "in_progress" not in out, out
    assert "completed success" in out, out
    assert rc == 0, out
    assert sum(c.startswith("gh run watch") for c in calls) == 2, calls


def test_run_failure_conclusion_exits_one(tmp_path: Path) -> None:
    script = {"gh": {
        "run watch": [{"rc": 1}],
        "run view": [{"stdout": {**RUN_DONE, "conclusion": "failure"}}],
    }}
    rc, out, _ = run_wait_for(tmp_path, script, "run", "1", "-R", "o/r")
    assert rc == 1, out
    assert "completed failure" in out, out


def test_pr_waits_on_actions_runs_when_checks_are_unreadable(tmp_path: Path) -> None:
    # 2026-10-04: the token lacks checks:read, so `gh pr checks` fails and
    # wait-for returned at once while PR #143's CI was still running.
    unreadable = {"rc": 1, "stderr": "GraphQL: Resource not accessible by personal access token"}
    script = {"gh": {
        "pr view": [{"stdout": {"state": "OPEN", "mergeable": "MERGEABLE",
                                "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}],
        "pr checks": [unreadable],
        "run list": [
            {"stdout": [{"status": "in_progress", "conclusion": "", "name": "CI"},
                        {"status": "completed", "conclusion": "success", "name": "iam"}]},
            {"stdout": [{"status": "completed", "conclusion": "success", "name": "CI"},
                        {"status": "completed", "conclusion": "failure", "name": "infra"}]},
        ],
    }}
    rc, out, calls = run_wait_for(tmp_path, script, "pr", "143", "-R", "o/r")
    assert sum(c.startswith("gh run list") for c in calls) >= 2, calls
    assert "failed=1" in out, out
    assert rc == 1, out


def test_pr_unreadable_checks_and_green_runs_exit_zero(tmp_path: Path) -> None:
    script = {"gh": {
        "pr view": [{"stdout": {"state": "OPEN", "mergeable": "MERGEABLE",
                                "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}],
        "pr checks": [{"rc": 1, "stderr": "Resource not accessible by personal access token"}],
        "run list": [{"stdout": [{"status": "completed", "conclusion": "success", "name": "CI"},
                                 {"status": "completed", "conclusion": "skipped", "name": "x"}]}],
    }}
    rc, out, _ = run_wait_for(tmp_path, script, "pr", "143", "-R", "o/r")
    assert rc == 0, out
    assert "failed=0" in out, out


def test_transient_unit_is_not_done_while_its_job_runs(tmp_path: Path) -> None:
    # 2026-10-03: a `systemd-run --user` gate (Type=simple, Transient=yes)
    # counted as "up" the moment it was active.
    running = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
               "Type": "simple", "Transient": "yes", "Result": "success",
               "ExecMainStatus": "0", "ExecMainStartTimestamp": "Sat 2026-10-04 08:00:00 CEST"}
    failed = {**running, "ActiveState": "failed", "SubState": "failed",
              "Result": "exit-code", "ExecMainStatus": "1"}
    script = {"systemctl": [running, running, running, failed]}
    rc, out, _ = run_wait_for(tmp_path, script, "unit", "gate", "--user")
    assert "result=exit-code" in out, out
    assert rc == 1, out


def test_collected_transient_unit_reports_success_without_defaults(tmp_path: Path) -> None:
    # A transient unit that exits 0 is garbage-collected; `systemctl show` then
    # answers with defaults (LoadState=not-found, Result=success). Only report
    # success because the unit was seen loaded and then vanished.
    running = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
               "Type": "simple", "Transient": "yes", "ExecMainStartTimestamp": "Sat 2026-10-04 08:00:00 CEST"}
    gone = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "ExecMainStatus": "0"}
    script = {"systemctl": [running, running, gone]}
    rc, out, _ = run_wait_for(tmp_path, script, "unit", "gate", "--user")
    assert rc == 0, out
    assert "collected" in out, out


def test_long_running_service_still_counts_as_up(tmp_path: Path) -> None:
    up = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
          "Type": "simple", "Transient": "no", "Result": "success", "ExecMainStatus": "0",
          "ExecMainStartTimestamp": "Sat 2026-10-04 08:00:00 CEST"}
    rc, out, _ = run_wait_for(tmp_path, {"systemctl": [up]}, "unit", "web", "--user")
    assert rc == 0, out
    assert "active/running" in out, out

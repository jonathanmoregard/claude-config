"""wait-for must not report a run, PR or unit as settled while it is still running.

Each case replays an incident from 2026-10-04 against a scripted `gh` /
`systemctl` (tests/stubs/wait-for-fake.py) with `sleep` stubbed out, so a
whole wait finishes in well under a second.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WAIT_FOR = REPO / "scripts" / "wait-for"
FAKE = REPO / "tests" / "stubs" / "wait-for-fake.py"

RUN_DONE = {"status": "completed", "conclusion": "success", "name": "infra"}
RUN_BUSY = {"status": "in_progress", "conclusion": "", "name": "infra"}


def run_wait_for(
    tmp_path: Path,
    script: dict,
    *args: str,
    wait_for: Path = WAIT_FOR,
    cwd: Path | None = None,
    timeout: str = "600",
) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("gh", "systemctl", "sleep"):
        link = bin_dir / name
        if not link.exists():
            link.symlink_to(FAKE)
    state = tmp_path / "script.json"
    scripted = json.loads(json.dumps(script))
    scripted.setdefault("gh", {}).setdefault("auth token", [{"rc": 0}])
    state.write_text(json.dumps(scripted))
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SCRIPT": str(state),
        "FAKE_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(wait_for), *args, "--timeout", timeout],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=60, check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines()


def sandbox_layout(tmp_path: Path, status_root: Path) -> Path:
    layout = tmp_path / "layout"
    scripts = layout / "scripts"
    policy_dir = layout / "mcps" / "git-broker"
    scripts.mkdir(parents=True)
    policy_dir.mkdir(parents=True)
    copied = scripts / "wait-for"
    shutil.copy2(WAIT_FOR, copied)
    (policy_dir / "config.json").write_text(
        json.dumps({"gitBroker": {"watchStatusDir": str(status_root)}}),
        encoding="utf-8",
    )
    return copied


def write_status(
    root: Path,
    *,
    state: str,
    exit_code: int | str | None = None,
    line: str = "settled",
    repo: str = "owner/repo",
    kind: str = "pr",
    number: int = 7,
) -> Path:
    owner, name = repo.split("/")
    path = root / owner / name / f"{kind}-{number}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"state": state, "kind": kind, "repo": repo, "number": number}
    if exit_code is not None:
        data.update({"exit": exit_code, "line": line})
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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


def test_sandbox_done_status_returns_host_line_and_exit(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    for exit_code in (0, 1, 2):
        write_status(status_root, state="done", exit_code=exit_code, line=f"host {exit_code}")
        rc, out, calls = run_wait_for(
            tmp_path,
            {"gh": {"auth token": [{"rc": 1}]}},
            "pr", "7", "-R", "Owner/Repo",
            wait_for=copied,
        )
        assert rc == exit_code, out
        assert f"host {exit_code}" in out, out
        assert [call for call in calls if call.startswith("gh ")] == ["gh auth token"], calls
        (tmp_path / "calls.log").write_text("")


def test_sandbox_watching_then_done_waits_and_reports(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    status = write_status(status_root, state="watching")
    done = {"state": "done", "kind": "pr", "repo": "owner/repo", "number": 7,
            "exit": 0, "line": "host finished"}
    script = {
        "gh": {"auth token": [{"rc": 1}]},
        "sleep": [{"write_json": str(status), "value": done}],
    }
    rc, out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )
    assert rc == 0, out
    assert "host finished" in out, out


def test_sandbox_missing_watch_has_actionable_error(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}},
        "run", "99", "-R", "owner/repo",
        wait_for=copied,
    )
    assert rc == 1
    assert "gh cannot authenticate here and no host watch exists" in out
    assert "git broker github tool (operation watch)" in out


def test_sandbox_watching_forever_times_out(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    write_status(status_root, state="watching", kind="run", number=99)
    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}, "sleep": [{"delay": 0.02}]},
        "run", "99", "-R", "owner/repo",
        wait_for=copied,
        timeout="1",
    )
    assert rc == 124, out
    assert "timed out" in out


def test_sandbox_repo_is_derived_from_origin_without_dash_r(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    write_status(status_root, state="done", exit_code=0, line="derived")
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Owner/Repo.git"],
        check=True,
    )
    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}},
        "pr", "7",
        wait_for=copied,
        cwd=repo,
    )
    assert rc == 0, out
    assert "derived" in out

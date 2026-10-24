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
import time
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
    extra_env: dict[str, str] | None = None,
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
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SCRIPT": str(state),
        "FAKE_LOG": str(log),
    }
    env.update(extra_env or {})
    (tmp_path / "home").mkdir(exist_ok=True)
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
    watch_id: str = "a" * 32,
    started_at: int = 1_700_000_000,
    finished_at: int = 1_700_000_100,
) -> Path:
    owner, name = repo.split("/")
    path = root / owner / name / f"{kind}-{number}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "state": state,
        "kind": kind,
        "repo": repo,
        "number": number,
        "watch_id": watch_id,
        "started_at": started_at,
    }
    if exit_code is not None:
        data.update({"exit": exit_code, "line": line, "finished_at": finished_at})
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
        write_status(
            status_root,
            state="done",
            exit_code=exit_code,
            line=f"host {exit_code}",
            watch_id=str(exit_code + 1) * 32,
            started_at=1_700_000_000 + exit_code,
        )
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
            "watch_id": "a" * 32,
            "started_at": 1_700_000_000, "finished_at": 1_700_000_100,
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


def test_sandbox_done_result_is_reported_once_per_watch(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    status = write_status(
        status_root,
        state="done",
        exit_code=2,
        line="first result",
        finished_at=1_700_000_100,
    )
    script = {"gh": {"auth token": [{"rc": 1}]}}

    first_rc, first_out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )
    marker = (
        tmp_path
        / "home"
        / ".cache"
        / "git-broker"
        / "watch-reported"
        / "owner"
        / "repo"
        / "pr-7"
    )
    assert marker.read_text(encoding="utf-8") == f"{'a' * 32}\n"
    second_rc, second_out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )

    assert first_rc == 2, first_out
    assert "first result" in first_out
    assert second_rc == 1, second_out
    assert (
        "the host watch result for pr 7 in owner/repo "
        "(finished 2023-11-14T22:15:00Z) was already reported; "
        "start a new watch for a fresh result"
    ) in second_out

    write_status(
        status_root,
        state="done",
        exit_code=0,
        line="fresh result",
        watch_id="b" * 32,
        started_at=1_700_000_001,
        finished_at=1_700_000_200,
    )
    third_rc, third_out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )
    assert third_rc == 0, third_out
    assert "fresh result" in third_out
    assert marker.read_text(encoding="utf-8") == f"{'b' * 32}\n"


def test_sandbox_distinguishes_watches_started_in_same_second(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    script = {"gh": {"auth token": [{"rc": 1}]}}
    write_status(
        status_root,
        state="done",
        exit_code=0,
        line="first result",
        watch_id="1" * 32,
    )
    first_rc, first_out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )

    write_status(
        status_root,
        state="done",
        exit_code=0,
        line="second result",
        watch_id="2" * 32,
    )
    second_rc, second_out, _ = run_wait_for(
        tmp_path, script, "pr", "7", "-R", "owner/repo", wait_for=copied
    )

    assert first_rc == 0, first_out
    assert "first result" in first_out
    assert second_rc == 0, second_out
    assert "second result" in second_out


def test_sandbox_reads_each_status_generation_as_one_snapshot(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    status = write_status(
        status_root, state="done", exit_code=0, line="old success"
    )
    replacement = {
        "state": "done",
        "kind": "pr",
        "repo": "owner/repo",
        "number": 7,
        "watch_id": "b" * 32,
        "started_at": 1_700_000_001,
        "finished_at": 1_700_000_200,
        "exit": 1,
        "line": "new failure",
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    jq_log = tmp_path / "jq-status-calls"
    jq_wrapper = bin_dir / "jq"
    jq_wrapper.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

result = subprocess.run(
    [os.environ["REAL_JQ"], *sys.argv[1:]], capture_output=True, check=False
)
if Path(sys.argv[-1]) == Path(os.environ["STATUS_FILE"]):
    log = Path(os.environ["JQ_STATUS_LOG"])
    count = int(log.read_text() or "0") + 1 if log.exists() else 1
    log.write_text(str(count))
    if count == 2:
        target = Path(os.environ["STATUS_FILE"])
        temporary = target.with_suffix(".replacement")
        temporary.write_text(os.environ["REPLACEMENT"])
        os.replace(temporary, target)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    jq_wrapper.chmod(0o755)
    real_jq = shutil.which("jq")
    assert real_jq is not None
    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}},
        "pr", "7", "-R", "owner/repo",
        wait_for=copied,
        extra_env={
            "REAL_JQ": real_jq,
            "STATUS_FILE": str(status),
            "JQ_STATUS_LOG": str(jq_log),
            "REPLACEMENT": json.dumps(replacement),
        },
    )

    assert jq_log.read_text(encoding="utf-8") == "1"
    assert rc == 0, out
    assert "old success" in out
    assert "new failure" not in out


def test_sandbox_concurrent_callers_keep_atomic_marker(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    write_status(status_root, state="done", exit_code=2, line="one result")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    ready = tmp_path / "mv-ready"
    release = tmp_path / "mv-release"
    mv_wrapper = bin_dir / "mv"
    mv_wrapper.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import time

ready = Path(os.environ["MV_READY"])
if not ready.exists():
    ready.touch()
    release = Path(os.environ["MV_RELEASE"])
    while not release.exists():
        time.sleep(0.01)
os.execv(os.environ["REAL_MV"], [os.environ["REAL_MV"], *os.sys.argv[1:]])
""",
        encoding="utf-8",
    )
    mv_wrapper.chmod(0o755)
    real_mv = shutil.which("mv")
    assert real_mv is not None
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MV_READY": str(ready),
        "MV_RELEASE": str(release),
        "REAL_MV": real_mv,
    }
    argv = [
        "bash", str(copied), "pr", "7", "-R", "owner/repo", "--timeout", "30"
    ]
    first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
    second: subprocess.Popen[str] | None = None
    try:
        limit = time.monotonic() + 2
        while not ready.exists() and first.poll() is None and time.monotonic() < limit:
            time.sleep(0.01)
        assert ready.exists(), "first caller did not reach the atomic marker update"
        second = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        second_out, _ = second.communicate(timeout=5)
        release.touch()
        first_out, _ = first.communicate(timeout=5)
    finally:
        release.touch(exist_ok=True)
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
        if second is not None and second.poll() is None:
            second.terminate()
            second.wait(timeout=5)

    assert first.returncode == 2, first_out
    assert "one result" in first_out
    assert second.returncode == 2, second_out
    assert "one result" in second_out
    marker = home / ".cache/git-broker/watch-reported/owner/repo/pr-7"
    assert marker.read_text(encoding="utf-8") == f"{'a' * 32}\n"


def test_sandbox_stale_lock_directory_does_not_block_result(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    write_status(status_root, state="done", exit_code=0, line="still report")
    marker_dir = (
        tmp_path / "home" / ".cache" / "git-broker" / "watch-reported"
        / "owner" / "repo"
    )
    marker_dir.mkdir(parents=True)
    stale_lock = marker_dir / "pr-7.lock"
    stale_lock.mkdir()

    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}},
        "pr", "7", "-R", "owner/repo",
        wait_for=copied,
    )

    assert rc == 0, out
    assert "still report" in out
    assert (marker_dir / "pr-7").read_text(encoding="utf-8") == f"{'a' * 32}\n"
    assert stale_lock.is_dir()


def test_sandbox_refuses_symlinked_report_marker(tmp_path: Path) -> None:
    status_root = tmp_path / "status"
    copied = sandbox_layout(tmp_path, status_root)
    write_status(status_root, state="done", exit_code=0, line="must not report")
    marker_dir = (
        tmp_path / "home" / ".cache" / "git-broker" / "watch-reported"
        / "owner" / "repo"
    )
    marker_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep me\n", encoding="utf-8")
    (marker_dir / "pr-7").symlink_to(victim)

    rc, out, _ = run_wait_for(
        tmp_path,
        {"gh": {"auth token": [{"rc": 1}]}},
        "pr", "7", "-R", "owner/repo",
        wait_for=copied,
    )

    assert rc == 1, out
    assert "must not report" not in out
    assert victim.read_text(encoding="utf-8") == "keep me\n"

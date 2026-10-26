"""Job state machine through the real runner with stub providers (contract
§Jobs): launch, events, results, ids, quota retry, stall, timeout, cancel,
unparseable, rejected, dangerous-flag refusal, dead-runner repair."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from ai_router import adapters, jobs, quota
from ai_router.config import load_config

pytestmark = pytest.mark.usefixtures("stub_providers")


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def env(state_dir: Path, tmp_path: Path) -> dict[str, str]:
    e = dict(os.environ)
    e["HOME"] = str(tmp_path / "home")
    Path(e["HOME"]).mkdir()
    return e


def spec_for(provider: str, model: str, prompt: str, tmp_path: Path, env: dict[str, str], *,
             timeout_s: float = 30, stall_min: float = 0.5, sandbox: str = "read-only", schema: str | None = None,
             effort: str | None = "low", tier: str = "fast", model_pinned: bool = False) -> jobs.JobSpec:
    binary = adapters.resolve_binary(provider, env)
    assert binary
    return jobs.JobSpec(provider=provider, model=model, effort=effort, tier=tier, cwd=str(tmp_path),
                        sandbox=sandbox, prompt=prompt, timeout_s=timeout_s, stall_min=stall_min, schema=schema,
                        argv=[], host="claude", model_pinned=model_pinned)


def start(state: Path, spec: jobs.JobSpec, env: dict[str, str], now: float | None = None) -> tuple[str, Path]:
    job_id, d = jobs.create_job(state, spec, now=now or time.time())
    argv = adapters.build_argv(spec.provider, adapters.resolve_binary(spec.provider, env) or "", model=spec.model,
                               effort=spec.effort, sandbox=spec.sandbox, cwd=spec.cwd, schema=spec.schema,
                               bare=spec.bare, job_dir=d)
    jobs.update_meta(d, argv=argv)
    jobs.launch(d, env=env, launcher="setsid")
    return job_id, d


def finish(state: Path, job_id: str, timeout_s: float = 30) -> tuple[str, str]:
    return jobs.wait_for(state, job_id, timeout_s=timeout_s, poll_s=0.1)


def test_create_job_layout(state_dir: Path, tmp_path: Path, env):
    job_id, d = jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "hi", tmp_path, env), now=1789400000)
    assert d.name == job_id and job_id.startswith(time.strftime("%Y%m%d-%H%M%S", time.localtime(1789400000)))
    assert len(job_id.split("-")[-1]) == 6
    assert (d / "prompt.md").read_text() == "hi"
    assert jobs.raw_status(d) == "queued"
    meta = jobs.read_meta(d)
    assert set(jobs.META_KEYS) <= set(meta)
    assert meta["retry_of"] is None and meta["forced"] is False and meta["stall_min"] == 0.5
    assert meta["model_pinned"] is False
    assert (d.stat().st_mode & 0o777) == 0o700
    with pytest.raises(jobs.JobNotFound):
        jobs.job_dir(state_dir, "nope")
    with pytest.raises(jobs.JobNotFound):
        jobs.job_dir(state_dir, "../etc")


def test_systemd_job_environment_preserves_runtime_directory():
    values = jobs._router_env({"PATH": "/bin", "HOME": "/home/test", "XDG_RUNTIME_DIR": "/run/user/999"})
    assert "--setenv=XDG_RUNTIME_DIR=/run/user/999" in values


def test_codex_job_runs_to_done(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "Say hi\nMODE:ok", tmp_path, env), env)
    fid, status = finish(state_dir, job_id)
    assert fid == job_id and status == "done"
    meta = jobs.read_meta(d)
    assert meta["launcher"] == "setsid" and meta["runner_pid"] and meta["child_pid"]
    assert meta["thread_id"] == "thread-fake-0001" and meta["exit_code"] == 0 and meta["error_class"] is None
    assert meta["started_at"] and meta["finished_at"]
    assert (d / "exit_code").read_text().strip() == "0"
    text = (d / "result.txt").read_text()
    assert "OK Say hi" in text and "\x1b" not in text  # sanitised, from -o file
    assert not (d / "result.json").exists()
    events = adapters.read_events(d / "events.jsonl")
    assert events[0]["type"] == "thread.started" and events[-1]["type"] == "turn.completed"
    assert "--" in meta["argv"] and meta["argv"][-1] == adapters.PROMPT_PLACEHOLDER
    res = jobs.result_obj(d)
    assert res["ok"] and res["thread_id"] == "thread-fake-0001" and res["usage"]["input_tokens"] == 11
    assert res["duration_s"] is not None and res["error_class"] is None
    assert jobs.peek(d) == "OK Say hi"
    lines = jobs.log_lines(d, 50)
    assert any(l.startswith("thread.started") for l in lines) and any("agent_message" in l for l in lines)


def test_claude_job_json_result_and_session(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("claude", "claude-haiku-4-5-20251001", "MODE:json", tmp_path, env), env)
    _, status = finish(state_dir, job_id)
    assert status == "done"
    meta = jobs.read_meta(d)
    assert meta["session_id"] == "session-fake-0001" and meta["thread_id"] is None
    assert json.loads((d / "result.json").read_text()) == {"answer": 42, "model": "claude-haiku-4-5-20251001"}
    res = jobs.result_obj(d)
    assert res["result_json"]["answer"] == 42 and res["session_id"] == "session-fake-0001"
    assert res["usage"]["total_cost_usd"] == 0.0007


def test_quota_failure_writes_rejected_and_retries_once(state_dir: Path, tmp_path: Path, env, cfg):
    # luna fails on quota; the retry must land on the other provider's fast model
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "MODE:quota@gpt-5.6-luna", tmp_path, env), env)
    fid, status = finish(state_dir, job_id)
    old = jobs.read_meta(d)
    assert jobs.raw_status(d) == "failed:quota" and old["error_class"] == "quota"
    rej = json.loads((state_dir / "quota" / "codex.rejected.json").read_text())
    assert rej["provider"] == "codex" and rej["source"] == f"job:{job_id}" and "usage limit" in rej["reason"]
    # The captured provider reset is historical once this fixture is older
    # than its original week. ai-router must not park a provider until a time
    # already in the past; before that instant it preserves the exact reset.
    expected_until = 1789805383 if time.time() < 1789805383 else None
    assert rej["until"] == expected_until
    assert old["retried_as"] and old["retried_as"] != job_id
    assert fid == old["retried_as"] and status == "done"
    new = jobs.read_meta(jobs.job_dir(state_dir, fid))
    assert new["retry_of"] == job_id and new["provider"] == "claude" and new["model"] == "claude-haiku-4-5-20251001"
    assert new["effort"] == "low" and new["forced"] is False
    assert jobs.status_obj(state_dir, job_id)["retried_as"] == fid


def test_provider_constrained_quota_failure_never_retries_on_claude(
    state_dir: Path, tmp_path: Path, env
):
    spec = spec_for("codex", "gpt-5.6-luna", "MODE:quota", tmp_path, env)
    spec.provider_constraint = "codex"
    job_id, d = start(state_dir, spec, env)

    fid, status = finish(state_dir, job_id)

    meta = jobs.read_meta(d)
    assert fid == job_id and status == "failed:quota"
    assert meta["provider_constraint"] == "codex"
    assert meta["retried_as"] is None
    assert "no eligible fast model for provider codex" in meta["retry_note"]


def test_quota_twice_stops_after_one_retry(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "MODE:quota", tmp_path, env), env)
    fid, status = finish(state_dir, job_id)
    assert status == "failed:quota" and fid != job_id
    retry = jobs.read_meta(jobs.job_dir(state_dir, fid))
    assert retry["retry_of"] == job_id and retry["retried_as"] is None
    assert retry["retry_note"].startswith("already a retry")
    assert (state_dir / "quota" / "claude.rejected.json").exists()
    assert (state_dir / "quota" / "codex.rejected.json").exists()


def test_pinned_model_quota_failure_never_cross_retries(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(
        state_dir,
        spec_for(
            "codex",
            "gpt-5.6-luna",
            "MODE:quota",
            tmp_path,
            env,
            model_pinned=True,
        ),
        env,
    )
    fid, status = finish(state_dir, job_id)
    meta = jobs.read_meta(d)
    assert fid == job_id and status == "failed:quota"
    assert meta["retried_as"] is None
    assert meta["retry_note"] == "no retry: model pinned (--model); retry the same model later"
    assert (state_dir / "quota" / "codex.rejected.json").exists()


def test_rate_limit_never_rejects_provider_or_cross_retries(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(
        state_dir,
        spec_for("codex", "gpt-5.6-luna", "MODE:rate_limit", tmp_path, env),
        env,
    )
    fid, status = finish(state_dir, job_id)
    meta = jobs.read_meta(d)
    assert fid == job_id and status == "failed:rate_limit"
    assert meta["error_class"] == "rate_limit" and meta["retried_as"] is None
    assert not (state_dir / "quota" / "codex.rejected.json").exists()
    assert len(jobs.list_jobs(state_dir, all_jobs=True)) == 1


def test_stall_watchdog(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "MODE:stall", tmp_path, env, stall_min=0.02), env)
    _, status = finish(state_dir, job_id, timeout_s=30)
    assert status == "stalled"
    meta = jobs.read_meta(d)
    assert meta["error_class"] == "stalled" and not jobs.pid_alive(meta["child_pid"])
    assert not (state_dir / "quota" / "codex.rejected.json").exists()


def test_wall_clock_timeout(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "MODE:slow", tmp_path, env, timeout_s=1.5), env)
    _, status = finish(state_dir, job_id)
    assert status == "timeout"
    assert not jobs.pid_alive(jobs.read_meta(d)["child_pid"])


def test_cancel_running_job(state_dir: Path, tmp_path: Path, env):
    job_id, d = start(state_dir, spec_for("codex", "gpt-5.6-luna", "MODE:stall", tmp_path, env, stall_min=5), env)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not jobs.read_meta(d).get("child_pid"):
        time.sleep(0.05)
    r = jobs.cancel(state_dir, job_id)
    assert r["cancelled"] and jobs.raw_status(d) == "cancelled"
    meta = jobs.read_meta(d)
    assert not jobs.pid_alive(meta["child_pid"]) and not jobs.pid_alive(meta["runner_pid"])
    assert jobs.cancel(state_dir, job_id)["cancelled"] is False


def test_unparseable_rejected_overloaded_error(state_dir: Path, tmp_path: Path, env):
    outcomes = {}
    for mode in ("unparseable", "rejected", "overloaded", "crash"):
        job_id, _ = start(state_dir, spec_for("codex", "gpt-5.6-luna", f"MODE:{mode}", tmp_path, env), env)
        outcomes[mode] = finish(state_dir, job_id)[1]
    assert outcomes == {"unparseable": "failed:unparseable", "rejected": "failed:rejected",
                        "overloaded": "failed:overloaded", "crash": "failed:error"}
    assert not (state_dir / "quota" / "codex.rejected.json").exists()


def test_runner_refuses_tampered_dangerous_argv(state_dir: Path, tmp_path: Path, env):
    spec = spec_for("codex", "gpt-5.6-luna", "hi", tmp_path, env)
    job_id, d = jobs.create_job(state_dir, spec, now=time.time())
    binary = adapters.resolve_binary("codex", env)
    # arftl-allow: permission-widened — tamper test: the runner must refuse this argv and never launch it
    jobs.update_meta(d, argv=[binary, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--",
                              adapters.PROMPT_PLACEHOLDER])
    jobs.launch(d, env=env, launcher="setsid")
    _, status = finish(state_dir, job_id)
    assert status == "failed:error"
    assert "refusing sandbox-escaping flag" in (d / "stderr.log").read_text()


def test_dead_runner_is_repaired(state_dir: Path, tmp_path: Path, env):
    job_id, d = jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "hi", tmp_path, env), now=time.time())
    jobs.write_status(d, "running")
    jobs.update_meta(d, runner_pid=2**22 - 1)  # not a live pid
    assert jobs.status_of(d) == "failed:error"
    assert jobs.read_meta(d)["error_class"] == "error"
    # queued with no pid past the grace window → same repair
    job2, d2 = jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "hi", tmp_path, env), now=time.time() - 100)
    assert jobs.status_of(d2) == "failed:error"
    # queued but fresh → still queued
    job3, d3 = jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "hi", tmp_path, env), now=time.time())
    assert jobs.status_of(d3) == "queued"


def test_list_and_status_ordering(state_dir: Path, tmp_path: Path, env):
    ids = []
    for i in range(3):
        ids.append(jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "x", tmp_path, env),
                                   now=1789400000 + i)[0])
    rows = jobs.list_jobs(state_dir, now=1789400010)
    assert [r["job_id"] for r in rows] == list(reversed(ids))
    assert rows[0]["age_s"] == 8 and rows[0]["status"] == "queued"


def test_wait_times_out(state_dir: Path, tmp_path: Path, env):
    job_id, d = jobs.create_job(state_dir, spec_for("codex", "gpt-5.6-luna", "x", tmp_path, env), now=time.time())
    with pytest.raises(TimeoutError):
        jobs.wait_for(state_dir, job_id, timeout_s=0.3, poll_s=0.05)


def test_systemd_launcher_when_available(state_dir: Path, tmp_path: Path, env):
    import shutil
    import subprocess
    if not shutil.which("systemd-run") or subprocess.run(["systemctl", "--user", "is-system-running"],
                                                         capture_output=True).returncode not in (0, 1):
        pytest.skip("no user systemd")
    spec = spec_for("codex", "gpt-5.6-luna", "MODE:ok via systemd", tmp_path, env)
    job_id, d = jobs.create_job(state_dir, spec, now=time.time())
    argv = adapters.build_argv("codex", adapters.resolve_binary("codex", env) or "", model=spec.model,
                               effort=spec.effort, sandbox=spec.sandbox, cwd=spec.cwd, schema=None, bare=True,
                               job_dir=d)
    jobs.update_meta(d, argv=argv)
    info = jobs.launch(d, env=env, launcher="auto")
    if info["launcher"] != "systemd-run":
        pytest.skip("systemd-run --user refused (degraded manager?)")
    assert info["unit"] == f"ai-router-{job_id}"
    _, status = finish(state_dir, job_id)
    assert status == "done"
    meta = jobs.read_meta(d)
    assert meta["launcher"] == "systemd-run" and meta["runner_pid"]

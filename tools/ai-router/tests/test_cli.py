"""CLI surface (contract §CLI): output shapes, exit codes, sad paths.
Runs `cli.main` in-process with stub providers and a private HOME."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ai_router import cli, jobs

pytestmark = pytest.mark.usefixtures("stub_providers")
FIXTURE_NOW = 1_789_400_000


@pytest.fixture
def env(state_dir: Path, tmp_path: Path) -> dict[str, str]:
    e = dict(os.environ)
    e["HOME"] = str(tmp_path / "home")   # no ~/.claude/.credentials.json → OAuth never attempted
    Path(e["HOME"]).mkdir()
    e.pop("CLAUDECODE", None)
    e.pop("CLAUDE_CODE_SESSION_ID", None)
    return e


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.md"
    p.write_text("reply with exactly OK\nMODE:ok\n")
    return p


def run(capsys, argv: list[str], env: dict[str, str], **kw) -> tuple[int, dict | None, str, str]:
    rc = cli.main(
        argv,
        env=env,
        now=kw.pop("now", FIXTURE_NOW),
        launcher=kw.pop("launcher", "setsid"),
        **kw,
    )
    cap = capsys.readouterr()
    obj = None
    if "--json" in argv and cap.out.strip():
        obj = json.loads(cap.out)
    return rc, obj, cap.out, cap.err


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.name", "test"],
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "initial"],
        ["git", "-C", str(repo), "worktree", "add", "-qb", "probe", str(worktree)],
    ):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    return worktree, repo / ".git"


def test_version_and_usage(capsys, env):
    rc, obj, *_ = run(capsys, ["version", "--json"], env)
    assert rc == 0 and obj["version"]
    assert run(capsys, ["bogus"], env)[0] == 2
    assert run(capsys, ["delegate", "--prompt-file", "x"], env)[0] == 2          # neither --tier nor --model
    assert run(capsys, ["delegate", "--tier", "fast", "--model", "sol", "--prompt-file", "x"], env)[0] == 2


def test_quota_json_shape(capsys, env):
    rc, obj, *_ = run(capsys, ["quota", "--json"], env)
    assert rc == 0 and obj["ok"] and set(obj["providers"]) == {"codex", "claude"}
    codex = obj["providers"]["codex"]
    assert codex["status"] == "ok" and codex["source"] == "app-server" and codex["buckets"][0]["used_pct"] == 98
    assert obj["providers"]["claude"]["status"] == "unknown"
    rc, obj, *_ = run(capsys, ["quota", "--provider", "claude", "--json"], env)
    assert list(obj["providers"]) == ["claude"]
    rc, _, out, _ = run(capsys, ["quota"], env)
    assert rc == 0 and "codex: ok" in out and "claude: unknown" in out


def test_pick_json_and_exit3(capsys, env):
    rc, obj, *_ = run(capsys, ["pick", "--tier", "fast", "--json"], env)
    assert rc == 0 and obj["model"] == "claude-haiku-4-5-20251001" and obj["native"] is False
    assert obj["candidates"][0]["model"] == "gpt-5.6-luna" and obj["candidates"][0]["eligible"] is False
    assert "surplus" in obj and all("surplus" in c for c in obj["candidates"])
    rc, _, out, _ = run(capsys, ["pick", "--tier", "fast"], env)
    assert rc == 0 and "pace=" in out
    rc, obj, *_ = run(capsys, ["pick", "--tier", "fast", "--host", "claude", "--json"], env)
    assert obj["native"] is True
    rc, obj, *_ = run(capsys, ["pick", "--tier", "fast", "--provider", "codex", "--json"], env)
    assert rc == 3 and obj["ok"] is False and obj["error_class"] == "quota" and obj["candidates"]
    rc, _, out, err = run(capsys, ["pick", "--tier", "fast", "--provider", "codex"], env)
    assert rc == 3 and out == "" and "no eligible" in err
    e2 = dict(env, CLAUDECODE="1")
    assert run(capsys, ["pick", "--tier", "fast", "--json"], e2)[1]["host"] == "claude"


def test_delegate_wait_and_inspect(capsys, env, prompt, state_dir, tmp_path):
    rc, res, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                               "--wait", "--json", "--name", "smoke"], env)
    assert rc == 0 and res["ok"] and res["status"] == "done"
    assert res["provider"] == "claude" and res["model"] == "claude-haiku-4-5-20251001"
    assert res["result_text"].startswith("OK reply with exactly OK") and res["session_id"] == "session-fake-0001"
    jid = res["job_id"]
    meta = jobs.read_meta(jobs.job_dir(state_dir, jid))
    assert meta["name"] == "smoke" and meta["bare"] is True and meta["launcher"] == "setsid"
    assert "--setting-sources" in meta["argv"] and meta["argv"][-1] == "<prompt.md>"

    rc, obj, *_ = run(capsys, ["status", jid, "--json"], env)
    assert rc == 0 and obj["status"] == "done" and obj["name"] == "smoke"
    rc, obj, *_ = run(capsys, ["status", "--json"], env)
    assert obj["jobs"][0]["job_id"] == jid
    rc, obj, *_ = run(capsys, ["peek", jid, "--json"], env)
    assert obj["text"].startswith("OK")
    rc, obj, *_ = run(capsys, ["log", jid, "--tail", "2", "--json"], env)
    assert len(obj["lines"]) == 2 and obj["lines"][-1].startswith("result success")
    rc, obj, *_ = run(capsys, ["result", jid, "--json"], env)
    assert rc == 0 and obj["result_json"] is None and obj["usage"]["total_cost_usd"] == 0.0007
    rc, obj, *_ = run(capsys, ["list", "--json"], env)
    assert obj["jobs"][0]["job_id"] == jid
    rc, _, out, _ = run(capsys, ["list"], env)
    assert jid in out and "smoke" in out


def test_delegate_detached_then_wait(capsys, env, prompt, tmp_path, state_dir):
    rc, started, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                                   "--json"], env)
    assert rc == 0 and started["ok"] and started["launcher"] == "setsid" and started["runner_pid"]
    assert started["unit"] is None and Path(started["events"]).parent == Path(started["job_dir"])
    jid = started["job_id"]
    rc, obj, *_ = run(capsys, ["wait", jid, "--timeout-s", "30", "--json"], env)
    assert rc == 0 and obj["status"] == "done"
    rc, _, out, _ = run(capsys, ["result", jid], env)
    assert rc == 0 and out.startswith(f"{jid} done claude/")


def test_delegate_model_quota_gate_and_force(capsys, env, prompt, tmp_path, state_dir):
    # codex is at 98 % in the stub snapshot → luna ineligible without --force
    rc, obj, *_ = run(capsys, ["delegate", "--model", "luna", "--prompt-file", str(prompt), "--cwd", str(tmp_path),
                               "--json"], env)
    assert rc == 3 and obj["error_class"] == "quota" and "--force" in obj["message"]
    rc, res, *_ = run(capsys, ["delegate", "--model", "luna", "--effort", "low", "--force", "--prompt-file",
                               str(prompt), "--cwd", str(tmp_path), "--wait", "--json"], env)
    assert rc == 0 and res["provider"] == "codex" and res["model"] == "gpt-5.6-luna"
    meta = jobs.read_meta(jobs.job_dir(state_dir, res["job_id"]))
    assert meta["forced"] is True and meta["effort"] == "low" and "model_reasoning_effort=low" in meta["argv"]
    assert res["thread_id"] == "thread-fake-0001"


def test_workspace_write_codex_records_managed_profile_truthfully(capsys, env, prompt, tmp_path, state_dir):
    worktree, _common_dir = linked_worktree(tmp_path)
    rc, started, *_ = run(capsys, ["delegate", "--model", "luna", "--force", "--prompt-file", str(prompt),
                                   "--cwd", str(worktree), "--sandbox", "workspace-write", "--json"], env)
    assert rc == 0 and started["provider"] == "codex"
    meta = jobs.read_meta(jobs.job_dir(state_dir, started["job_id"]))
    assert meta["effective_sandbox"] == {
        "mode": "workspace-write",
        "managed_profile": "repos_dev",
        "writable_roots": [
            "/home/jonathan/Repos",
            "/home/jonathan/worktrees",
            "/home/jonathan/.local/state/ai-router",
        ],
        "network_access": False,
        "cli_overrides_effective": False,
    }
    argv = meta["argv"]
    assert "--add-dir" not in argv
    assert "sandbox_workspace_write.network_access=true" not in argv


def test_dev_gates_rejects_managed_profile_instead_of_claiming_inert_overrides(capsys, env, prompt, tmp_path):
    rc, obj, _, err = run(capsys, ["delegate", "--model", "luna", "--force", "--prompt-file", str(prompt),
                                   "--sandbox", "workspace-write", "--dev-gates", "--json"], env)
    assert rc == 2 and obj["error_class"] == "usage"
    assert "managed Codex permission profile" in obj["message"]
    assert "klaffat-app-runner check mode" in obj["message"]
    assert err == ""


def test_delegate_setup_and_usage_errors(capsys, env, prompt, tmp_path):
    bad = dict(env, AI_ROUTER_CLAUDE_BIN=str(tmp_path / "missing-claude"))
    rc, obj, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(prompt), "--json"], bad)
    assert rc == 2 and obj["error_class"] == "setup" and "binary" in obj["message"]
    rc, obj, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(tmp_path / "nope"), "--json"], env)
    assert rc == 2 and obj["error_class"] == "usage"
    rc, obj, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(prompt), "--cwd",
                               str(tmp_path / "nodir"), "--json"], env)
    assert rc == 2
    rc, obj, *_ = run(capsys, ["delegate", "--model", "haiku", "--effort", "ultra", "--prompt-file", str(prompt),
                               "--json"], env)
    assert rc == 2 and "effort" in obj["message"]
    rc, obj, *_ = run(capsys, ["delegate", "--model", "no-such", "--prompt-file", str(prompt), "--json"], env)
    assert rc == 2
    assert run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(prompt), "--sandbox", "yolo"], env)[0] == 2


def test_tier_picked_delegate_quota_retry_via_cli(capsys, env, tmp_path, state_dir):
    p = tmp_path / "p.md"
    p.write_text("MODE:quota\n")
    rc, res, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(p), "--cwd",
                               str(tmp_path), "--wait", "--json"], env)
    assert rc == 3 and res["status"] == "failed:quota" and res["error_class"] == "quota"
    first = next(
        row
        for row in jobs.list_jobs(state_dir, all_jobs=True)
        if row["retried_as"] == res["job_id"]
    )
    assert first["retried_as"] == res["job_id"] and first["status"] == "failed:quota"
    rc, obj, *_ = run(capsys, ["status", first["job_id"], "--json"], env)
    assert obj["retried_as"] == res["job_id"]


def test_explicit_model_quota_failure_does_not_cross_retry(capsys, env, tmp_path, state_dir):
    p = tmp_path / "p.md"
    p.write_text("MODE:quota\n")
    rc, res, *_ = run(
        capsys,
        [
            "delegate",
            "--model",
            "luna",
            "--force",
            "--prompt-file",
            str(p),
            "--cwd",
            str(tmp_path),
            "--wait",
            "--json",
        ],
        env,
    )
    assert rc == 3 and res["status"] == "failed:quota"
    meta = jobs.read_meta(jobs.job_dir(state_dir, res["job_id"]))
    assert meta["model_pinned"] is True and meta["retried_as"] is None
    assert meta["retry_note"] == "no retry: model pinned (--model); retry the same model later"


def test_result_not_finished_and_not_found(capsys, env, tmp_path, state_dir):
    p = tmp_path / "p.md"
    p.write_text("MODE:stall\n")
    rc, started, *_ = run(capsys, ["delegate", "--tier", "fast", "--prompt-file", str(p), "--cwd", str(tmp_path),
                                   "--json"], env)
    jid = started["job_id"]
    rc, obj, *_ = run(capsys, ["result", jid, "--json"], env)
    assert rc == 7 and obj["error_class"] == "not_finished"
    rc, obj, *_ = run(capsys, ["wait", jid, "--timeout-s", "0.3", "--json"], env)
    assert rc == 5 and obj["error_class"] == "timeout"
    rc, obj, *_ = run(capsys, ["cancel", jid, "--json"], env)
    assert rc == 0 and obj["status"] == "cancelled"
    rc, obj, *_ = run(capsys, ["result", jid, "--json"], env)
    assert rc == 1 and obj["status"] == "cancelled"
    for c in (["result", "nope"], ["status", "nope"], ["peek", "nope"], ["log", "nope"], ["cancel", "nope"]):
        rc, obj, *_ = run(capsys, c + ["--json"], env)
        assert rc == 7 and obj["error_class"] == "not_found"


def test_exit_codes_per_failure_class(capsys, env, tmp_path):
    p = tmp_path / "p.md"
    expected = {
        "unparseable": 6,
        "rejected": 4,
        "rate_limit": 5,
        "overloaded": 5,
        "crash": 1,
    }
    for mode, code in expected.items():
        p.write_text(f"MODE:{mode}\n")
        rc, res, *_ = run(capsys, ["delegate", "--model", "haiku", "--prompt-file", str(p), "--cwd", str(tmp_path),
                                   "--wait", "--json"], env) if mode in ("overloaded", "crash") else \
            run(capsys, ["delegate", "--model", "luna", "--force", "--prompt-file", str(p), "--cwd", str(tmp_path),
                         "--wait", "--json"], env)
        assert rc == code, (mode, res)


def test_send_resumes_finished_job(capsys, env, prompt, tmp_path, state_dir):
    rc, res, *_ = run(capsys, ["delegate", "--model", "luna", "--force", "--prompt-file", str(prompt), "--cwd",
                               str(tmp_path), "--wait", "--json"], env)
    jid = res["job_id"]
    p2 = tmp_path / "follow.md"
    p2.write_text("and now?\n")
    rc, res2, *_ = run(capsys, ["send", jid, "--prompt-file", str(p2), "--wait", "--json"], env)
    assert rc == 0 and res2["result_text"].startswith("RESUMED: OK and now?")
    meta = jobs.read_meta(jobs.job_dir(state_dir, res2["job_id"]))
    assert meta["parent_job"] == jid and meta["argv"][2:4] == ["resume", "thread-fake-0001"]
    # claude side
    rc, res3, *_ = run(capsys, ["delegate", "--model", "haiku", "--prompt-file", str(prompt), "--cwd",
                                str(tmp_path), "--wait", "--json"], env)
    rc, res4, *_ = run(capsys, ["send", res3["job_id"], "--prompt-file", str(p2), "--wait", "--json"], env)
    assert rc == 0 and res4["session_id"] == "session-fake-0001" and "RESUMED" in res4["result_text"]
    meta4 = jobs.read_meta(jobs.job_dir(state_dir, res4["job_id"]))
    assert meta4["argv"][meta4["argv"].index("--resume") + 1] == "session-fake-0001"


def test_schema_and_workspace_write(capsys, env, tmp_path, state_dir):
    schema = tmp_path / "s.json"
    schema.write_text('{"type":"object","properties":{"answer":{"type":"integer"}},"required":["answer"],'
                      '"additionalProperties":false}')
    p = tmp_path / "p.md"
    p.write_text("MODE:json\n")
    rc, res, *_ = run(capsys, ["delegate", "--model", "haiku", "--prompt-file", str(p), "--cwd", str(tmp_path),
                               "--schema", str(schema), "--sandbox", "workspace-write", "--no-bare", "--wait",
                               "--json"], env)
    assert rc == 0 and res["result_json"] == {"answer": 42, "model": "claude-haiku-4-5-20251001"}
    meta = jobs.read_meta(jobs.job_dir(state_dir, res["job_id"]))
    assert meta["schema"] == str(schema) and meta["bare"] is False and meta["sandbox"] == "workspace-write"
    argv = meta["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits" and "--json-schema" in argv
    assert "--setting-sources" not in argv


def test_output_is_sanitised(capsys, env, tmp_path):
    p = tmp_path / "p.md"
    p.write_text("\x1b[31mred\x1b[0m\nMODE:ok\n")
    rc, res, *_ = run(capsys, ["delegate", "--model", "luna", "--force", "--prompt-file", str(p), "--cwd",
                               str(tmp_path), "--wait", "--json"], env)
    assert "\x1b" not in res["result_text"]
    rc, _, out, _ = run(capsys, ["peek", res["job_id"]], env)
    assert "\x1b" not in out and "OK" in out
    rc, _, out, _ = run(capsys, ["log", res["job_id"]], env)
    assert "\x1b" not in out

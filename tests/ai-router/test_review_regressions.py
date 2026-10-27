"""Regressions from the 2026-09-14 close-out review, each reproduced before
the fix (advice-refine-test-loop, findings F2–F4, F6).

  F2  Claude argv had no `--` before the positional prompt: a prompt starting
      with `-` (markdown bullet) was reported as "unknown option" by the CLI.
  F3  classify() consulted the quota regex before the exit code: a successful
      codex run whose recoverable `error` event mentioned 429 became
      failed:quota, parked the provider until its weekly reset, ran twice.
  F4  The runner wrote the terminal `failed:quota` status BEFORE linking the
      retry, so `delegate --wait` could return the failure while the retry
      was still being decided (seen as an intermittent test failure).
  F6  The skill documented `delegate --provider codex`; the flag did not exist.

Run: nix shell nixpkgs#python3Packages.pytest -c pytest -q tests/ai-router/test_review_regressions.py
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_router import adapters, jobs, runner
from ai_router.adapters import PROMPT_PLACEHOLDER

pytestmark = pytest.mark.usefixtures("stub_providers")


# ── F2: option parsing ends before the prompt ──────────────────────────────


def test_claude_argv_ends_options_before_prompt(tmp_path: Path):
    argv = adapters.build_argv("claude", "/bin/claude", model="claude-haiku-4-5-20251001", effort=None,
                               sandbox="read-only", cwd=str(tmp_path), schema=None, bare=True, job_dir=tmp_path)
    assert argv[-2:] == ["--", PROMPT_PLACEHOLDER]
    resume = adapters.build_resume_argv("claude", "/bin/claude", thread_id="sess-1", model="claude-haiku-4-5-20251001",
                                        effort=None, sandbox="read-only", cwd=str(tmp_path), bare=True)
    assert resume[-2:] == ["--", PROMPT_PLACEHOLDER]
    bullet = adapters.materialize(argv, "- a\n- b")
    assert bullet[-1] == "- a\n- b" and bullet[-2] == "--"


# ── F3: a finished run is done, whatever a transient event said ────────────


def _codex_success_with_transient_429() -> list[dict]:
    return [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "error", "message": "Reconnecting... 1/5 (HTTP 429 rate limit)"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]


def test_classify_success_beats_transient_429_event():
    events = _codex_success_with_transient_429()
    assert adapters.classify("codex", 0, events, "") == "done"
    # the same stream with a failing exit is still a quota failure
    assert adapters.classify("codex", 1, events, "") == "quota"


def test_classify_claude_error_result_is_not_done_even_on_exit_zero():
    events = [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"},
    ]
    assert adapters.classify("claude", 0, events, "") == "error"


# ── F4: retry linked before the terminal status ────────────────────────────


def test_quota_retry_is_linked_before_terminal_status(state_dir: Path, tmp_path: Path, monkeypatch):
    import os

    env = dict(os.environ)
    binary = adapters.resolve_binary("codex", env)
    assert binary
    spec = jobs.JobSpec(provider="codex", model="gpt-5.6-luna", effort="low", tier="fast", cwd=str(tmp_path),
                        sandbox="read-only", prompt="MODE:quota\n", timeout_s=30, stall_min=0.5, schema=None,
                        argv=[], host="claude")
    job_id, d = jobs.create_job(state_dir, spec, now=time.time())
    jobs.update_meta(d, argv=adapters.build_argv("codex", binary, model=spec.model, effort=spec.effort,
                                                 sandbox=spec.sandbox, cwd=spec.cwd, schema=None, bare=True,
                                                 job_dir=d))
    seen: dict[str, str] = {}

    def fake_retry(state, dd, meta, diag, now):
        seen["status_when_retry_decided"] = jobs.raw_status(dd)
        jobs.update_meta(dd, retried_as="linked-first")

    monkeypatch.setattr(runner, "_retry_after_quota", fake_retry)
    rc = runner.main([str(d)])
    assert rc == 1
    assert seen["status_when_retry_decided"] == "running", "retry must be decided while the job is still active"
    assert jobs.raw_status(d) == "failed:quota"
    assert jobs.read_meta(d)["retried_as"] == "linked-first"


# ── F6: delegate --provider ────────────────────────────────────────────────


def test_delegate_provider_constrains_the_pick(capsys, tmp_path: Path, state_dir: Path):
    import os

    from test_cli import run  # same harness as the CLI suite (tests dir is on sys.path under pytest)

    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    p = tmp_path / "p.md"
    p.write_text("reply OK\nMODE:ok\n")
    rc, res, *_ = run(capsys, ["delegate", "--tier", "fast", "--provider", "claude", "--prompt-file", str(p),
                               "--cwd", str(tmp_path), "--json"], env)
    assert rc == 0 and res["ok"] and res["provider"] == "claude", res
    rc, obj, *_ = run(capsys, ["delegate", "--model", "luna", "--provider", "claude", "--prompt-file", str(p),
                               "--cwd", str(tmp_path), "--json"], env)
    assert rc == 2 and obj["error_class"] == "usage", obj


# ── list ordering: same-second jobs (a retry and its parent) ───────────────


def test_list_orders_same_second_jobs_by_creation_not_random_suffix(state_dir: Path, tmp_path: Path):
    """Job ids have one-second resolution plus random hex; a retry created in
    the same second as its parent used to sort randomly against it (the root
    of the intermittent test_delegate_quota_retry_via_cli failure). Twenty
    same-second pairs make a lucky pass astronomically unlikely."""
    base = 1789400000.0
    spec = jobs.JobSpec(provider="codex", model="gpt-5.6-luna", effort="low", tier="fast", cwd=str(tmp_path),
                        sandbox="read-only", prompt="x", timeout_s=30, stall_min=0.5, schema=None, argv=[],
                        host="claude")
    for i in range(20):
        second = base + i * 10
        older, _ = jobs.create_job(state_dir, spec, now=second + 0.2)
        newer, _ = jobs.create_job(state_dir, spec, now=second + 0.7)
        listed = [j["job_id"] for j in jobs.list_jobs(state_dir, all_jobs=True)]
        assert listed.index(newer) < listed.index(older), (older, newer, listed[:4])
    assert listed[0] == newer and listed[-1].startswith(time.strftime("%Y%m%d-%H%M%S", time.localtime(base)))

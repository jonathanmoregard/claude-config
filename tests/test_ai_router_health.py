"""SessionStart hook `hooks/ai-router-health.py` — the channel through which
ai-router's background failures reach a Claude session.

WHAT THIS GUARDS. The ranking-evidence updater runs from a systemd user timer
(nixos-config `home/ai-router.nix`). Its `OnFailure` notifier reaches the
desktop and the journal — neither of which a session reads. The 2026-08-31
constraint ("failure notifications must reach CLAUDE, not just the desktop")
is met the same way `hooks/claude-pull-health.py` meets it: the producer writes
a durable outcome record (`ranking.last-run.json`, written by the unit wrapper
before and after every run) and this hook states the fact at SessionStart.

Two opposite silent failure modes, both tested:

  * speaking when healthy — burns the attention budget. A healthy state prints
    nothing.
  * staying quiet when it could not tell — no record, an unparseable record, a
    record older than the timer's cadence allows, all speak.

It also surfaces a provider that ai-router has marked *rejected* (quota error
or rate-limit stop): a session that sees "picks avoid Codex until 19:00" stops
wondering why the router keeps answering with Claude.

Every case was run RED first against a hook that did not exist.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, "hooks", "ai-router-health.py")


def run_hook(state_dir: str, now: float | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["AI_ROUTER_STATE"] = state_dir
    if now is not None:
        env["AI_ROUTER_HEALTH_NOW"] = str(now)
    return subprocess.run(
        [sys.executable, "-S", "-E", HOOK],
        input='{"hook_event_name": "SessionStart", "session_id": "t"}',
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def write(path: str, payload: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)


NOW = 1_800_000_000.0


def last_run(exit_code: int | None = 0, age_s: float = 3600, skipped: bool = False, finished: bool = True) -> dict:
    started = NOW - age_s
    return {
        "unit": "ai-router-ranking",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(started)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(started + 5)) if finished else None,
        "exit_code": exit_code,
        "skipped": skipped,
    }


def healthy_state(tmp_path) -> str:
    state = str(tmp_path / "state")
    write(os.path.join(state, "ranking.last-run.json"), last_run())
    write(
        os.path.join(state, "ranking.auto.json"),
        {"generated_at": NOW - 3600, "sources": [{"name": "lmarena", "ok": True}, {"name": "epoch", "ok": True}], "disagreements": []},
    )
    return state


# ── silence is the budget ─────────────────────────────────────────────────


def test_no_state_dir_is_silent(tmp_path):
    proc = run_hook(str(tmp_path / "absent"), NOW)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_healthy_state_is_silent(tmp_path):
    proc = run_hook(healthy_state(tmp_path), NOW)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_expired_rejection_is_silent(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "quota", "codex.rejected.json"), {"provider": "codex", "until": NOW - 10, "reason": "old", "observed_at": NOW - 4000})
    proc = run_hook(state, NOW)
    assert proc.stdout == ""


# ── could-not-tell speaks ─────────────────────────────────────────────────


def test_missing_last_run_speaks_when_state_exists(tmp_path):
    state = str(tmp_path / "state")
    os.makedirs(os.path.join(state, "jobs"))
    proc = run_hook(state, NOW)
    assert proc.returncode == 0
    assert "[ai-router]" in proc.stdout
    assert "never run" in proc.stdout
    assert "ai-router-ranking" in proc.stdout


def test_unparseable_last_run_speaks(tmp_path):
    state = str(tmp_path / "state")
    write(os.path.join(state, "ranking.last-run.json"), "{not json")
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "unreadable" in proc.stdout


def test_failed_run_speaks_with_log_path(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.last-run.json"), last_run(exit_code=1))
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "exit 1" in proc.stdout
    assert "ranking.log" in proc.stdout
    assert "journalctl --user -u ai-router-ranking" in proc.stdout


def test_stale_run_speaks(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.last-run.json"), last_run(age_s=4 * 86400))
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "days" in proc.stdout


def test_skipped_run_speaks(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.last-run.json"), last_run(exit_code=0, skipped=True))
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "update_ranking.py" in proc.stdout


def test_unfinished_run_older_than_an_hour_speaks(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.last-run.json"), last_run(exit_code=None, age_s=2 * 3600, finished=False))
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "did not finish" in proc.stdout


def test_unfinished_recent_run_is_silent(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.last-run.json"), last_run(exit_code=None, age_s=120, finished=False))
    proc = run_hook(state, NOW)
    assert proc.stdout == ""


def test_all_sources_failed_speaks(tmp_path):
    state = healthy_state(tmp_path)
    write(
        os.path.join(state, "ranking.auto.json"),
        {"generated_at": NOW - 60, "sources": [{"name": "lmarena", "ok": False, "error": "HTTP 503"}, {"name": "epoch", "ok": False, "error": "timeout"}], "disagreements": []},
    )
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "lmarena" in proc.stdout and "epoch" in proc.stdout


def test_one_source_failed_is_silent(tmp_path):
    state = healthy_state(tmp_path)
    write(
        os.path.join(state, "ranking.auto.json"),
        {"generated_at": NOW - 60, "sources": [{"name": "lmarena", "ok": True}, {"name": "epoch", "ok": False, "error": "timeout"}], "disagreements": []},
    )
    proc = run_hook(state, NOW)
    assert proc.stdout == ""


def test_disagreement_speaks_once_per_session(tmp_path):
    state = healthy_state(tmp_path)
    write(
        os.path.join(state, "ranking.auto.json"),
        {"generated_at": NOW - 60, "sources": [{"name": "lmarena", "ok": True}], "disagreements": [{"model": "gpt-5.6-sol", "pin_rank": 1, "evidence_rank": 3}]},
    )
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "ranking.proposal.md" in proc.stdout


def test_active_rejection_speaks(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "quota", "codex.rejected.json"), {"provider": "codex", "until": NOW + 3600, "reason": "usage limit reached", "observed_at": NOW - 60})
    proc = run_hook(state, NOW)
    assert "[ai-router]" in proc.stdout
    assert "codex" in proc.stdout and "rejected" in proc.stdout
    assert "usage limit reached" in proc.stdout


def test_rejection_without_until_uses_default_ttl(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "quota", "claude.rejected.json"), {"provider": "claude", "until": None, "reason": "rate_limit", "observed_at": NOW - 600})
    proc = run_hook(state, NOW)
    assert "claude" in proc.stdout and "rejected" in proc.stdout
    write(os.path.join(state, "quota", "claude.rejected.json"), {"provider": "claude", "until": None, "reason": "rate_limit", "observed_at": NOW - 7200})
    proc = run_hook(state, NOW)
    assert proc.stdout == ""


def test_settings_wires_hook_at_session_start():
    with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as fh:
        settings = json.load(fh)
    commands = [
        hook.get("command", "")
        for entry in settings["hooks"]["SessionStart"]
        for hook in entry.get("hooks", [])
    ]
    matches = [c for c in commands if c.endswith("hooks/ai-router-health.py")]
    assert matches == ["python3 -S -E $HOME/.claude/hooks/ai-router-health.py"]


def test_hook_is_fast_and_fail_open(tmp_path):
    state = healthy_state(tmp_path)
    write(os.path.join(state, "ranking.auto.json"), "garbage")
    started = time.monotonic()
    proc = run_hook(state, NOW)
    assert time.monotonic() - started < 2.0
    assert proc.returncode == 0
    assert "[ai-router]" in proc.stdout and "unreadable" in proc.stdout

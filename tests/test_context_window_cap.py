"""The context window our hooks measure against is the one Claude Code lets fill.

settings.json `autoCompactWindow` caps when Claude Code auto-compacts a session:
it compacts at min(model window, cap) - output reserve - buffer. A 1M Opus
session with a 400k cap never reaches 700k, so a nudge at "70% of 1M" would
never fire, and self-compact must hand off before Claude's own compaction does.

Claude Code 2.1.310 facts these tests mirror (binary + headless `--debug-file`
runs, 2026-10-24):
  * top-level `autoCompactWindow` is an INTEGER in [100000, 1000000]; any other
    value (a "400k" string, out of range) is silently dropped (zod `.catch`).
    Measured: `{"autoCompactWindow": 150000}` -> effectiveWindow=130000,
    `{"autoCompactWindow": "150k"}` -> effectiveWindow=980000 (ignored).
  * CLAUDE_CODE_AUTO_COMPACT_WINDOW beats the setting and accepts k/M
    suffixes; out-of-range values clamp to the bounds, garbage is ignored.
    Measured: setting 400000 + env 250k -> effectiveWindow=230000.
  * effectiveWindow = min(model window, cap) - min(maxOutputTokens, 20000);
    auto-compact fires at effectiveWindow - 13000.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import context_usage  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "self_compact_trigger", REPO / "self-compact-trigger.py")
sct = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sct)

CB_SPEC = importlib.util.spec_from_file_location(
    "context_baggage", REPO / "context-baggage.py")
cb = importlib.util.module_from_spec(CB_SPEC)
CB_SPEC.loader.exec_module(cb)

# Claude Code 2.1.310: MJ() subtracts min(maxOutputTokens, 20000) from the
# window, GQ() subtracts a further 13000 to get the compact threshold.
CLAUDE_OUTPUT_RESERVE = 20_000
CLAUDE_COMPACT_BUFFER = 13_000


def claude_auto_compact_point(window: int) -> int:
    return window - CLAUDE_OUTPUT_RESERVE - CLAUDE_COMPACT_BUFFER


def write_settings(home: Path, settings: dict) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))
    return path


def write_transcript(path: Path, model: str, tokens: int, user_lines: int = 5) -> Path:
    lines = [{"type": "user", "message": {"role": "user", "content": "work"}}
             for _ in range(user_lines)]
    lines.append({"type": "assistant", "message": {
        "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 500, "cache_creation_input_tokens": 500,
                  "cache_read_input_tokens": tokens - 1000, "output_tokens": 10}}})
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return path


# --- reading the configured cap, at the edge ---------------------------------

def test_settings_integer_is_the_cap(tmp_path):
    path = write_settings(tmp_path, {"autoCompactWindow": 400_000})
    assert context_usage.auto_compact_window(path, {}) == 400_000


@pytest.mark.parametrize("value", ["400k", 50_000, 2_000_000, True, None])
def test_settings_values_claude_code_drops_are_no_cap(tmp_path, value):
    path = write_settings(tmp_path, {"autoCompactWindow": value})
    assert context_usage.auto_compact_window(path, {}) is None


def test_missing_or_unreadable_settings_is_no_cap(tmp_path):
    assert context_usage.auto_compact_window(tmp_path / "absent.json", {}) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert context_usage.auto_compact_window(bad, {}) is None


@pytest.mark.parametrize("raw,expected", [
    ("250k", 250_000), ("1M", 1_000_000), ("300000", 300_000),
    ("50k", 100_000),        # below minimum clamps up, as Claude Code does
    ("2M", 1_000_000),       # above maximum clamps down
])
def test_env_beats_settings(tmp_path, raw, expected):
    path = write_settings(tmp_path, {"autoCompactWindow": 400_000})
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": raw}
    assert context_usage.auto_compact_window(path, env) == expected


@pytest.mark.parametrize("raw", ["junk", "", "0", "infk", "1e999k", "nanm"])
def test_unusable_env_falls_back_to_settings(tmp_path, raw):
    path = write_settings(tmp_path, {"autoCompactWindow": 400_000})
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": raw}
    assert context_usage.auto_compact_window(path, env) == 400_000


# --- the window consumers measure against ------------------------------------

def test_cap_below_model_window_becomes_the_window():
    win = context_usage.detect_window("claude-opus-5[1m]", 0, auto_compact_window=400_000)
    assert win.size == 400_000
    assert win.known


def test_cap_above_model_window_changes_nothing():
    win = context_usage.detect_window("claude-haiku-4-5", 0, auto_compact_window=400_000)
    assert win == context_usage.detect_window("claude-haiku-4-5", 0)


def test_cap_below_an_assumed_window_is_known():
    # An unknown model is assumed 200k, a LOWER bound on its real window. A cap
    # under that bound is the effective window whatever the model really is.
    win = context_usage.detect_window("claude-zeta-9", 0, auto_compact_window=150_000)
    assert (win.size, win.known) == (150_000, True)


def test_occupancy_fill_is_relative_to_the_capped_window(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5", 280_000)
    occ = context_usage.occupancy(str(t), auto_compact_window=400_000)
    assert occ.window == 400_000
    assert occ.fill_pct == pytest.approx(0.70)


# --- entrypoints read the cap themselves --------------------------------------

def run_hook(script: str, payload: dict, home: Path, env_extra: dict | None = None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CONTEXT_MAX")}
    env.update({"HOME": str(home)}, **(env_extra or {}))
    return subprocess.run([sys.executable, str(REPO / script)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=30)


def test_nudge_fires_at_threshold_of_the_capped_window(tmp_path):
    write_settings(tmp_path, {"autoCompactWindow": 400_000})
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5[1m]", 370_000)
    out = run_hook("context-baggage.py", {"transcript_path": str(t)}, tmp_path)
    assert out.returncode == 0, out.stderr
    assert "Context is 92% full" in out.stdout
    assert "window: 400k" in out.stdout


def test_nudge_honours_the_env_cap(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5[1m]", 290_000)
    out = run_hook("context-baggage.py", {"transcript_path": str(t)}, tmp_path,
                   {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300k"})
    assert "Context is 96% full" in out.stdout


def test_nudge_silent_below_threshold_without_a_cap(tmp_path):
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5[1m]", 290_000)
    out = run_hook("context-baggage.py", {"transcript_path": str(t)}, tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_cli_reports_the_capped_window(tmp_path):
    write_settings(tmp_path, {"autoCompactWindow": 400_000})
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5", 200_000)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CONTEXT_MAX")}
    env["HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, str(REPO / "context_usage.py"), str(t)],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["window"] == 400_000


def test_self_compact_request_records_the_capped_window(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write_settings(home, {"autoCompactWindow": 400_000})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    monkeypatch.delenv("CLAUDE_CONTEXT_MAX", raising=False)
    monkeypatch.delenv("REFLECT_SUBPROCESS", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sct, "spawn_inplace_reset", lambda sid, instructions="": "stubbed")
    t = write_transcript(tmp_path / "t.jsonl", "claude-opus-5[1m]", sct.FIRE_AT_TOKENS + 1)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(sct.sys, "stdin", io.StringIO(json.dumps(
        {"session_id": sid, "transcript_path": str(t), "cwd": str(cwd)})))
    assert sct.main() == 0
    request = json.loads((tmp_path / "sessions" / sid / "request.json").read_text())
    assert request["window"] == 400_000


# --- the ordering invariant ---------------------------------------------------

def test_self_compact_fires_before_claude_code_auto_compacts():
    """Self-compact hands off from a warm cache; auto-compact is the backstop.

    If the configured cap is retuned so low that Claude Code compacts first,
    self-compact never fires on a capped session. Read from the repo's own
    settings.json, so retuning the cap and FIRE_AT_TOKENS stay one decision.
    """
    cap = context_usage.auto_compact_window(REPO / "settings.json", {})
    assert cap is not None, "settings.json must set autoCompactWindow (the backstop)"
    assert sct.FIRE_AT_TOKENS < claude_auto_compact_point(cap)


def test_nudge_falls_between_self_compact_and_auto_compact():
    """Self-compact hands off unattended first; the human nudge is the fallback
    for when it did not, and must still land before Claude Code compacts."""
    cap = context_usage.auto_compact_window(REPO / "settings.json", {})
    nudge_at = cb.THRESHOLD * cap
    assert sct.FIRE_AT_TOKENS < nudge_at < claude_auto_compact_point(cap)

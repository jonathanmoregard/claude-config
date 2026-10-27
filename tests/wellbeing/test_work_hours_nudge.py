#!/usr/bin/env python3
"""
Plain-assert tests for work-hours-nudge.py (no pytest on this machine).

Run:  python3 tests/wellbeing/test_work_hours_nudge.py

Invariants:
  * prompts typed in an interactive Codex session count toward today's hours
  * Codex sessions driven by automation (codex exec, subagents) do not
  * a Claude prompt and a Codex prompt in the same 5-minute window are one
    window, not two
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The scripts live under runtime/, the tree projected into ~/.claude; this
# suite does not, so it reaches across rather than up.
WELLBEING = Path(__file__).resolve().parents[2] / "runtime" / "wellbeing"
WINDOW_H = 5 / 60  # one prompt claims a 5-minute window


def _load():
    spec = importlib.util.spec_from_file_location(
        "work_hours_nudge", WELLBEING / "work-hours-nudge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(local_naive: datetime) -> str:
    return local_naive.astimezone().astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _codex_rollout(root: Path, name: str, source, times: list[datetime]) -> None:
    entries = [{
        "timestamp": _iso(times[0]),
        "type": "session_meta",
        "payload": {"id": name, "originator": "x", "source": source,
                    "cwd": "/home/u/proj"},
    }]
    for t in times:
        entries.append({
            "timestamp": _iso(t),
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "env ctx"}]},
        })
        entries.append({
            "timestamp": _iso(t),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hi"},
        })
        entries.append({
            "timestamp": _iso(t + timedelta(seconds=30)),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "ok"},
        })
    d = times[0]
    _write_jsonl(
        root / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"rollout-{name}.jsonl",
        entries,
    )


def _claude_session(root: Path, name: str, times: list[datetime]) -> None:
    _write_jsonl(root / "proj" / f"{name}.jsonl", [{
        "type": "user", "timestamp": _iso(t), "cwd": "/home/u/proj",
        "entrypoint": "cli", "message": {"content": "hi"},
    } for t in times])


def _hours(setup) -> float:
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mod.PROJECTS_DIR = tmp / "claude-projects"
        mod.CODEX_SESSIONS_DIR = tmp / "codex-sessions"
        mod.CACHE = tmp / "cache.json"
        mod.PROJECTS_DIR.mkdir()
        mod.CODEX_SESSIONS_DIR.mkdir()
        now = datetime.now().replace(microsecond=0)
        base = max(now.replace(hour=0, minute=0, second=0) + timedelta(minutes=1),
                   now - timedelta(hours=3))
        setup(mod.PROJECTS_DIR, mod.CODEX_SESSIONS_DIR, base)
        total, _ = mod._compute_hours(now)
        return total


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def test_interactive_codex_prompts_count():
    total = _hours(lambda c, x, b: _codex_rollout(
        x, "tui", "cli", [b, b + timedelta(minutes=20)]))
    assert _close(total, 2 * WINDOW_H), total


def test_vscode_codex_prompts_count():
    total = _hours(lambda c, x, b: _codex_rollout(x, "ide", "vscode", [b]))
    assert _close(total, WINDOW_H), total


def test_codex_exec_and_subagents_do_not_count():
    def setup(c, x, b):
        _codex_rollout(x, "exec", "exec", [b, b + timedelta(minutes=20)])
        _codex_rollout(x, "sub", {"subagent": {"thread_spawn": {}}},
                       [b + timedelta(minutes=40)])
    assert _close(_hours(setup), 0.0)


def test_claude_and_codex_overlap_is_not_double_counted():
    def setup(c, x, b):
        _claude_session(c, "s1", [b])
        _codex_rollout(x, "tui", "cli", [b + timedelta(minutes=1)])
    total = _hours(setup)
    assert _close(total, WINDOW_H + 1 / 60), total


def test_claude_and_codex_days_add_up():
    def setup(c, x, b):
        _claude_session(c, "s1", [b])
        _codex_rollout(x, "tui", "cli", [b + timedelta(minutes=30)])
    assert _close(_hours(setup), 2 * WINDOW_H)


def test_missing_codex_dir_is_harmless():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mod.PROJECTS_DIR = tmp / "p"
        mod.PROJECTS_DIR.mkdir()
        mod.CODEX_SESSIONS_DIR = tmp / "absent"
        mod.CACHE = tmp / "cache.json"
        total, _ = mod._compute_hours(datetime.now())
        assert total == 0.0


def test_codex_rollout_being_written_does_not_crash():
    """A rollout read mid-append can end in a cut multibyte char or hold a
    non-object JSON line; the hook must skip it, not raise."""
    def setup(c, x, b):
        _codex_rollout(x, "tui", "cli", [b])
        f = next(x.rglob("rollout-tui.jsonl"))
        with open(f, "ab") as fh:
            fh.write(b"[1, 2]\n")
            fh.write('{"type": "event_msg", "payload": {"message": "hå'
                     .encode()[:-1])
    assert _close(_hours(setup), WINDOW_H)


def test_codex_line_with_non_string_timestamp_is_skipped():
    """A user_message whose timestamp is not a string is skipped; the valid
    prompts around it still count."""
    for ts in (123, 1.5, ["2026"], {"t": 1}, True):
        def setup(c, x, b, ts=ts):
            _codex_rollout(x, "tui", "cli", [b, b + timedelta(minutes=20)])
            f = next(x.rglob("rollout-tui.jsonl"))
            with open(f, "a") as fh:
                fh.write(json.dumps({
                    "timestamp": ts, "type": "event_msg",
                    "payload": {"type": "user_message", "message": "x"},
                }) + "\n")
        total = _hours(setup)
        assert _close(total, 2 * WINDOW_H), (ts, total)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

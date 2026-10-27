#!/usr/bin/env python3
"""
Plain-assert tests for the Claude-transcript reader in work-hours-nudge.py
(no pytest on this machine).

Run:  python3 ~/.claude/wellbeing/tests/test_work_hours_claude_reader.py

Invariant: a malformed transcript never takes the hook down. A file that ends
mid-UTF-8 character (a session still being appended to) or holds a JSON value
that is not a transcript entry is skipped line-by-line, and every well-formed
prompt around it still counts. A crash here writes no cache, so it would
re-crash on every prompt.
"""
from __future__ import annotations

import importlib.util
import json
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


def _prompt(t: datetime) -> bytes:
    return (json.dumps({"type": "user", "timestamp": _iso(t), "cwd": "/home/u/p",
                        "entrypoint": "cli", "message": {"content": "hi"}})
            + "\n").encode()


def _hours_with(junk: bytes) -> float:
    """Two real prompts 20 min apart, `junk` between them and again at EOF."""
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mod.PROJECTS_DIR = tmp / "projects"
        mod.CACHE = tmp / "cache.json"
        now = datetime.now().replace(microsecond=0)
        base = max(now.replace(hour=0, minute=0, second=0) + timedelta(minutes=1),
                   now - timedelta(hours=3))
        path = mod.PROJECTS_DIR / "p" / "s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(_prompt(base) + junk + b"\n"
                         + _prompt(base + timedelta(minutes=20)) + junk)
        total, _ = mod._compute_hours(now)
        assert mod.CACHE.exists(), "no cache written"
        return total


def _assert_both_prompts_count(junk: bytes) -> None:
    total = _hours_with(junk)
    assert abs(total - 2 * WINDOW_H) < 1e-6, total


def test_file_ending_mid_utf8_character():
    _assert_both_prompts_count('{"type":"user","message":{"content":"hä'.encode()[:-1])


def test_json_values_that_are_not_objects():
    for junk in (b"[1, 2]", b'"text"', b"42", b"null"):
        _assert_both_prompts_count(junk)


def test_entry_with_non_object_message_or_null_cwd():
    _assert_both_prompts_count(json.dumps(
        {"type": "user", "message": "x", "timestamp": "2026-01-01T00:00:00Z"}).encode())
    _assert_both_prompts_count(json.dumps(
        {"type": "user", "cwd": None, "message": {"content": "x"},
         "timestamp": "2026-01-01T00:00:00Z"}).encode())


def test_entry_with_non_string_timestamp():
    for ts in (123, 1.5, ["2026"], {"t": 1}):
        _assert_both_prompts_count(json.dumps(
            {"type": "user", "timestamp": ts, "message": {"content": "x"}}).encode())


def test_invalid_utf8_inside_an_otherwise_valid_file():
    _assert_both_prompts_count(b"\xff\xfe not text")


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
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

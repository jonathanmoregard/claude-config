#!/usr/bin/env python3
"""
Plain-assert tests for sunset-walk-tracker.py (no pytest on this machine).

Run:  python3 ~/.claude/wellbeing/tests/test_sunset_walk_tracker.py

Covers:
  * local NOAA sunset computation at both solstice extremes (Stockholm refs)
  * cross-check against a known Open-Meteo value for the configured place
  * stale-cache + network-down -> local recompute path (urllib mocked to raise)
  * SUNSET_WALK_OFFLINE=1 skips the network entirely
  * nudge message stays honest when weather is unknown
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

WELLBEING = Path(__file__).resolve().parent.parent


def _load(script: str):
    spec = importlib.util.spec_from_file_location(
        script.replace("-", "_"), WELLBEING / f"{script}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tracker = _load("sunset-walk-tracker")
nudge = _load("sunset-walk-nudge")

STOCKHOLM = (59.33, 18.07)


def _assert_close(actual: datetime, expected: datetime, tol_min: int = 10) -> None:
    delta = abs((actual - expected).total_seconds()) / 60
    assert delta <= tol_min, f"sunset {actual} differs from reference {expected} by {delta:.1f} min"


def test_sunset_stockholm_summer_solstice():
    # Reference: Stockholm 2026-06-21 sunset ~= 22:08 CEST (verified by hand
    # from the NOAA formulas: eqtime -1.3 min, decl +23.45 deg -> 20:08 UTC).
    got = tracker.sunset_local(date(2026, 6, 21), *STOCKHOLM)
    _assert_close(got, datetime(2026, 6, 21, 22, 8))


def test_sunset_stockholm_winter_solstice():
    # Reference: Stockholm 2026-12-21 sunset ~= 14:48 CET (verified by hand
    # from the NOAA formulas: eqtime +2.2 min, decl -23.42 deg -> 13:48 UTC).
    got = tracker.sunset_local(date(2026, 12, 21), *STOCKHOLM)
    _assert_close(got, datetime(2026, 12, 21, 14, 48))


def test_sunset_uppsala_matches_open_meteo():
    # Open-Meteo reported 22:04 local for the configured place on 2026-07-08 (from the
    # last good cache written by the API path).
    got = tracker.sunset_local(date(2026, 7, 8), tracker.LAT, tracker.LON)
    _assert_close(got, datetime(2026, 7, 8, 22, 4))


def test_offline_state_advances_past_window():
    # 23:30 is after sunset+15min in July -> state must describe tomorrow.
    now = datetime(2026, 7, 8, 23, 30)
    state = tracker.compute_state_offline(now)
    assert state["sunset_date"] == "2026-07-09", state
    assert state["good"] is True, state


class _FakeResp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_tracker_main(env_offline: bool = False, urlopen=None):
    """Run tracker.main() with CACHE redirected to a temp file and the
    environment snapshotted/restored. Returns (exit_code, stdout, stderr,
    cache_path)."""
    tmp = Path(tempfile.mkdtemp(prefix="sunset-test-")) / ".sunset-walk-cache.json"
    # Pre-seed a stale cache (3 days old) to exercise stale -> recompute.
    stale = {"ts": time.time() - 3 * 86400, "good": False, "sunset_date": "2026-07-05"}
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(stale))

    out, err = io.StringIO(), io.StringIO()
    from contextlib import ExitStack

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(tracker, "CACHE", tmp))
        # patch.dict snapshots on __enter__ and restores on __exit__, so
        # mutations below never leak into the real process environment.
        stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
        if env_offline:
            os.environ["SUNSET_WALK_OFFLINE"] = "1"
        else:
            os.environ.pop("SUNSET_WALK_OFFLINE", None)
        if urlopen is not None:
            stack.enter_context(
                mock.patch.object(tracker.urllib.request, "urlopen", urlopen)
            )
        with redirect_stdout(out), redirect_stderr(err):
            code = tracker.main()
    return code, out.getvalue(), err.getvalue(), tmp


def _mock_503():
    return mock.Mock(side_effect=urllib.error.HTTPError(
        tracker.ENDPOINT, 503, "Service Unavailable", hdrs=None, fp=None
    ))


def test_stale_cache_network_down_recomputes_locally():
    code, out, err, cache = _run_tracker_main(urlopen=_mock_503())
    assert code == 0, f"exit {code}, stderr: {err}"
    state = json.loads(cache.read_text())
    assert time.time() - state["ts"] < 60, "cache ts not refreshed"
    assert state["source"] == "local", state
    assert state["good"] is True, state
    assert state["cloud_cover_pct"] is None, state
    for key in ("sunset_iso", "sunset_hhmm", "sunset_date",
                "window_start_iso", "window_end_iso", "lat", "lon"):
        assert key in state, f"missing contract key {key}"
    # Healthy contract: JSON state printed to stdout.
    assert json.loads(out)["sunset_date"] == state["sunset_date"]
    assert "falling back to local" in err.lower(), err


def test_env_offline_skips_network():
    boom = mock.Mock(side_effect=AssertionError("network touched"))
    code, out, err, cache = _run_tracker_main(env_offline=True, urlopen=boom)
    assert code == 0, f"exit {code}, stderr: {err}"
    state = json.loads(cache.read_text())
    assert state["source"] == "local", state


def test_degenerate_api_payload_falls_back_to_local():
    # HTTP 200 with unusable data (null sunsets) must not crash main() or
    # leave the cache stale — it falls back to local computation.
    today = datetime.now().strftime("%Y-%m-%d")
    payload = {"daily": {"time": [today], "sunset": [None]}, "hourly": {}}
    resp = mock.Mock(side_effect=lambda *a, **k: _FakeResp(payload))
    code, out, err, cache = _run_tracker_main(urlopen=resp)
    assert code == 0, f"exit {code}, stderr: {err}"
    state = json.loads(cache.read_text())
    assert time.time() - state["ts"] < 60, "cache ts not refreshed"
    assert state["source"] == "local", state
    assert "falling back to local" in err.lower(), err


def test_bare_oserror_falls_back_to_local():
    # ConnectionResetError is an OSError but not a URLError/TimeoutError;
    # it must be caught like any other network failure.
    resp = mock.Mock(side_effect=ConnectionResetError("connection reset"))
    code, out, err, cache = _run_tracker_main(urlopen=resp)
    assert code == 0, f"exit {code}, stderr: {err}"
    state = json.loads(cache.read_text())
    assert state["source"] == "local", state


def test_api_advance_picks_day_after_today():
    # When today is NOT at days[0] (e.g. payload includes yesterday) and the
    # window has passed, the advance branch must pick the day after today,
    # not blindly sunsets[1] (which would be today's already-past sunset).
    now = datetime(2026, 7, 8, 23, 30)
    data = {
        "daily": {
            "time": ["2026-07-07", "2026-07-08", "2026-07-09"],
            "sunset": ["2026-07-07T22:05", "2026-07-08T22:04", "2026-07-09T22:03"],
        },
        "hourly": {
            "time": ["2026-07-09T22:00"],
            "cloud_cover": [10],
            "precipitation": [0.0],
            "visibility": [20000.0],
        },
    }
    state = tracker.compute_state(now, data)
    assert state.get("sunset_date") == "2026-07-09", state


def test_nudge_honest_when_weather_unknown():
    tmpdir = Path(tempfile.mkdtemp(prefix="sunset-nudge-test-"))
    cache_p = tmpdir / "cache.json"
    fired_p = tmpdir / "fired.json"
    now = datetime.now()
    sunset = now + timedelta(minutes=20)  # inside pre-nudge window
    cache_p.write_text(json.dumps({
        "ts": time.time(),
        "good": True,
        "sunset_iso": sunset.isoformat(),
        "sunset_hhmm": sunset.strftime("%H:%M"),
        "sunset_date": sunset.date().isoformat(),
        "window_start_iso": (sunset - timedelta(minutes=30)).isoformat(),
        "window_end_iso": (sunset + timedelta(minutes=15)).isoformat(),
        "cloud_cover_pct": None,
        "precipitation_mm": None,
        "visibility_m": None,
        "source": "local",
    }))
    out = io.StringIO()
    with mock.patch.object(nudge, "CACHE", cache_p), \
         mock.patch.object(nudge, "FIRED", fired_p), \
         redirect_stdout(out):
        code = nudge.main()
    text = out.getvalue()
    assert code == 0
    assert "Sunset is at" in text, f"nudge did not fire: {text!r}"
    assert "clear visibility" not in text, f"claims visibility w/o data: {text!r}"
    assert "not verified" in text, f"missing honesty marker: {text!r}"


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
    raise SystemExit(main())

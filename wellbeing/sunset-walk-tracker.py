#!/usr/bin/env python3
"""
Cron-friendly tracker for tonight's sunset-walk window in the configured place.

Fetches Open-Meteo (free, no API key), computes whether tonight qualifies as
"good weather for a sunset walk", and writes ~/.claude/tmp/.sunset-walk-cache.json
for the sunset-walk-nudge hook to read.

"Good weather" rule:
  * cloud cover at sunset hour < 40%
  * no precipitation in the sunset hour
  * visibility >= 5 km

Walk window: sunset - 30min to sunset + 15min (golden hour + early civil
twilight). If today's window has already passed, the next day's sunset is
used so the cache always describes the next upcoming opportunity.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

LAT = 0.0
LON = 0.0
TIMEZONE = "Europe/Stockholm"
CACHE = Path.home() / ".claude" / "tmp" / ".sunset-walk-cache.json"
ENDPOINT = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&hourly=cloud_cover,precipitation,visibility"
    "&daily=sunset"
    f"&timezone={TIMEZONE.replace('/', '%2F')}"
    "&forecast_days=2"
)

CLOUD_MAX_PCT = 40
VIS_MIN_M = 5000
WINDOW_BEFORE_MIN = 30
WINDOW_AFTER_MIN = 15


def _fetch() -> dict:
    req = urllib.request.Request(ENDPOINT, headers={"User-Agent": "claude-sunset-walk/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _hour_index(times: list[str], target: datetime) -> int | None:
    target_str = target.strftime("%Y-%m-%dT%H:00")
    try:
        return times.index(target_str)
    except ValueError:
        return None


def compute_state(now: datetime, data: dict) -> dict:
    daily = data.get("daily", {})
    sunsets = daily.get("sunset", [])
    days = daily.get("time", [])
    hourly = data.get("hourly", {})
    h_times = hourly.get("time", [])
    cloud = hourly.get("cloud_cover", [])
    precip = hourly.get("precipitation", [])
    vis = hourly.get("visibility", [])

    today = now.strftime("%Y-%m-%d")
    if today not in days:
        return {"ts": time.time(), "good": False, "reason": "no forecast for today"}
    sunset_str = sunsets[days.index(today)]
    sunset_dt = datetime.fromisoformat(sunset_str)

    # If today's window has fully passed, advance to tomorrow.
    if now > sunset_dt + timedelta(minutes=WINDOW_AFTER_MIN):
        if len(days) > 1:
            sunset_str = sunsets[1]
            sunset_dt = datetime.fromisoformat(sunset_str)
        else:
            return {"ts": time.time(), "good": False, "reason": "sunset past, no tomorrow data"}

    idx = _hour_index(h_times, sunset_dt)
    if idx is None:
        return {"ts": time.time(), "good": False, "reason": "sunset hour not in hourly forecast"}

    c = cloud[idx] if idx < len(cloud) else None
    p = precip[idx] if idx < len(precip) else None
    v = vis[idx] if idx < len(vis) else None

    good = (
        c is not None and c < CLOUD_MAX_PCT
        and p is not None and p == 0
        and v is not None and v >= VIS_MIN_M
    )

    window_start = sunset_dt - timedelta(minutes=WINDOW_BEFORE_MIN)
    window_end = sunset_dt + timedelta(minutes=WINDOW_AFTER_MIN)

    return {
        "ts": time.time(),
        "good": good,
        "sunset_iso": sunset_dt.isoformat(),
        "sunset_hhmm": sunset_dt.strftime("%H:%M"),
        "sunset_date": sunset_dt.date().isoformat(),
        "window_start_iso": window_start.isoformat(),
        "window_end_iso": window_end.isoformat(),
        "cloud_cover_pct": c,
        "precipitation_mm": p,
        "visibility_m": v,
        "lat": LAT,
        "lon": LON,
    }


def main() -> int:
    try:
        data = _fetch()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"[sunset-walk-tracker] fetch failed: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"[sunset-walk-tracker] bad JSON: {e}", file=sys.stderr)
        return 1

    state = compute_state(datetime.now(), data)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

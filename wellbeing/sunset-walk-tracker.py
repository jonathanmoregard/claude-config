#!/usr/bin/env python3
"""
Cron-friendly tracker for tonight's sunset-walk window in the configured place.

Primary path: fetch Open-Meteo (free, no API key) for sunset time + weather,
compute whether tonight qualifies as "good weather for a sunset walk", and
write ~/.claude/tmp/.sunset-walk-cache.json for the sunset-walk-nudge hook.

Offline path: if the fetch fails (or SUNSET_WALK_OFFLINE=1), compute sunset
locally with the NOAA solar-position approximation (stdlib math only,
accurate to a few minutes). Weather is then unknown; we stay optimistic
(good=True, weather fields None) so nudges keep working — a sunset walk
nudge on an occasional cloudy day beats days of silence. The nudge hook
words its message honestly when weather fields are None.

"Good weather" rule (API path only):
  * cloud cover at sunset hour < 40%
  * no precipitation in the sunset hour
  * visibility >= 5 km

Walk window: sunset - 30min to sunset + 15min (golden hour + early civil
twilight). If today's window has already passed, the next day's sunset is
used so the cache always describes the next upcoming opportunity.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


# --- local sunset computation (NOAA solar-position approximation) ----------
#
# NOAA "General Solar Position Calculations" (ESRL Global Monitoring Div.).
# Accuracy is within a few minutes at Swedish latitudes — plenty for a walk
# nudge. Stdlib math only, no network.

_SUNSET_ZENITH_DEG = 90.833  # official sunset: refraction + solar disc radius


def sunset_utc(d: date, lat: float, lon: float) -> datetime | None:
    """Sunset on date *d* as an aware UTC datetime.

    Longitude positive east. Returns None during polar day/night.
    """
    day_of_year = d.timetuple().tm_yday
    # Fractional year at solar noon (hour=12 -> term drops out).
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1)

    # Equation of time (minutes).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    # Solar declination (radians).
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    lat_rad = math.radians(lat)
    cos_ha = (
        math.cos(math.radians(_SUNSET_ZENITH_DEG)) / (math.cos(lat_rad) * math.cos(decl))
        - math.tan(lat_rad) * math.tan(decl)
    )
    if cos_ha < -1.0 or cos_ha > 1.0:
        return None  # polar day (< -1) or polar night (> 1)

    ha_deg = math.degrees(math.acos(cos_ha))
    sunset_minutes_utc = 720.0 - 4.0 * (lon - ha_deg) - eqtime
    base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return base + timedelta(minutes=sunset_minutes_utc)


def sunset_local(d: date, lat: float = LAT, lon: float = LON,
                 tz: str = TIMEZONE) -> datetime | None:
    """Sunset on date *d* as a naive local-time datetime (same shape as the
    Open-Meteo daily.sunset strings). None during polar day/night."""
    utc = sunset_utc(d, lat, lon)
    if utc is None:
        return None
    return utc.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def compute_state_offline(now: datetime, lat: float = LAT, lon: float = LON) -> dict:
    """Cache state from local computation only. Weather is unknown, so weather
    fields are None and good=True (optimistic — see module docstring)."""
    sunset_dt = sunset_local(now.date(), lat, lon)
    if sunset_dt is not None and now > sunset_dt + timedelta(minutes=WINDOW_AFTER_MIN):
        sunset_dt = sunset_local(now.date() + timedelta(days=1), lat, lon)
    if sunset_dt is None:
        return {
            "ts": time.time(),
            "good": False,
            "reason": "polar day/night: no sunset",
            "source": "local",
        }

    window_start = sunset_dt - timedelta(minutes=WINDOW_BEFORE_MIN)
    window_end = sunset_dt + timedelta(minutes=WINDOW_AFTER_MIN)
    return {
        "ts": time.time(),
        "good": True,
        "sunset_iso": sunset_dt.isoformat(),
        "sunset_hhmm": sunset_dt.strftime("%H:%M"),
        "sunset_date": sunset_dt.date().isoformat(),
        "window_start_iso": window_start.isoformat(),
        "window_end_iso": window_end.isoformat(),
        "cloud_cover_pct": None,
        "precipitation_mm": None,
        "visibility_m": None,
        "lat": lat,
        "lon": lon,
        "source": "local",
    }


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
    idx_today = days.index(today)
    sunset_str = sunsets[idx_today]
    sunset_dt = datetime.fromisoformat(sunset_str)

    # If today's window has fully passed, advance to the day after today
    # (today is not necessarily at days[0] — payloads may include past days).
    if now > sunset_dt + timedelta(minutes=WINDOW_AFTER_MIN):
        if idx_today + 1 < min(len(days), len(sunsets)):
            sunset_str = sunsets[idx_today + 1]
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
        "source": "api",
    }


def main() -> int:
    now = datetime.now()
    data = None
    if os.environ.get("SUNSET_WALK_OFFLINE") != "1":
        try:
            data = _fetch()
        except OSError as e:
            # URLError/HTTPError/TimeoutError are OSError subclasses; bare
            # OSErrors (e.g. ConnectionResetError mid-read) land here too.
            print(
                f"[sunset-walk-tracker] fetch failed: {e}; "
                "falling back to local sunset computation",
                file=sys.stderr,
            )
        except json.JSONDecodeError as e:
            print(
                f"[sunset-walk-tracker] bad JSON: {e}; "
                "falling back to local sunset computation",
                file=sys.stderr,
            )

    state = None
    if data is not None:
        try:
            state = compute_state(now, data)
        except Exception as e:
            # A 200 with a degenerate payload (null sunsets, short arrays,
            # malformed ISO strings) must never leave the cache stale.
            print(
                f"[sunset-walk-tracker] bad API data: {type(e).__name__}: {e}; "
                "falling back to local sunset computation",
                file=sys.stderr,
            )
    if state is None or "sunset_iso" not in state:
        # API down, offline mode, or API data unusable -> local computation.
        state = compute_state_offline(now)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(state, indent=2))
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

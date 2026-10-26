"""Per-bucket pacing terms (README "Selection").

One boundary, two consumers: ``selection`` (eligibility and the ``pace``
policy) and ``quota.live_supersedes`` (when a newer snapshot clears a
rejection marker). A provider the marker rule clears must be one ``pick``
would actually offer, so both read the same function.
"""

from __future__ import annotations

from typing import Any

from .config import Config


def bucket_status(cfg: Config, bucket: dict[str, Any], now: float) -> tuple[float, float | None, bool, float]:
    """(headroom, line, relaxed, floor) for one bucket at ``now``.

    headroom: 100 − used_pct.
    line: where linear pacing would leave the bucket now — 100 × the fraction
      of the window still ahead; None when the bucket carries no reset geometry
      (no ``resets_at`` or ``window_min``).
    relaxed: the bucket resets within ``settings.near_reset_relax_min`` and is
      exempt from the floor.
    floor: min(reserve_pct, line) — the reserve holds at ``reserve_pct`` for
      most of the window and descends with the line over the final stretch, so
      a period can end near 0 without running dry early; ``reserve_pct`` when
      the line is unknown.
    """
    room = 100.0 - float(bucket["used_pct"])
    resets_at, window_min = bucket.get("resets_at"), bucket.get("window_min")
    line: float | None = None
    relaxed = False
    if resets_at is not None:
        ahead_s = float(resets_at) - now
        relaxed = ahead_s <= cfg.settings.near_reset_relax_min * 60
        if window_min:
            line = 100.0 * max(0.0, min(1.0, ahead_s / (float(window_min) * 60.0)))
    floor = cfg.settings.reserve_pct if line is None else min(cfg.settings.reserve_pct, line)
    return room, line, relaxed, floor

"""A rejection marker is a hint with a horizon, not a verdict: a LIVE quota
observation made AFTER the rejection that shows headroom above reserve on every
applicable bucket supersedes it (the window rolled, credits were added, or the
parsed "try again at" was wrong).

Regression for 2026-09-14: `codex.rejected.json` from a 15:17 usage-limit error
carried `until` = Sep 19, while the app-server at 18:33 reported the weekly
bucket at 11 % in a new window. `pick` kept Codex ineligible for five days.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_router import quota, selection
from ai_router.config import atomic_write_json, load_config

NOW = 1_789_400_000
REJECTED_AT = NOW - 3 * 3600          # marker written three hours ago
UNTIL = NOW + 5 * 86400               # "try again at" five days out
RESERVE = 15.0                        # models.toml settings.reserve_pct


@pytest.fixture
def cfg():
    return load_config()


def codex_record(observed_at: int, used_pct: float, *, applies: list[str] | None = None,
                 extra: list[dict] | None = None, resets_at: int | None = None) -> dict:
    return {"provider": "codex", "source": "app-server", "observed_at": observed_at, "plan": "pro",
            "buckets": [{"id": "primary", "used_pct": used_pct,
                         "resets_at": UNTIL + 2 * 86400 if resets_at is None else resets_at,
                         "window_min": 10080, "applies_to": ["*"] if applies is None else applies}]
            + (extra or []),
            "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]}


def write_codex(state_dir: Path, record: dict) -> None:
    atomic_write_json(quota.snapshot_path(state_dir, "codex"), record)


def write_claude(state_dir: Path, observed_at: int, used_pct: float) -> None:
    raw = {"provider": "claude", "source": "statusline", "observed_at": observed_at, "cli_version": "2.1.270",
           "rate_limits": {"five_hour": {"used_percentage": used_pct, "resets_at": NOW + 3600},
                           "seven_day": {"used_percentage": used_pct, "resets_at": NOW + 86400}}}
    atomic_write_json(quota.snapshot_path(state_dir, "claude"), raw)


def broken(methods):
    raise RuntimeError("app-server down")


def marker(state_dir: Path, provider: str = "codex") -> Path:
    return quota.rejected_path(state_dir, provider)


# --- codex ---------------------------------------------------------------

def test_newer_healthy_snapshot_clears_stale_rejection(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 11.0))      # fresh cache, newer than the marker

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "ok" and s["rejected_until"] is None and s["reason"] is None
    assert not marker(state_dir).exists()
    assert quota.rejection(cfg, state_dir, "codex", NOW) is None


def test_snapshot_older_than_rejection_keeps_it(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(REJECTED_AT - 60, 11.0))   # stale cache from before the rejection

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "rejected" and s["rejected_until"] == UNTIL
    assert marker(state_dir).exists()


def test_newer_snapshot_still_at_cap_keeps_rejection(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 100.0))

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "rejected" and s["rejected_until"] == UNTIL
    assert marker(state_dir).exists()


def test_headroom_below_reserve_does_not_clear(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 100.0 - RESERVE + 0.5))   # headroom 14.5 < reserve

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "rejected" and marker(state_dir).exists()


def test_descended_floor_clears_marker_exactly_where_selection_offers_the_provider(cfg, state_dir: Path):
    """The clear rule must track selection's floor min(reserve, line), not a flat reserve:
    90 % used 3 h before a weekly reset → line 1.8 → pick offers sol → the marker must go."""
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 90.0, resets_at=NOW + 3 * 3600))
    write_claude(state_dir, NOW - 30, 40.0)

    snaps = quota.all_snapshots(cfg, state_dir, now=NOW, rpc=broken, urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("no egress")))
    r = selection.pick(cfg, snaps, "strongest", now=NOW)

    assert not marker(state_dir).exists()
    assert snaps["codex"]["status"] == "ok" and snaps["codex"]["rejected_until"] is None
    sol = next(c for c in r["candidates"] if c["model"] == "gpt-5.6-sol")
    assert sol["eligible"] is True and sol["headroom"] == 10
    # Same headroom with the whole window ahead: floor is the full reserve → marker stays
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 20, 90.0))
    snaps = quota.all_snapshots(cfg, state_dir, now=NOW, rpc=broken, urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("no egress")))
    assert marker(state_dir).exists() and snaps["codex"]["status"] == "rejected"


def test_headroom_equal_to_reserve_clears_like_selection_treats_it_eligible(cfg, state_dir: Path):
    """selection._evaluate rejects only `headroom < reserve`; the clear rule uses the same boundary."""
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 100.0 - RESERVE))   # headroom == reserve

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "ok" and not marker(state_dir).exists()


def test_one_capped_applicable_bucket_keeps_rejection(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    capped = {"id": "secondary", "used_pct": 100.0, "resets_at": NOW + 600, "window_min": 300, "applies_to": ["*"]}
    write_codex(state_dir, codex_record(NOW - 30, 11.0, extra=[capped]))

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "rejected" and marker(state_dir).exists()


def test_buckets_that_apply_to_nothing_are_no_evidence(cfg, state_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 0.0, applies=[]))   # e.g. codex_<other>.primary

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken)

    assert s["status"] == "rejected" and marker(state_dir).exists()


def test_live_probe_result_supersedes_rejection(cfg, state_dir: Path):
    """The clearing evidence may come straight from the RPC, not only from the cache."""
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)

    def rpc(methods):
        return {"account/rateLimits/read": {"rateLimits": {"planType": "pro",
                                                            "primary": {"usedPercent": 11, "resetsAt": UNTIL + 86400,
                                                                        "windowDurationMins": 10080}}},
                "model/list": {"data": [{"id": "gpt-5.6-luna"}]}}

    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=rpc)

    assert s["status"] == "ok" and s["rejected_until"] is None
    assert not marker(state_dir).exists()


# --- claude (symmetric) ---------------------------------------------------------------

def test_newer_statusline_snapshot_clears_claude_rejection(cfg, state_dir: Path, tmp_path: Path):
    quota.mark_rejected(state_dir, "claude", until=UNTIL, reason="rate_limit", source="stop-failure", now=REJECTED_AT)
    write_claude(state_dir, NOW - 30, 40.0)

    s = quota.probe_claude(cfg, state_dir, now=NOW, credentials_path=tmp_path / "none.json")

    assert s["status"] == "ok" and s["rejected_until"] is None
    assert not marker(state_dir, "claude").exists()


def test_claude_snapshot_at_cap_keeps_rejection(cfg, state_dir: Path, tmp_path: Path):
    quota.mark_rejected(state_dir, "claude", until=UNTIL, reason="rate_limit", source="stop-failure", now=REJECTED_AT)
    write_claude(state_dir, NOW - 30, 100.0)

    s = quota.probe_claude(cfg, state_dir, now=NOW, credentials_path=tmp_path / "none.json")

    assert s["status"] == "rejected" and marker(state_dir, "claude").exists()


# --- end to end through pick ---------------------------------------------------------------

def test_pick_offers_codex_again_after_window_rollover(cfg, state_dir: Path, tmp_path: Path):
    # The policy is not the subject here (the fixture's fresh window puts codex 11 pts
    # behind pace, which the default `pace` policy rightly diverts around): pin
    # rank-first so "offered again" reads as "picked again".
    p = tmp_path / "m.toml"
    p.write_text(re.sub(r'^policy = "[a-z-]+"', 'policy = "rank-first"', cfg.path.read_text(), count=1, flags=re.M))
    cfg = load_config(p)
    quota.mark_rejected(state_dir, "codex", until=UNTIL, reason="usage limit", source="job:x", now=REJECTED_AT)
    write_codex(state_dir, codex_record(NOW - 30, 11.0))
    write_claude(state_dir, NOW - 30, 40.0)

    snaps = quota.all_snapshots(cfg, state_dir, now=NOW, rpc=broken, urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("no egress")))
    r = selection.pick(cfg, snaps, "strongest", now=NOW)

    assert r["ok"] and r["provider"] == "codex" and r["model"] == "gpt-5.6-sol"
    assert all(c["eligible"] for c in r["candidates"] if c["provider"] == "codex")
    assert not any("rejected" in c["why"] for c in r["candidates"])
    assert json.loads(quota.snapshot_path(state_dir, "codex").read_text())["observed_at"] == NOW - 30  # cache untouched

"""Selection matrix (contract §Selection) plus models.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_router import config as cfgmod
from ai_router.config import ConfigError, load_config
from ai_router.selection import detect_host, pick

NOW = 1_789_400_000


def snap(provider: str, buckets: list[dict], status: str = "ok", **extra) -> dict:
    base = {
        "provider": provider, "status": status, "source": "test", "observed_at": NOW - 10,
        "age_s": 10, "stale": False, "plan": "x", "buckets": buckets,
        "rejected_until": None, "reason": None, "error": None,
    }
    base.update(extra)
    return base


def bucket(bid: str, used: float, resets_in_s: int = 100_000, applies=("*",), window_min: int = 10080) -> dict:
    return {"id": bid, "used_pct": used, "resets_at": NOW + resets_in_s,
            "window_min": window_min, "applies_to": list(applies)}


@pytest.fixture
def cfg():
    return load_config()


def with_policy(cfg, tmp_path: Path, policy: str):
    """Reload models.toml with ``settings.policy`` swapped (whatever the shipped default is)."""
    import re
    text = re.sub(r'^policy = "[a-z-]+"', f'policy = "{policy}"', cfg.path.read_text(), count=1, flags=re.M)
    assert f'policy = "{policy}"' in text
    p = tmp_path / "m.toml"
    p.write_text(text)
    return load_config(p)


def both_ok(codex_used: float = 10, claude_used: float = 10, resets_in_s: int = 100_000) -> dict:
    return {
        "codex": snap("codex", [bucket("primary", codex_used, resets_in_s)]),
        "claude": snap("claude", [bucket("five_hour", claude_used, resets_in_s),
                                  bucket("seven_day", claude_used, resets_in_s)]),
    }


# Fixture geometry: window 10080 min = 604 800 s. resets_in 100 000 s → 16.5 % of the
# window left → linear line 16.5; resets_in 500 000 s → 82.7 % left → line 82.7.
LINE_LATE = 100 * 100_000 / 604_800   # 16.53
LINE_EARLY = 100 * 500_000 / 604_800  # 82.67


# --- config -----------------------------------------------------------------

def test_models_toml_loads_and_resolves_aliases(cfg):
    assert cfg.settings.reserve_pct == 15
    assert cfg.model("sol").id == "gpt-5.6-sol"
    assert cfg.model("gpt-5.6").id == "gpt-5.6-sol"
    assert cfg.model("haiku").id == "claude-haiku-4-5-20251001"
    assert cfg.model("nope") is None
    assert [m.id for m in cfg.by_tier("fast")] == ["gpt-5.6-luna", "claude-haiku-4-5-20251001"]
    assert cfg.pins[0] == "gpt-5.6-sol"
    assert cfg.bucket_applies_to("claude", "seven_day_fable") == ("claude-fable-5-1",)
    assert cfg.efforts["codex"][-1] == "ultra"


def test_bad_models_toml_rejected(tmp_path: Path):
    p = tmp_path / "m.toml"
    p.write_text('version = 1\n[pins]\norder = ["ghost"]\n')
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("version = 2\n")
    with pytest.raises(ConfigError):
        load_config(p)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_state_dir_created_0700(tmp_path: Path):
    d = cfgmod.state_dir({"AI_ROUTER_STATE": str(tmp_path / "s")})
    assert d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o700


# --- pick: rank-first -----------------------------------------------------------

def test_rank_first_picks_first_pin_in_tier(cfg, tmp_path: Path):
    cfg = with_policy(cfg, tmp_path, "rank-first")
    r = pick(cfg, both_ok(), "strongest", now=NOW)
    assert r["ok"] and r["model"] == "gpt-5.6-sol" and r["provider"] == "codex"
    assert r["effort"] == "high" and r["tier"] == "strongest"
    assert r["headroom"] == 90
    assert [c["model"] for c in r["candidates"]] == ["gpt-5.6-sol", "claude-fable-5-1", "claude-opus-5"]
    assert r["native"] is False and r["host"] is None


def test_reserve_skips_low_headroom_provider(cfg):
    r = pick(cfg, both_ok(codex_used=90), "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1"
    sol = r["candidates"][0]
    assert sol["eligible"] is False and "reserve" in sol["why"] and sol["headroom"] == 10


def test_no_eligible_returns_not_ok_with_candidates(cfg):
    r = pick(cfg, both_ok(codex_used=95, claude_used=95), "fast", now=NOW)
    assert r["ok"] is False
    assert len(r["candidates"]) == 2 and all(not c["eligible"] for c in r["candidates"])
    assert "no eligible" in r["reason"]


def test_rejected_provider_is_ineligible(cfg):
    s = both_ok()
    s["codex"] = snap("codex", [bucket("primary", 5)], status="rejected",
                      rejected_until=NOW + 500, reason="usage limit")
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1"
    assert "rejected" in r["candidates"][0]["why"]


def test_unknown_quota_is_eligible_by_default_with_null_headroom(cfg):
    s = both_ok()
    s["claude"] = snap("claude", [], status="unknown", source="none")
    r = pick(cfg, s, "fast", provider="claude", now=NOW)
    assert r["ok"] and r["model"] == "claude-haiku-4-5-20251001"
    assert r["headroom"] is None and "unknown" in r["reason"]


def test_unknown_quota_ineligible_when_setting_off(cfg, tmp_path: Path):
    text = cfg.path.read_text().replace("unknown_is_eligible = true", "unknown_is_eligible = false")
    p = tmp_path / "m.toml"
    p.write_text(text)
    c2 = load_config(p)
    s = both_ok()
    s["claude"] = snap("claude", [], status="unknown", source="none")
    r = pick(c2, s, "fast", provider="claude", now=NOW)
    assert r["ok"] is False


def test_missing_provider_snapshot_treated_as_unknown(cfg):
    r = pick(cfg, {"codex": snap("codex", [bucket("primary", 0)])}, "fast", provider="claude", now=NOW)
    assert r["ok"] and r["headroom"] is None


def test_need_filters_on_caps(cfg):
    r = pick(cfg, both_ok(), "fast", need=("long-context",), now=NOW)
    assert r["ok"] is False
    assert all("long-context" in c["why"] for c in r["candidates"])
    r2 = pick(cfg, both_ok(), "fast", need=("vision",), now=NOW)
    assert r2["ok"] and r2["model"] == "gpt-5.6-luna"


def test_provider_filter(cfg):
    r = pick(cfg, both_ok(), "strongest", provider="claude", now=NOW)
    assert r["model"] == "claude-fable-5-1"
    assert [c["model"] for c in r["candidates"]] == ["claude-fable-5-1", "claude-opus-5"]


def test_family_scoped_bucket_only_hits_its_model(cfg):
    s = both_ok()
    s["claude"]["buckets"].append(bucket("seven_day_fable", 99, applies=("claude-fable-5-1",)))
    r = pick(cfg, s, "strongest", provider="claude", now=NOW)
    assert r["model"] == "claude-opus-5"
    assert r["candidates"][0]["headroom"] == 1


def test_informational_bucket_never_blocks(cfg):
    s = both_ok()
    s["codex"]["buckets"].append(bucket("codex_bengalfox.primary", 100, applies=()))
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["model"] == "gpt-5.6-sol" and r["headroom"] == 90


def test_near_reset_bucket_relaxed(cfg):
    s = both_ok()
    # 90 % used but resets in 30 min (< 120 min relax window) → not counted for reserve
    s["codex"] = snap("codex", [bucket("primary", 90, resets_in_s=30 * 60)])
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["model"] == "gpt-5.6-sol"
    assert "reset" in r["candidates"][0]["why"]
    # A 5 h bucket resetting in 3 h is NOT relaxed (and its floor is still the full
    # reserve: 60 % of the window is left, so the line sits at 60)
    s["codex"] = snap("codex", [bucket("primary", 90, resets_in_s=3 * 3600, window_min=300)])
    r2 = pick(cfg, s, "strongest", now=NOW)
    assert r2["model"] == "claude-fable-5-1"
    assert "reset" not in r2["candidates"][0]["why"] and "reserve" in r2["candidates"][0]["why"]


def test_codex_model_list_availability(cfg):
    s = both_ok()
    s["codex"]["models"] = ["gpt-5.6-terra", "gpt-5.6-luna"]  # sol missing
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1"
    assert "unavailable" in r["candidates"][0]["why"]
    # A stale list (> 1 h) is ignored → assume available
    s["codex"]["observed_at"] = NOW - 7200
    assert pick(cfg, s, "strongest", now=NOW)["model"] == "gpt-5.6-sol"


def test_allow_downgrade_walks_ladder(cfg):
    s = both_ok(codex_used=95, claude_used=10)
    s["claude"]["buckets"].append(bucket("seven_day_fable", 95, applies=("claude-fable-5-1",)))
    s["claude"]["buckets"].append(bucket("seven_day_opus", 95, applies=("claude-opus-5",)))
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["ok"] is False
    r2 = pick(cfg, s, "strongest", allow_downgrade=True, now=NOW)
    assert r2["ok"] and r2["model"] == "claude-sonnet-5" and r2["tier"] == "standard"
    assert [c["model"] for c in r2["candidates"]][:4] == [
        "gpt-5.6-sol", "claude-fable-5-1", "claude-opus-5", "gpt-5.6-terra"]


def test_headroom_weighted_prefers_max_headroom_within_tier(cfg, tmp_path: Path):
    c2 = with_policy(cfg, tmp_path, "headroom-weighted")
    s = both_ok(codex_used=50, claude_used=20)
    r = pick(c2, s, "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1" and r["headroom"] == 80
    # ties by pins order; null headroom sorts last
    s2 = both_ok(codex_used=20, claude_used=20)
    assert pick(c2, s2, "strongest", now=NOW)["model"] == "gpt-5.6-sol"
    s3 = both_ok(codex_used=20)
    s3["claude"] = snap("claude", [], status="unknown")
    assert pick(c2, s3, "strongest", now=NOW)["model"] == "gpt-5.6-sol"
    # downgrade only reaches a lower tier when the higher one has no eligible
    s4 = both_ok(codex_used=95, claude_used=95)
    assert pick(c2, s4, "strongest", allow_downgrade=True, now=NOW)["ok"] is False


def test_native_flag_follows_host(cfg):
    r = pick(cfg, both_ok(), "strongest", provider="claude", host="claude", now=NOW)
    assert r["native"] is True and r["host"] == "claude"
    r2 = pick(cfg, both_ok(), "strongest", host="claude", now=NOW)
    assert r2["provider"] == "codex" and r2["native"] is False


def test_pick_is_deterministic_and_pure(cfg):
    s = both_ok(codex_used=50)
    before = repr(s)
    a = pick(cfg, s, "standard", now=NOW)
    b = pick(cfg, s, "standard", now=NOW)
    assert a == b and repr(s) == before


def test_bad_tier_rejected(cfg):
    with pytest.raises(ValueError):
        pick(cfg, both_ok(), "ultra", now=NOW)


def test_detect_host():
    assert detect_host({"CLAUDECODE": "1"}) == "claude"
    assert detect_host({"CLAUDE_CODE_SESSION_ID": "x"}) == "claude"
    assert detect_host({"CODEX_SANDBOX": "1"}) == "codex"
    assert detect_host({"HOME": "/x"}) is None


# --- pacing: surplus against the linear spend line, descending reserve floor -----

def test_pace_settings_loaded_and_policy_validated(cfg, tmp_path: Path):
    assert cfg.settings.policy == "pace"
    assert cfg.settings.pace_slack_pct == 5
    assert with_policy(cfg, tmp_path, "rank-first").settings.policy == "rank-first"
    with pytest.raises(ConfigError):
        with_policy(cfg, tmp_path, "ghost")


def test_candidate_rows_report_surplus_against_linear_line(cfg):
    # used 10 → headroom 90; 16.5 % of the window left → line 16.5 → 73.5 pts ahead
    r = pick(cfg, both_ok(), "strongest", now=NOW)
    sol = r["candidates"][0]
    assert sol["headroom"] == 90
    assert sol["surplus"] == round(90 - LINE_LATE, 1) == 73.5
    assert "73.5 pts ahead of pace" in sol["why"]
    assert r["surplus"] == sol["surplus"]


def test_behind_pace_is_reported_as_negative_surplus(cfg):
    # 82.7 % of the window left → line 82.7; used 40 → headroom 60 → 22.7 pts behind
    r = pick(cfg, both_ok(codex_used=40, resets_in_s=500_000), "strongest", now=NOW)
    sol = next(c for c in r["candidates"] if c["model"] == "gpt-5.6-sol")
    assert sol["surplus"] == round(60 - LINE_EARLY, 1) == -22.7
    assert sol["eligible"] is True and "22.7 pts behind pace" in sol["why"]


def test_reserve_floor_descends_with_the_line_on_a_weekly_bucket(cfg):
    # 90 % used, 3 h before a weekly reset: the line is at 1.8, so the floor is 1.8,
    # not 15 → the last 10 % is spendable (the period can end near 0).
    s = both_ok()
    s["codex"] = snap("codex", [bucket("primary", 90, resets_in_s=3 * 3600)])
    r = pick(cfg, s, "strongest", now=NOW)
    sol = r["candidates"][0]
    assert sol["eligible"] is True and sol["headroom"] == 10
    assert sol["surplus"] == round(10 - 100 * 3 * 3600 / 604_800, 1) == 8.2
    assert r["model"] == "gpt-5.6-sol"


def test_reserve_floor_holds_while_the_line_is_above_reserve(cfg):
    # 90 % used with 82.7 % of the window left: floor = min(15, 82.7) = 15 → ineligible
    s = both_ok(resets_in_s=500_000)
    s["codex"] = snap("codex", [bucket("primary", 90, resets_in_s=500_000)])
    r = pick(cfg, s, "strongest", now=NOW)
    sol = r["candidates"][0]
    assert sol["eligible"] is False and "headroom 10 % < reserve floor 15 %" in sol["why"]
    assert r["model"] == "claude-fable-5-1"


def test_reserve_floor_reason_names_the_descended_floor(cfg):
    # 99 % used, 3 h before a weekly reset: floor 1.8 > headroom 1 → still ineligible
    s = both_ok()
    s["codex"] = snap("codex", [bucket("primary", 99, resets_in_s=3 * 3600)])
    sol = pick(cfg, s, "strongest", now=NOW)["candidates"][0]
    assert sol["eligible"] is False and "headroom 1 % < reserve floor 1.8 %" in sol["why"]


def test_bucket_without_reset_info_has_null_surplus_and_full_reserve(cfg):
    s = both_ok()
    s["codex"] = snap("codex", [{"id": "primary", "used_pct": 10, "resets_at": None,
                                 "window_min": None, "applies_to": ["*"]}])
    r = pick(cfg, s, "strongest", now=NOW)
    sol = r["candidates"][0]
    assert sol["eligible"] is True and sol["headroom"] == 90 and sol["surplus"] is None
    assert "pace" not in sol["why"]
    s["codex"]["buckets"][0]["used_pct"] = 90
    assert pick(cfg, s, "strongest", now=NOW)["candidates"][0]["eligible"] is False


# --- pick: pace policy -------------------------------------------------------------

def test_pace_keeps_pins_order_when_everyone_is_on_pace(cfg):
    r = pick(cfg, both_ok(), "strongest", now=NOW)
    assert r["ok"] and r["model"] == "gpt-5.6-sol"
    assert r["reason"].startswith("pace: ")


def test_pace_diverts_when_top_pin_is_behind_and_another_provider_is_ahead(cfg):
    # codex 22.7 behind, claude 7.3 ahead → fable although sol is pinned first
    r = pick(cfg, both_ok(codex_used=40, claude_used=10, resets_in_s=500_000), "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1" and r["surplus"] == 7.3
    assert "behind pace" in r["candidates"][0]["why"]


def test_pace_picks_least_behind_when_everyone_is_behind(cfg):
    # codex −22.7, claude −12.7 → claude
    r = pick(cfg, both_ok(codex_used=40, claude_used=30, resets_in_s=500_000), "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1" and r["surplus"] == -12.7
    # codex −22.7, claude −32.7 → codex
    r2 = pick(cfg, both_ok(codex_used=40, claude_used=50, resets_in_s=500_000), "strongest", now=NOW)
    assert r2["model"] == "gpt-5.6-sol" and r2["surplus"] == -22.7


def test_pace_slack_keeps_a_slightly_behind_top_pin(cfg):
    # codex 3.7 behind (within slack 5) → still sol; 8.7 behind → divert to claude (+7.3)
    r = pick(cfg, both_ok(codex_used=21, claude_used=10, resets_in_s=500_000), "strongest", now=NOW)
    assert r["model"] == "gpt-5.6-sol" and r["surplus"] == -3.7
    r2 = pick(cfg, both_ok(codex_used=26, claude_used=10, resets_in_s=500_000), "strongest", now=NOW)
    assert r2["model"] == "claude-fable-5-1"


def test_pace_ties_keep_pins_order(cfg):
    r = pick(cfg, both_ok(codex_used=40, claude_used=40, resets_in_s=500_000), "strongest", now=NOW)
    assert r["model"] == "gpt-5.6-sol"


def test_pace_treats_unknown_quota_as_on_pace(cfg):
    # claude unknown (surplus None) and codex behind → claude is the on-pace choice
    s = both_ok(codex_used=40, resets_in_s=500_000)
    s["claude"] = snap("claude", [], status="unknown", source="none")
    r = pick(cfg, s, "strongest", now=NOW)
    assert r["model"] == "claude-fable-5-1" and r["surplus"] is None and "unknown" in r["reason"]


def test_pace_uses_the_worst_bucket_of_a_provider(cfg):
    # claude five_hour ahead but seven_day behind → provider surplus is the seven_day one
    s = both_ok(codex_used=10, claude_used=10)
    s["claude"]["buckets"][1] = bucket("seven_day", 40, resets_in_s=500_000)
    r = pick(cfg, s, "strongest", provider="claude", now=NOW)
    assert r["surplus"] == -22.7


def test_pace_walks_downgrade_ladder_only_when_tier_is_empty(cfg):
    s = both_ok(codex_used=95, claude_used=10)
    s["claude"]["buckets"].append(bucket("seven_day_fable", 95, applies=("claude-fable-5-1",)))
    s["claude"]["buckets"].append(bucket("seven_day_opus", 95, applies=("claude-opus-5",)))
    assert pick(cfg, s, "strongest", now=NOW)["ok"] is False
    r = pick(cfg, s, "strongest", allow_downgrade=True, now=NOW)
    assert r["ok"] and r["model"] == "claude-sonnet-5" and r["tier"] == "standard"
    assert "downgraded" in r["reason"]

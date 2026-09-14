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


def bucket(bid: str, used: float, resets_in_s: int = 100_000, applies=("*",)) -> dict:
    return {"id": bid, "used_pct": used, "resets_at": NOW + resets_in_s,
            "window_min": 10080, "applies_to": list(applies)}


@pytest.fixture
def cfg():
    return load_config()


def both_ok(codex_used: float = 10, claude_used: float = 10) -> dict:
    return {
        "codex": snap("codex", [bucket("primary", codex_used)]),
        "claude": snap("claude", [bucket("five_hour", claude_used), bucket("seven_day", claude_used)]),
    }


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

def test_rank_first_picks_first_pin_in_tier(cfg):
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
    # Same bucket resetting in 3 h is NOT relaxed
    s["codex"] = snap("codex", [bucket("primary", 90, resets_in_s=3 * 3600)])
    assert pick(cfg, s, "strongest", now=NOW)["model"] == "claude-fable-5-1"


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
    text = cfg.path.read_text().replace('policy = "rank-first"', 'policy = "headroom-weighted"')
    p = tmp_path / "m.toml"
    p.write_text(text)
    c2 = load_config(p)
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

"""Quota probes, parsers, cache and rejection markers (contract §Quota
snapshot schemas, §Codex probe, §Claude probe). Live fixtures under
fixtures/ are verbatim app-server responses captured 2026-09-14."""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
from pathlib import Path

import pytest

from ai_router import quota
from ai_router.config import load_config

NOW = 1_789_400_000


@pytest.fixture
def cfg():
    return load_config()


def load(fixtures_dir: Path, name: str) -> dict:
    return json.loads((fixtures_dir / name).read_text())


def fake_rpc(fixtures_dir: Path):
    calls: list[list[str]] = []

    def rpc(methods: list[str]) -> dict:
        calls.append(methods)
        return {"account/rateLimits/read": load(fixtures_dir, "codex-app-server-rate-limits.json")["result"],
                "model/list": load(fixtures_dir, "codex-app-server-model-list.json")["result"]}
    rpc.calls = calls  # runtime attribute on the fake; no type checker runs here
    return rpc


# --- codex normalisation ---------------------------------------------------------------

def test_codex_live_fixture_normalises(cfg, fixtures_dir: Path):
    res = load(fixtures_dir, "codex-app-server-rate-limits.json")["result"]
    buckets = quota.codex_buckets(cfg, res["rateLimits"], res["rateLimitsByLimitId"])
    by_id = {b["id"]: b for b in buckets}
    assert by_id["primary"] == {"id": "primary", "used_pct": 98.0, "resets_at": 1789805383,
                                "window_min": 10080, "applies_to": ["*"]}
    assert "secondary" not in by_id  # null in the live payload
    assert by_id["codex_bengalfox.primary"]["applies_to"] == []
    assert by_id["codex_bengalfox.secondary"]["window_min"] == 10080
    ids = quota.model_ids(load(fixtures_dir, "codex-app-server-model-list.json")["result"])
    assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= set(ids)


def test_rollout_parser_takes_last_token_count_and_both_window_keys(fixtures_dir: Path, cfg):
    hit = quota.rollout_rate_limits(fixtures_dir / "codex-rollout-sample.jsonl.txt")
    assert hit is not None
    rl, ts = hit
    assert rl["primary"]["used_percent"] == 98.0
    assert int(ts) == 1789390396
    buckets = {b["id"]: b for b in quota.codex_buckets(cfg, rl, None)}
    assert buckets["primary"]["window_min"] == 10080 and buckets["primary"]["used_pct"] == 98.0
    assert buckets["secondary"]["window_min"] == 300 and buckets["secondary"]["resets_at"] == 1789400000


# --- codex probe + cache ---------------------------------------------------------------

def test_probe_codex_caches_and_honours_fresh(cfg, state_dir: Path, fixtures_dir: Path):
    rpc = fake_rpc(fixtures_dir)
    s1 = quota.probe_codex(cfg, state_dir, now=NOW, rpc=rpc)
    assert s1["status"] == "ok" and s1["source"] == "app-server" and s1["stale"] is False
    assert s1["plan"] == "pro" and s1["buckets"][0]["used_pct"] == 98.0
    assert "gpt-5.6-luna" in s1["models"]
    assert (state_dir / "quota" / "codex.json").exists()
    assert not (state_dir / "quota" / "codex.json.tmp").exists()
    s2 = quota.probe_codex(cfg, state_dir, now=NOW + 30, rpc=rpc)
    assert len(rpc.calls) == 1 and s2["age_s"] == 30
    quota.probe_codex(cfg, state_dir, now=NOW + 30, rpc=rpc, fresh=True)
    assert len(rpc.calls) == 2
    quota.probe_codex(cfg, state_dir, now=NOW + 200, rpc=rpc)
    assert len(rpc.calls) == 3


def test_probe_codex_failure_keeps_stale_snapshot(cfg, state_dir: Path, fixtures_dir: Path):
    quota.probe_codex(cfg, state_dir, now=NOW, rpc=fake_rpc(fixtures_dir))

    def broken(methods):
        raise RuntimeError("app-server dead")
    s = quota.probe_codex(cfg, state_dir, now=NOW + 2000, rpc=broken)
    assert s["status"] == "ok" and s["stale"] is True and s["age_s"] == 2000
    assert "app-server dead" in s["error"]
    assert s["buckets"][0]["used_pct"] == 98.0


def test_probe_codex_rollout_fallback_then_unknown(cfg, state_dir: Path, fixtures_dir: Path, tmp_path: Path):
    sessions = tmp_path / "sessions" / "2026" / "09" / "14"
    sessions.mkdir(parents=True)
    shutil.copy(fixtures_dir / "codex-rollout-sample.jsonl.txt", sessions / "rollout-2026-09-14T12-50-00-x.jsonl")

    def broken(methods):
        raise RuntimeError("no app-server")
    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=broken, sessions_dir=tmp_path / "sessions")
    assert s["status"] == "ok" and s["source"] == "rollout"
    assert s["buckets"][0]["used_pct"] == 98.0 and s["models"] is None
    assert s["observed_at"] == 1789390396

    other = tmp_path / "state2"
    s2 = quota.probe_codex(cfg, other, now=NOW, rpc=broken, sessions_dir=tmp_path / "nowhere")
    assert s2["status"] == "unknown" and s2["source"] == "none" and s2["buckets"] == []
    assert "no app-server" in s2["error"]
    s3 = quota.probe_codex(cfg, tmp_path / "state3", now=NOW, sessions_dir=tmp_path / "nowhere")
    assert s3["status"] == "unknown" and "binary" in s3["error"]


def test_app_server_call_against_stub(stub_providers, fixtures_dir: Path):
    res = quota.app_server_call(str(stub_providers["codex"]), ["account/rateLimits/read", "model/list"])
    assert res["account/rateLimits/read"]["rateLimits"]["primary"]["usedPercent"] == 98
    assert res["model/list"]["data"][0]["id"] == "gpt-5.6-sol"
    with pytest.raises(RuntimeError):
        quota.app_server_call(str(stub_providers["codex"]), ["no/such/method"])


def test_app_server_call_times_out(stub_providers, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CODEX_APPSERVER", "hang")
    with pytest.raises(RuntimeError, match="no response"):
        quota.app_server_call(str(stub_providers["codex"]), ["account/rateLimits/read"], timeout_s=0.5)


# --- claude ---------------------------------------------------------------

def test_claude_statusline_raw_normalises_with_applies_to(cfg, fixtures_dir: Path, state_dir: Path):
    raw = load(fixtures_dir, "claude-statusline-raw.json")
    (state_dir / "quota").mkdir(parents=True)
    (state_dir / "quota" / "claude.json").write_text(json.dumps(raw))
    s = quota.probe_claude(cfg, state_dir, now=raw["observed_at"] + 100)
    assert s["status"] == "ok" and s["source"] == "statusline" and s["stale"] is False
    by_id = {b["id"]: b for b in s["buckets"]}
    assert by_id["five_hour"]["used_pct"] == 42 and by_id["five_hour"]["applies_to"] == ["*"]
    assert by_id["seven_day_fable"]["applies_to"] == ["claude-fable-5-1"]
    assert by_id["seven_day"]["resets_at"] == 1789800000 and by_id["seven_day"]["window_min"] == 10080


def test_claude_family_fallback_and_extra_usage(cfg):
    buckets = {b["id"]: b for b in quota.claude_buckets(cfg, {
        "seven_day_haiku": {"utilization": 1, "resets_at": None},
        "extra_usage": {"utilization": 3}, "weird": {"percent": 9}})}
    assert buckets["seven_day_haiku"]["applies_to"] == ["claude-haiku-4-5-20251001"]
    assert buckets["extra_usage"]["applies_to"] == []
    assert buckets["weird"]["applies_to"] == ["*"]


def make_urlopen(payload, status: int | None = None):
    calls: list = []

    def urlopen(req, timeout=None):
        calls.append(req)
        if status is not None:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b""))

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode()
        return Resp()
    urlopen.calls = calls  # runtime attribute on the fake; no type checker runs here
    return urlopen


def creds(tmp_path: Path) -> Path:
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-test"}}))
    return p


def test_claude_oauth_fallback_when_stale(cfg, state_dir: Path, fixtures_dir: Path, tmp_path: Path):
    u = make_urlopen(load(fixtures_dir, "claude-oauth-usage.json"))
    s = quota.probe_claude(cfg, state_dir, now=NOW, urlopen=u, credentials_path=creds(tmp_path))
    assert s["status"] == "ok" and s["source"] == "oauth" and s["stale"] is False
    req = u.calls[0]
    assert req.full_url == quota.OAUTH_URL
    assert req.get_header("Authorization") == "Bearer sk-ant-oat01-test"
    assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert req.get_header("User-agent").startswith("claude-code/")
    by_id = {b["id"]: b for b in s["buckets"]}
    assert by_id["five_hour"]["used_pct"] == 12.5 and by_id["five_hour"]["resets_at"] == 1789398000
    assert by_id["seven_day_opus"]["applies_to"] == ["claude-opus-5"]
    assert by_id["extra_usage"]["applies_to"] == [] and by_id["extra_usage"]["resets_at"] is None
    assert by_id["seven_day_fable"]["used_pct"] == 70 and by_id["seven_day_fable"]["applies_to"] == ["claude-fable-5-1"]
    assert by_id["seven_day_sonnet"]["used_pct"] == 33
    assert "seven_day_oauth_apps" not in by_id
    written = json.loads((state_dir / "quota" / "claude.json").read_text())
    assert written["source"] == "oauth" and written["observed_at"] == NOW
    # fresh snapshot now → no second call
    quota.probe_claude(cfg, state_dir, now=NOW + 10, urlopen=u, credentials_path=creds(tmp_path))
    assert len(u.calls) == 1
    # --fresh forces a call even when fresh
    quota.probe_claude(cfg, state_dir, now=NOW + 10, fresh=True, urlopen=u, credentials_path=creds(tmp_path))
    assert len(u.calls) == 2


def test_claude_oauth_429_writes_backoff_and_skips(cfg, state_dir: Path, tmp_path: Path):
    u = make_urlopen(None, status=429)
    s = quota.probe_claude(cfg, state_dir, now=NOW, urlopen=u, credentials_path=creds(tmp_path))
    assert s["status"] == "unknown" and "429" in s["error"]
    marker = state_dir / "quota" / "claude.oauth-backoff"
    assert int(marker.read_text()) == NOW + 900
    quota.probe_claude(cfg, state_dir, now=NOW + 100, urlopen=u, credentials_path=creds(tmp_path))
    assert len(u.calls) == 1
    quota.probe_claude(cfg, state_dir, now=NOW + 1000, urlopen=u, credentials_path=creds(tmp_path))
    assert len(u.calls) == 2


def test_claude_oauth_not_called_without_creds_or_when_disabled(cfg, state_dir: Path, tmp_path: Path):
    u = make_urlopen({})
    s = quota.probe_claude(cfg, state_dir, now=NOW, urlopen=u, credentials_path=tmp_path / "none.json")
    assert s["status"] == "unknown" and not u.calls and "credentials" in s["error"]
    text = cfg.path.read_text().replace("claude_oauth_fallback = true", "claude_oauth_fallback = false")
    p = tmp_path / "m.toml"
    p.write_text(text)
    s2 = quota.probe_claude(load_config(p), state_dir, now=NOW, urlopen=u, credentials_path=creds(tmp_path))
    assert s2["status"] == "unknown" and not u.calls


def test_claude_oauth_failure_keeps_stale_statusline(cfg, state_dir: Path, fixtures_dir: Path, tmp_path: Path):
    raw = load(fixtures_dir, "claude-statusline-raw.json")
    (state_dir / "quota").mkdir(parents=True)
    (state_dir / "quota" / "claude.json").write_text(json.dumps(raw))

    def boom(req, timeout=None):
        raise OSError("egress denied")
    s = quota.probe_claude(cfg, state_dir, now=raw["observed_at"] + 5000, urlopen=boom,
                           credentials_path=creds(tmp_path))
    assert s["status"] == "ok" and s["stale"] is True and s["source"] == "statusline"
    assert "egress denied" in s["error"]


# --- rejection ---------------------------------------------------------------

def test_rejection_marker_lifecycle(cfg, state_dir: Path):
    assert quota.rejection(cfg, state_dir, "codex", NOW) is None
    quota.mark_rejected(state_dir, "codex", until=NOW + 100, reason="x" * 500, source="job", now=NOW)
    r = quota.rejection(cfg, state_dir, "codex", NOW + 50)
    assert r["until"] == NOW + 100 and len(r["reason"]) == 300 and r["source"] == "job"
    assert quota.rejection(cfg, state_dir, "codex", NOW + 100) is None
    quota.mark_rejected(state_dir, "claude", until=None, reason="r", source="stop-failure", now=NOW)
    assert quota.rejection(cfg, state_dir, "claude", NOW + 3599)["until"] == NOW + 3600
    assert quota.rejection(cfg, state_dir, "claude", NOW + 3600) is None


def test_rejected_status_overlays_snapshot(cfg, state_dir: Path, fixtures_dir: Path):
    quota.mark_rejected(state_dir, "codex", until=NOW + 100, reason="usage limit", source="job", now=NOW)
    s = quota.probe_codex(cfg, state_dir, now=NOW, rpc=fake_rpc(fixtures_dir))
    assert s["status"] == "rejected" and s["rejected_until"] == NOW + 100 and s["reason"] == "usage limit"
    assert s["buckets"]  # data still present
    both = quota.all_snapshots(cfg, state_dir, now=NOW, rpc=fake_rpc(fixtures_dir),
                               urlopen=make_urlopen({}), )
    assert set(both) == {"codex", "claude"}
    assert quota.all_snapshots(cfg, state_dir, now=NOW, provider="codex", rpc=fake_rpc(fixtures_dir)).keys() == {"codex"}


def test_blocking_reset_picks_fullest_applicable_bucket():
    snap = {"buckets": [{"id": "a", "used_pct": 50, "resets_at": 1, "applies_to": ["*"]},
                        {"id": "b", "used_pct": 99, "resets_at": 2, "applies_to": ["claude-fable-5-1"]},
                        {"id": "c", "used_pct": 100, "resets_at": 3, "applies_to": []}]}
    assert quota.blocking_reset(snap, "claude-fable-5-1", "fable") == 2
    assert quota.blocking_reset(snap, "claude-opus-5", "opus") == 1
    assert quota.blocking_reset({"buckets": []}, "x", "y") is None

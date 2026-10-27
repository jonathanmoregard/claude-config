"""Tests for tools/ai-router/update_ranking.py (build contract §RANK).

Run from the worktree root (pytest is nix-provided, not in the base python3):

    nix shell nixpkgs#python3Packages.pytest -c pytest -q tests/ai-router/test_update_ranking.py

Every run is offline: `--offline-fixture` payloads under fixtures/ranking/, and
the network / app-server paths get injected fakes (`urlopen`, `spawn`).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import sys
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

# conftest.py beside this file already inserts the tool on sys.path; this
# insert keeps the module runnable standalone when that file is absent. The
# tool lives under runtime/, which is projected into ~/.claude, and this suite
# does not, so it reaches across rather than up.
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "runtime" / "tools" / "ai-router"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import update_ranking as ur  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ranking"
ALL_IDS = [
    "gpt-5.6-sol", "claude-fable-5-1", "claude-opus-5", "gpt-5.6-terra",
    "claude-sonnet-5", "gpt-5.6-luna", "claude-haiku-4-5-20251001",
]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a default path escape into the real home or state dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AI_ROUTER_STATE", str(tmp_path / "state"))


@pytest.fixture
def rank_state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def pending(tmp_path: Path) -> Path:
    return tmp_path / "notes" / "pending_for_human.md"


def fixture_copy(tmp_path: Path, name: str = "fx") -> Path:
    dst = tmp_path / name
    shutil.copytree(FIXTURES, dst)
    return dst


def lmarena_page(rows: list[dict], total: int | None = None) -> dict:
    return {
        "features": [],
        "rows": [{"row_idx": i, "row": r, "truncated_cells": []} for i, r in enumerate(rows)],
        "num_rows_total": len(rows) if total is None else total,
        "num_rows_per_page": 100,
        "partial": False,
    }


def text_row(name: str, org: str, rank: int, score: float, category: str = "overall") -> dict:
    return {"model_name": name, "organization": org, "license": "Proprietary", "score": score,
            "score_ci_lower": score - 0.01, "score_ci_upper": score + 0.01, "votes": 1000,
            "rank": rank, "category": category, "date": "2026-09-10"}


def run_offline(fixture_dir: Path, pending: Path, extra: list[str] | None = None) -> int:
    argv = ["--models-toml", str(fixture_dir / "models.toml"), "--offline-fixture", str(fixture_dir),
            "--pending", str(pending)] + (extra or [])
    return ur.main(argv)


def read_out(rank_state: Path) -> dict:
    return json.loads((rank_state / "ranking.auto.json").read_text())


# --- offline end-to-end -----------------------------------------------------

def test_offline_run_writes_ranking_auto_json_with_documented_keys(tmp_path, rank_state, pending):
    assert run_offline(FIXTURES, pending) == 0
    out = read_out(rank_state)
    assert set(out) >= {"generated_at", "sources", "models", "pins_order", "evidence_order",
                        "disagreements", "unknown"}
    assert out["pins_order"] == ALL_IDS
    assert set(out["models"]) == set(ALL_IDS)
    for m in out["models"].values():
        assert set(m) >= {"lmarena_text_rank", "lmarena_text_score", "lmarena_webdev_rank",
                          "epoch", "matched_names", "available"}
    assert [s["name"] for s in out["sources"]] == ["lmarena_text", "lmarena_webdev", "epoch",
                                                    "codex_model_list"]
    assert all(s["ok"] and s["error"] is None for s in out["sources"])
    assert all(set(s) >= {"name", "url", "fetched_at", "ok", "error"} for s in out["sources"])
    assert stat.S_IMODE(rank_state.stat().st_mode) == 0o700
    assert not list(rank_state.glob("*.tmp"))


def test_json_flag_prints_the_ranking_object(rank_state, pending, capsys):
    assert run_offline(FIXTURES, pending, ["--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["pins_order"] == ALL_IDS
    assert printed["evidence_order"] == read_out(rank_state)["evidence_order"]


def test_missing_models_toml_is_a_usage_error(tmp_path, pending, capsys):
    rc = ur.main(["--models-toml", str(tmp_path / "nope.toml"), "--offline-fixture", str(FIXTURES),
                  "--pending", str(pending)])
    assert rc == 2
    assert "nope.toml" in capsys.readouterr().err


# --- name reconciliation ---------------------------------------------------

def test_match_name_by_id_alias_family_and_rejections():
    cat = ur.load_models(FIXTURES / "models.toml")

    def kind(name: str, org: str | None = None):
        m = ur.match_name(name, cat, organization=org)
        return (m.model_id, m.kind) if m else None

    assert kind("claude-opus-5_max", "Anthropic") == ("claude-opus-5", "id")
    assert kind("Claude Opus 5 (High)") == ("claude-opus-5", "id")
    assert kind("GPT-5.6", "OpenAI") == ("gpt-5.6-sol", "alias")          # routing alias
    assert kind("Claude Fable 5 (High)") == ("claude-fable-5-1", "family")  # same generation
    assert kind("Claude Fable 5.1 (High)") == ("claude-fable-5-1", "id")
    assert kind("GPT 5.6 Terra (High)") == ("gpt-5.6-terra", "id")        # not sol via "gpt-5.6"
    assert kind("Claude Haiku 4.5") == ("claude-haiku-4-5-20251001", "family")
    assert kind("Kimi K3 (Max)", "Moonshot") is None                       # no token at all
    assert kind("Claude Opus 4.8 (High)") is None                          # cross-generation
    assert kind("claude-opus-4-6-thinking") is None
    assert kind("GPT 5.5 (High)") is None
    assert kind("Sol Invictus 7B", "SomeLab") is None                      # vendor guard


def test_unknown_names_are_listed_never_guessed(rank_state, pending):
    run_offline(FIXTURES, pending)
    out = read_out(rank_state)
    unknown = {(u["source"], u["name"]) for u in out["unknown"]}
    assert ("lmarena_text", "Kimi K3 (Max)") in unknown
    assert ("lmarena_text", "Claude Opus 4.8 (High)") in unknown
    assert ("lmarena_text", "GPT 5.5 (High)") in unknown
    assert ("epoch", "deepseek-v4") in unknown
    assert ("epoch", "claude-opus-4-6-thinking") in unknown
    matched = {n for m in out["models"].values() for n in m["matched_names"]}
    assert not matched & {u["name"] for u in out["unknown"]}


# --- evidence ---------------------------------------------------------------

def test_evidence_order_from_lmarena_text_rank(rank_state, pending):
    run_offline(FIXTURES, pending)
    out = read_out(rank_state)
    assert out["evidence_order"] == ["claude-opus-5", "claude-fable-5-1", "gpt-5.6-sol",
                                     "gpt-5.6-terra", "claude-sonnet-5", "gpt-5.6-luna",
                                     "claude-haiku-4-5-20251001"]
    opus = out["models"]["claude-opus-5"]
    assert opus["lmarena_text_rank"] == 3 and opus["lmarena_text_score"] == pytest.approx(0.110647)
    assert {"Claude Opus 5 (High)", "Claude Opus 5 (Max)"} <= set(opus["matched_names"])
    assert opus["lmarena_webdev_rank"] == 2
    sol = out["models"]["gpt-5.6-sol"]
    assert sol["lmarena_text_rank"] == 7        # the category=coding row (rank 1) is ignored
    fable = out["models"]["claude-fable-5-1"]
    assert fable["lmarena_text_rank"] == 5 and fable["lmarena_webdev_rank"] == 1
    assert fable["weakest_match"] == "family"   # text evidence is Fable 5, not 5.1
    assert fable["epoch"]["epoch_capabilities_index"] == pytest.approx(155.1)
    assert opus["epoch"]["gpqa_diamond"] == pytest.approx(0.912)


def test_disagreement_sol_pinned_first_but_fable_leads(tmp_path, rank_state, pending):
    fx = fixture_copy(tmp_path)
    (fx / "lmarena_text.json").write_text(json.dumps(lmarena_page([
        text_row("Claude Fable 5.1 (High)", "Anthropic", 1, 0.13),
        text_row("GPT 5.6 Sol (xHigh)", "OpenAI", 2, 0.12),
    ])))
    assert run_offline(fx, pending) == 0
    out = read_out(rank_state)
    assert out["evidence_order"] == ["claude-fable-5-1", "gpt-5.6-sol"]
    assert len(out["disagreements"]) == 1
    d = out["disagreements"][0]
    assert d["model"] == "gpt-5.6-sol" and d["pin_rank"] == 1 and d["evidence_rank"] == 2
    assert "claude-fable-5-1" in d["note"]


def test_no_disagreement_when_evidence_matches_pins(tmp_path, rank_state, pending):
    fx = fixture_copy(tmp_path)
    (fx / "lmarena_text.json").write_text(json.dumps(lmarena_page([
        text_row("GPT 5.6 Sol (xHigh)", "OpenAI", 1, 0.13),
        text_row("Claude Fable 5.1 (High)", "Anthropic", 2, 0.12),
        text_row("Claude Opus 5 (High)", "Anthropic", 3, 0.11),
    ])))
    assert run_offline(fx, pending) == 0
    out = read_out(rank_state)
    assert out["disagreements"] == []
    assert not (rank_state / "ranking.proposal.md").exists()
    assert not pending.exists()


# --- proposal + pending ------------------------------------------------------

def test_proposal_and_pending_written_once_not_duplicated(tmp_path, rank_state, pending):
    pending.parent.mkdir(parents=True)
    pending.write_text("# Pending for human — `~/.claude`\n\n## 2026-09-01: older entry\n"
                       "**Why blocked**: x\n**Steps for human**:\n1. y\n**Everything else done**: z\n")
    assert run_offline(FIXTURES, pending) == 0
    proposal = rank_state / "ranking.proposal.md"
    assert proposal.exists()
    body = proposal.read_text()
    assert "gpt-5.6-sol" in body and "never" in body.lower()
    text = pending.read_text()
    entries = re.findall(r"^## \d{4}-\d{2}-\d{2}: .*$", text, flags=re.M)
    assert len(entries) == 2 and "older entry" in entries[1]       # newest at the top
    assert text.startswith("# Pending for human")
    new_entry = text.split("## 2026-09-01")[0]
    assert "**Why blocked**:" in new_entry and "**Steps for human**:" in new_entry
    assert "**Everything else done**:" in new_entry
    assert str(proposal) in new_entry and str(FIXTURES / "models.toml") in new_entry
    assert not list(rank_state.glob("*.tmp"))

    # identical second run: nothing appended, proposal reported unchanged
    assert run_offline(FIXTURES, pending) == 0
    assert pending.read_text() == text
    assert read_out(rank_state)["proposal"]["changed"] is False

    # evidence changes → a new entry
    fx = fixture_copy(tmp_path)
    (fx / "lmarena_text.json").write_text(json.dumps(lmarena_page([
        text_row("Claude Opus 5 (High)", "Anthropic", 1, 0.13),
        text_row("GPT 5.6 Sol (xHigh)", "OpenAI", 2, 0.12),
    ])))
    assert run_offline(fx, pending) == 0
    assert len(re.findall(r"^## \d{4}-\d{2}-\d{2}: ", pending.read_text(), flags=re.M)) == 3
    assert read_out(rank_state)["proposal"]["changed"] is True


def test_pending_created_when_absent_and_hash_persisted(rank_state, pending):
    assert run_offline(FIXTURES, pending) == 0
    assert pending.exists()
    assert (rank_state / "ranking.proposal.sha256").read_text().strip()
    out = read_out(rank_state)
    assert out["proposal"]["written"] is True and out["proposal"]["pending_appended"] is True


# --- source failures --------------------------------------------------------

def test_missing_fixture_source_is_ok_false_and_run_still_exits_zero(tmp_path, rank_state, pending):
    fx = fixture_copy(tmp_path)
    (fx / "epoch.csv").unlink()
    assert run_offline(fx, pending) == 0
    out = read_out(rank_state)
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["epoch"]["ok"] is False and "epoch.csv" in by_name["epoch"]["error"]
    assert by_name["lmarena_text"]["ok"] is True
    assert out["models"]["claude-opus-5"]["epoch"] == {}
    assert out["evidence_order"]                    # the other sources still contribute


def test_all_sources_missing_still_writes_output(tmp_path, rank_state, pending):
    fx = tmp_path / "empty"
    fx.mkdir()
    assert ur.main(["--models-toml", str(FIXTURES / "models.toml"), "--offline-fixture", str(fx),
                    "--pending", str(pending)]) == 0
    out = read_out(rank_state)
    assert all(s["ok"] is False for s in out["sources"])
    assert out["evidence_order"] == [] and out["disagreements"] == []


# --- availability -----------------------------------------------------------

def test_availability_applied_from_model_list(tmp_path, rank_state, pending):
    run_offline(FIXTURES, pending)
    out = read_out(rank_state)
    assert out["models"]["gpt-5.6-sol"]["available"] is True
    assert out["models"]["claude-opus-5"]["available"] is None   # model/list is Codex-only

    fx = fixture_copy(tmp_path)
    ml = json.loads((fx / "model_list.json").read_text())
    ml["data"] = [m for m in ml["data"] if m["id"] != "gpt-5.6-sol"]
    (fx / "model_list.json").write_text(json.dumps(ml))
    run_offline(fx, pending)
    out = read_out(rank_state)
    assert out["models"]["gpt-5.6-sol"]["available"] is False
    assert out["models"]["gpt-5.6-terra"]["available"] is True
    assert out["models"]["gpt-5.6-luna"]["available"] is True


def test_extract_model_ids_accepts_envelope_result_and_bare_list():
    data = {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}]}
    assert ur.extract_model_ids(data) == ["gpt-5.6-sol", "gpt-5.5"]
    assert ur.extract_model_ids({"id": 2, "result": data}) == ["gpt-5.6-sol", "gpt-5.5"]
    assert ur.extract_model_ids(["a", {"model": "b"}]) == ["a", "b"]


# --- network path (fakes only) ----------------------------------------------

class FakeHTTP:
    def __init__(self, routes: dict[str, bytes | Exception]):
        self.routes = routes
        self.requests: list[tuple[str, float | None]] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append((url, timeout))
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return io.BytesIO(payload)
        raise urllib.error.URLError(f"no fake route for {url}")


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_fetch_lmarena_rows_paginates_with_fake_urlopen(monkeypatch):
    monkeypatch.setattr(ur, "LMARENA_PAGE", 2)
    rows = [text_row(f"Model {i}", "X", i + 1, 0.1 - i * 0.01) for i in range(3)]

    def route(req, timeout=None):
        q = _query(req.full_url)
        assert q["dataset"] == "lmarena-ai/leaderboard-dataset"
        assert q["config"] == "text" and q["split"] == "latest" and q["length"] == "2"
        off = int(q["offset"])
        return io.BytesIO(json.dumps(lmarena_page(rows[off:off + 2], total=3)).encode())

    got = ur.fetch_lmarena_rows("text", timeout_s=7, urlopen=route)
    assert [r["model_name"] for r in got] == ["Model 0", "Model 1", "Model 2"]


def test_run_network_path_end_to_end_with_fakes(tmp_path, rank_state, pending, monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_router.quota", None)  # force the local helper + fake spawn
    text = lmarena_page([text_row("Claude Opus 5 (High)", "Anthropic", 1, 0.12),
                         text_row("GPT 5.6 Sol (xHigh)", "OpenAI", 2, 0.11)])
    webdev = lmarena_page([text_row("GPT 5.6 Sol (xHigh)", "OpenAI", 1, 0.2)])
    csv_text = "model,benchmark,score\nclaude-opus-5_max,epoch_capabilities_index,150\n"
    http = FakeHTTP({
        ur.LMARENA_ROWS_API + "?" + urllib.parse.urlencode({"dataset": ur.LMARENA_DATASET, "config": "text"}):
            json.dumps(text).encode(),
        ur.LMARENA_ROWS_API + "?" + urllib.parse.urlencode({"dataset": ur.LMARENA_DATASET, "config": "webdev"}):
            json.dumps(webdev).encode(),
        ur.EPOCH_CSV_URL: csv_text.encode(),
    })
    spawn = FakeSpawn([{"id": 1, "result": {}}, {"id": 2, "result": {"data": [{"id": "gpt-5.6-sol"}]}}])
    opts = ur.parse_args(["--models-toml", str(FIXTURES / "models.toml"), "--pending", str(pending),
                          "--timeout-s", "9"])
    out = ur.run(opts, urlopen=http, spawn=spawn)
    assert all(s["ok"] for s in out["sources"]), out["sources"]
    assert {u for (u, _) in http.requests} == {r for r in http.routes} or len(http.requests) == 3
    assert all(t == 9 for (_, t) in http.requests)
    assert out["evidence_order"] == ["claude-opus-5", "gpt-5.6-sol"]
    assert out["models"]["claude-opus-5"]["epoch"] == {"epoch_capabilities_index": 150.0}
    assert out["models"]["gpt-5.6-sol"]["available"] is True
    assert out["models"]["gpt-5.6-terra"]["available"] is False
    assert (rank_state / "ranking.auto.json").exists()
    urls = {s["name"]: s["url"] for s in out["sources"]}
    assert urls["lmarena_text"].startswith(ur.LMARENA_ROWS_API) and urls["epoch"] == ur.EPOCH_CSV_URL


def test_network_source_failure_is_recorded_and_run_continues(tmp_path, rank_state, pending, monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_router.quota", None)
    http = FakeHTTP({ur.LMARENA_ROWS_API: json.dumps(lmarena_page([])).encode(),
                     ur.EPOCH_CSV_URL: urllib.error.HTTPError(ur.EPOCH_CSV_URL, 404, "Not Found", {}, None)})
    spawn = FakeSpawn([{"id": 1, "result": {}}, {"id": 2, "error": {"code": -1, "message": "nope"}}])
    opts = ur.parse_args(["--models-toml", str(FIXTURES / "models.toml"), "--pending", str(pending)])
    out = ur.run(opts, urlopen=http, spawn=spawn)
    by_name = {s["name"]: s for s in out["sources"]}
    assert by_name["epoch"]["ok"] is False and "404" in by_name["epoch"]["error"]
    assert by_name["codex_model_list"]["ok"] is False and "nope" in by_name["codex_model_list"]["error"]
    assert by_name["lmarena_text"]["ok"] is True
    assert out["models"]["gpt-5.6-sol"]["available"] is None
    assert (rank_state / "ranking.auto.json").exists()


def test_parse_epoch_csv_long_and_wide_formats():
    long = "model,organization,benchmark,score\nclaude-opus-5_max,Anthropic,gpqa_diamond,0.9\nx,Y,gpqa_diamond,n/a\n"
    rows = ur.parse_epoch_csv(long)
    assert [(r.model, r.benchmark, r.score) for r in rows] == [("claude-opus-5_max", "gpqa_diamond", 0.9)]
    wide = "Model,Organization,epoch_capabilities_index,gpqa_diamond,notes\ngpt-5.6-sol_xhigh,OpenAI,151,0.905,hi\n"
    rows = ur.parse_epoch_csv(wide)
    assert sorted((r.benchmark, r.score) for r in rows) == [("epoch_capabilities_index", 151.0), ("gpqa_diamond", 0.905)]
    assert all(r.model == "gpt-5.6-sol_xhigh" and r.organization == "OpenAI" for r in rows)
    with pytest.raises(ValueError):
        ur.parse_epoch_csv("foo,bar\n1,2\n")


# --- codex app-server helper ------------------------------------------------

class FakeSpawn:
    """Stands in for subprocess.Popen: scripted JSON-RPC replies, records stdin."""

    def __init__(self, replies: list[dict]):
        self.replies = replies
        self.calls: list[list[str]] = []
        self.stdin = io.StringIO()
        self.killed = False

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        lines = [json.dumps({"method": "remoteControl/status/changed", "params": {}})]
        lines += [json.dumps(r) for r in self.replies]
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        return self

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


def test_codex_model_list_helper_handshake_with_fake_spawn():
    spawn = FakeSpawn([{"id": 1, "result": {"userAgent": "x"}},
                       {"id": 2, "result": {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}]}}])
    ids = ur.codex_model_list(timeout_s=3, spawn=spawn, codex_bin="/bin/codex")
    assert ids == ["gpt-5.6-sol", "gpt-5.5"]
    assert spawn.calls == [["/bin/codex", "app-server"]]
    sent = [json.loads(l) for l in spawn.stdin.getvalue().splitlines()]
    assert [m.get("method") for m in sent] == ["initialize", "initialized", "model/list"]
    assert sent[0]["params"]["clientInfo"]["name"] == "ai-router" and sent[0]["id"] == 1
    assert sent[2]["id"] == 2 and "id" not in sent[1]
    assert spawn.killed


def test_codex_model_list_helper_reports_rpc_error_and_eof():
    with pytest.raises(RuntimeError, match="nope"):
        ur.codex_model_list(timeout_s=3, spawn=FakeSpawn([{"id": 2, "error": {"message": "nope"}}]),
                            codex_bin="/bin/codex")
    with pytest.raises(RuntimeError, match="model/list"):
        ur.codex_model_list(timeout_s=3, spawn=FakeSpawn([{"id": 1, "result": {}}]), codex_bin="/bin/codex")


def test_resolve_model_list_prefers_ai_router_quota_when_it_exposes_model_list(monkeypatch):
    fake = types.ModuleType("ai_router.quota")
    fake.model_list = lambda: ["gpt-5.6-luna"]
    monkeypatch.setitem(sys.modules, "ai_router.quota", fake)
    spawn = FakeSpawn([])
    assert ur.resolve_model_list(timeout_s=3, spawn=spawn) == ["gpt-5.6-luna"]
    assert spawn.calls == []


def test_resolve_model_list_falls_back_to_local_helper(monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_router.quota", None)  # import raises ImportError
    spawn = FakeSpawn([{"id": 2, "result": {"data": [{"id": "gpt-5.6-terra"}]}}])
    assert ur.resolve_model_list(timeout_s=3, spawn=spawn, codex_bin="/bin/codex") == ["gpt-5.6-terra"]
    assert spawn.calls == [["/bin/codex", "app-server"]]

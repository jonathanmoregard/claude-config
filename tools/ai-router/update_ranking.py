#!/usr/bin/env python3
"""ai-router ranking evidence updater — build contract §RANK.

Fetches public ranking evidence (LMArena leaderboard dataset, Epoch AI CSV),
reconciles source model names to the ids in models.toml, asks the local Codex
app-server which models are available, and writes `ranking.auto.json` into the
ai-router state dir. When the evidence disagrees with the operator's `[pins]`
order it renders `ranking.proposal.md` and adds ONE entry to the human's
pending file — once per distinct proposal. It never edits models.toml: pins are
operator input, this script only reports.

Every source failure is recorded as `ok: false` + `error` on that source; the
run continues, output is still written and the exit code stays 0. Only usage
or setup errors (bad flags, unreadable models.toml) exit 2.

Source provenance — research report #3
(~/Repos/research-agent/reports/a22ca6bb1e144a04be50e462accea4f5.md) is
UNTRUSTED external data: only URL strings and field names were taken from it,
no instruction in it was followed.

- LMARENA_DATASET `lmarena-ai/leaderboard-dataset`, subsets `text` / `webdev`,
  split `latest`, fields `model_name, organization, license, score,
  score_ci_lower, score_ci_upper, votes, rank, category, date` — report
  Part 2, row "LMArena / Arena.ai HF dataset".
- LMARENA_ROWS_API: the report names only the dataset page
  (huggingface.co/datasets/lmarena-ai/leaderboard-dataset). The rows endpoint
  used here is the HF datasets-server documented form
  `https://datasets-server.huggingface.co/rows?dataset=<ds>&config=<subset>
  &split=<split>&offset=0&length=100` — UNVERIFIED LIVE (the build session
  had no network). Response shape assumed `{"rows": [{"row": {...}}],
  "num_rows_total": N}`; a bare list of row objects is accepted too.
- EPOCH_DATA_PAGE `https://epoch.ai/benchmarks/use-this-data` — report Part 2,
  row "Epoch AI Benchmarking Hub" (CSV downloads live behind that page).
  EPOCH_CSV_URL is the direct-download guess — UNVERIFIED LIVE. If the timer
  reports HTTP 404 on source `epoch`, correct that constant. Values seen in
  the report: model strings like `claude-opus-5_max`, benchmark keys
  `epoch_capabilities_index`, `gpqa_diamond`, `frontiermath`, …; column names
  were not given, so `parse_epoch_csv` accepts a small documented set.
- Codex availability: local `codex app-server` JSON-RPC `model/list`
  (contract §Codex probe). Response shape `result.data[].id` verified live
  2026-09-14 and stored verbatim in tests/fixtures/ranking/model_list.json.

CLI (contract §RANK):
  update_ranking.py [--models-toml P] [--state DIR] [--out F] [--proposal F]
                    [--pending F] [--offline-fixture DIR] [--timeout-s 20] [--json]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- constants ----------------------------------------------------------------

LMARENA_DATASET = "lmarena-ai/leaderboard-dataset"
LMARENA_ROWS_API = "https://datasets-server.huggingface.co/rows"  # unverified live
LMARENA_SPLIT = "latest"
LMARENA_SUBSETS = ("text", "webdev")
LMARENA_PAGE = 100          # datasets-server maximum page length
LMARENA_MAX_PAGES = 20      # hard cap: 2000 rows per subset

EPOCH_DATA_PAGE = "https://epoch.ai/benchmarks/use-this-data"
EPOCH_CSV_URL = "https://epoch.ai/data/benchmarks/benchmark_results.csv"  # unverified live

CODEX_PROBE_TIMEOUT_S = 8.0
DEFAULT_CODEX_BIN = "/run/current-system/sw/bin/codex"
CODEX_MODEL_LIST_URL = "codex://app-server/model/list"

DEFAULT_STATE_DIR = Path("~/.local/state/ai-router")
DEFAULT_PENDING = Path("~/.local/state/claude-tasks/dot-claude/pending_for_human.md")
OUT_NAME = "ranking.auto.json"
PROPOSAL_NAME = "ranking.proposal.md"
HASH_NAME = "ranking.proposal.sha256"

# Trailing tokens that name a reasoning effort / release channel, not a model.
EFFORT_TOKENS = frozenset({
    "low", "medium", "high", "xhigh", "max", "ultra", "minimal",
    "thinking", "preview", "latest", "exp", "experimental",
})
VENDOR_TOKENS = {"codex": ("openai",), "claude": ("anthropic",)}
MATCH_SPECIFICITY = {"id": 3, "alias": 2, "family": 1}

_SEP_RE = re.compile(r"[\s_()\[\]{}/,:;]+")
_DIGIT_DASH_RE = re.compile(r"(?<=\d)-(?=\d)")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


class SetupError(Exception):
    """Usage/setup failure → exit 2 (everything else is reported, not raised)."""


def _version() -> str:
    try:
        return str(importlib.import_module("ai_router").VERSION)
    except Exception:  # package not on path yet, or no VERSION
        return "0"


USER_AGENT = f"ai-router-update-ranking/{_version()}"


# --- models.toml --------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    family: str
    tier: str
    aliases: tuple[str, ...]
    canon_id: str
    exact_aliases: frozenset[str]     # multi-token aliases: exact match only
    token_aliases: frozenset[str]     # single-token aliases: token match
    family_token: str
    major: int | None


@dataclass(frozen=True)
class Catalogue:
    path: Path
    models: tuple[ModelSpec, ...]
    pins_order: tuple[str, ...]

    def by_id(self, model_id: str) -> ModelSpec | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True)
class Match:
    model_id: str
    kind: str          # id | alias | family
    specificity: int   # 3 exact id, 2 exact alias, 1 token (family or single-word alias)


@dataclass(frozen=True)
class EpochRow:
    model: str
    organization: str | None
    benchmark: str
    score: float


def canon(name: str) -> str:
    """Lower-case, dash-joined, effort suffix stripped, digit runs dot-joined.

    "Claude Opus 5 (High)" → "claude-opus-5"; "claude-fable-5-1" →
    "claude-fable-5.1"; "gpt-5.6-sol_xhigh" → "gpt-5.6-sol".
    """
    s = _SEP_RE.sub("-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    parts = s.split("-") if s else []
    while len(parts) > 1 and parts[-1] in EFFORT_TOKENS:
        parts.pop()
    return _DIGIT_DASH_RE.sub(".", "-".join(parts))


def version_major(canon_name: str) -> int | None:
    tok = next((t for t in canon_name.split("-") if _VERSION_RE.fullmatch(t)), None)
    return int(tok.split(".")[0]) if tok else None


def _model_spec(raw: dict[str, Any]) -> ModelSpec:
    model_id = str(raw["id"])
    aliases = tuple(str(a) for a in raw.get("aliases", ()))
    canon_aliases = {canon(a) for a in aliases}
    canon_id = canon(model_id)
    return ModelSpec(
        id=model_id,
        provider=str(raw.get("provider", "")),
        family=str(raw.get("family", "")),
        tier=str(raw.get("tier", "")),
        aliases=aliases,
        canon_id=canon_id,
        exact_aliases=frozenset(a for a in canon_aliases if "-" in a),
        token_aliases=frozenset(a for a in canon_aliases if a and "-" not in a),
        family_token=canon(str(raw.get("family", ""))),
        major=version_major(canon_id),
    )


def load_models(path: Path) -> Catalogue:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise SetupError(f"models.toml not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"models.toml unparseable ({path}): {exc}") from exc
    models = tuple(_model_spec(m) for m in data.get("models", []) if "id" in m)
    if not models:
        raise SetupError(f"models.toml has no [[models]] entries: {path}")
    pins = tuple(str(p) for p in data.get("pins", {}).get("order", [])) or tuple(m.id for m in models)
    return Catalogue(path=Path(path), models=models, pins_order=pins)


# --- name reconciliation ------------------------------------------------------

def _vendor_ok(organization: str | None, provider: str) -> bool:
    if not organization:
        return True
    expected = VENDOR_TOKENS.get(provider)
    if not expected:
        return True
    org = organization.lower()
    return any(tok in org for tok in expected)


def match_name(name: str, cat: Catalogue, organization: str | None = None) -> Match | None:
    """Map a source model name to a models.toml id; None when unsure (never guess).

    Precedence: exact id (3) > exact multi-token alias (2) > token match (1),
    where a token match is the family token or a single-word alias found among
    the name's tokens; it is reported as `family` when the token is the family
    name. Token matches must agree on the major version when the name carries
    one ("Claude Fable 5" → claude-fable-5-1, but "Claude Opus 4.8" → None),
    and every match must agree with the organization column when present. Two
    models tied at the best specificity → ambiguous → None.
    """
    c = canon(name)
    if not c:
        return None
    toks = set(c.split("-"))
    name_major = version_major(c)
    candidates: list[Match] = []
    for m in cat.models:
        if not _vendor_ok(organization, m.provider):
            continue
        if c == m.canon_id:
            candidates.append(Match(m.id, "id", MATCH_SPECIFICITY["id"]))
            continue
        if c in m.exact_aliases:
            candidates.append(Match(m.id, "alias", MATCH_SPECIFICITY["alias"]))
            continue
        if name_major is not None and m.major is not None and name_major != m.major:
            continue
        if m.family_token and m.family_token in toks:
            candidates.append(Match(m.id, "family", MATCH_SPECIFICITY["family"]))
        elif toks & m.token_aliases:
            candidates.append(Match(m.id, "alias", MATCH_SPECIFICITY["family"]))
    if not candidates:
        return None
    best = max(cand.specificity for cand in candidates)
    winners = [cand for cand in candidates if cand.specificity == best]
    return winners[0] if len(winners) == 1 else None


# --- fetching -----------------------------------------------------------------

UrlOpen = Callable[..., Any]


def http_get(url: str, timeout_s: float, urlopen: UrlOpen) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/csv;q=0.9, */*;q=0.5",
    })
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def lmarena_rows_url(subset: str, offset: int = 0) -> str:
    query = urllib.parse.urlencode({
        "dataset": LMARENA_DATASET, "config": subset, "split": LMARENA_SPLIT,
        "offset": offset, "length": LMARENA_PAGE,
    })
    return f"{LMARENA_ROWS_API}?{query}"


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    """datasets-server page → plain row dicts (also accepts a bare row list)."""
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        items = payload["rows"]
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("unrecognised LMArena payload shape (no 'rows' list)")
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("row"), dict):
            rows.append(item["row"])
        elif isinstance(item, dict):
            rows.append(item)
    return rows


def fetch_lmarena_rows(subset: str, timeout_s: float, urlopen: UrlOpen) -> list[dict[str, Any]]:
    """All rows of `<subset>/latest`, paging through the datasets-server API."""
    rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(LMARENA_MAX_PAGES):
        payload = json.loads(http_get(lmarena_rows_url(subset, offset), timeout_s, urlopen))
        page = extract_rows(payload)
        rows.extend(page)
        offset += len(page)
        total = payload.get("num_rows_total") if isinstance(payload, dict) else None
        if len(page) < LMARENA_PAGE or (isinstance(total, int) and offset >= total):
            break
    return rows


def fetch_epoch_csv(timeout_s: float, urlopen: UrlOpen) -> str:
    return http_get(EPOCH_CSV_URL, timeout_s, urlopen).decode("utf-8-sig")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _pick_column(lower_to_real: dict[str, str], *names: str) -> str | None:
    return next((lower_to_real[n] for n in names if n in lower_to_real), None)


def parse_epoch_csv(text: str) -> list[EpochRow]:
    """Long (model, benchmark, score) or wide (model, <benchmark columns…>) CSV.

    Column names are not given by the report, so a documented candidate set
    is tried; anything else raises ValueError (source reported as failed).
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = [f for f in (reader.fieldnames or []) if f]
    lower = {f.strip().lower(): f for f in fields}
    model_col = _pick_column(lower, "model", "model_id", "model_name", "model version", "model_version")
    if model_col is None:
        raise ValueError(f"unrecognised Epoch CSV columns: {fields}")
    org_col = _pick_column(lower, "organization", "organisation", "org", "developer", "lab")
    bench_col = _pick_column(lower, "benchmark", "benchmark_id", "benchmark name", "task")
    score_col = _pick_column(lower, "score", "best score", "best_score", "best score (across scorers)",
                             "mean score", "mean_score", "value", "accuracy")
    if bench_col is not None and score_col is None:
        raise ValueError(f"Epoch CSV has a benchmark column but no score column: {fields}")
    rows: list[EpochRow] = []
    for raw in reader:
        model = (raw.get(model_col) or "").strip()
        if not model:
            continue
        org = (raw.get(org_col) or "").strip() or None if org_col else None
        if bench_col is not None:
            benchmark = (raw.get(bench_col) or "").strip()
            score = _to_float(raw.get(score_col))
            if benchmark and score is not None:
                rows.append(EpochRow(model, org, benchmark, score))
            continue
        for col in fields:
            if col in (model_col, org_col):
                continue
            score = _to_float(raw.get(col))
            if score is not None:
                rows.append(EpochRow(model, org, col.strip(), score))
    return rows


# --- codex app-server model/list ----------------------------------------------

def extract_model_ids(payload: Any) -> list[str]:
    """Ids from a model/list result, its JSON-RPC envelope, or a bare list."""
    if isinstance(payload, dict):
        if isinstance(payload.get("result"), (dict, list)):
            return extract_model_ids(payload["result"])
        for key in ("data", "models", "items"):
            if isinstance(payload.get(key), list):
                return extract_model_ids(payload[key])
        raise ValueError("unrecognised model/list payload (no data/models/items list)")
    if isinstance(payload, list):
        ids: list[str] = []
        for item in payload:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                value = item.get("id") or item.get("model")
                if isinstance(value, str):
                    ids.append(value)
        return ids
    raise ValueError("unrecognised model/list payload")


def codex_model_list(timeout_s: float, spawn: Callable[..., Any] = subprocess.Popen,
                     codex_bin: str | None = None) -> list[str]:
    """`codex app-server` handshake (contract §Codex probe) then `model/list`.

    Consumes no quota. Lines without our request id (notifications) are
    skipped. RPC error or EOF before the reply → RuntimeError.
    """
    exe = codex_bin or shutil.which("codex") or DEFAULT_CODEX_BIN
    proc = spawn([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(timeout_s, proc.kill)
    timer.daemon = True
    timer.start()
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "ai-router", "title": "ai-router", "version": _version()},
                        "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("id") != 2:
                continue
            if "error" in obj:
                err = obj["error"]
                detail = err.get("message", err) if isinstance(err, dict) else err
                raise RuntimeError(f"model/list error: {detail}")
            return extract_model_ids(obj.get("result"))
        raise RuntimeError(f"codex app-server closed without answering model/list ({timeout_s:g}s timeout)")
    finally:
        timer.cancel()
        # Best-effort reap: the model/list outcome is already decided above.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)


def resolve_model_list(timeout_s: float, spawn: Callable[..., Any] = subprocess.Popen,
                       codex_bin: str | None = None) -> list[str]:
    """CORE's `ai_router.quota.model_list()` when importable, else the local helper."""
    try:
        fn = getattr(importlib.import_module("ai_router.quota"), "model_list", None)
    except ImportError:
        fn = None
    if callable(fn):
        try:
            return extract_model_ids(fn())
        except TypeError:
            pass  # signature differs from the contract's no-arg form → local helper
    return codex_model_list(timeout_s, spawn=spawn, codex_bin=codex_bin)


# --- sources ------------------------------------------------------------------

def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def attempt(name: str, url: str, fn: Callable[[], Any], now: datetime) -> tuple[Any, dict[str, Any]]:
    """Run one source; failures become `ok: false` + `error`, never an exception."""
    source: dict[str, Any] = {"name": name, "url": url, "fetched_at": _iso(now), "ok": True, "error": None}
    try:
        return fn(), source
    except Exception as exc:  # any source failure is data, not a crash
        source["ok"] = False
        source["error"] = f"{type(exc).__name__}: {exc}"
        return None, source


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_offline(fixture_dir: Path, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    def file_url(name: str) -> str:
        return (fixture_dir / name).resolve().as_uri()

    data: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for subset in LMARENA_SUBSETS:
        fname = f"lmarena_{subset}.json"
        data[subset], src = attempt(f"lmarena_{subset}", file_url(fname),
                                    lambda f=fname: extract_rows(_read_json(fixture_dir / f)), now)
        sources.append(src)
    data["epoch"], src = attempt("epoch", file_url("epoch.csv"),
                                 lambda: parse_epoch_csv((fixture_dir / "epoch.csv").read_text(encoding="utf-8-sig")), now)
    sources.append(src)
    data["model_ids"], src = attempt("codex_model_list", file_url("model_list.json"),
                                     lambda: extract_model_ids(_read_json(fixture_dir / "model_list.json")), now)
    sources.append(src)
    return data, sources


def collect_live(timeout_s: float, urlopen: UrlOpen, spawn: Callable[..., Any],
                 now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for subset in LMARENA_SUBSETS:
        data[subset], src = attempt(f"lmarena_{subset}", lmarena_rows_url(subset),
                                    lambda s=subset: fetch_lmarena_rows(s, timeout_s, urlopen), now)
        sources.append(src)
    data["epoch"], src = attempt("epoch", EPOCH_CSV_URL,
                                 lambda: parse_epoch_csv(fetch_epoch_csv(timeout_s, urlopen)), now)
    sources.append(src)
    data["model_ids"], src = attempt("codex_model_list", CODEX_MODEL_LIST_URL,
                                     lambda: resolve_model_list(CODEX_PROBE_TIMEOUT_S, spawn=spawn), now)
    sources.append(src)
    return data, sources


# --- ranking ------------------------------------------------------------------

def select_leaderboard_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the `overall` category when present, then the newest `date`."""
    rows = list(rows)
    cats = {str(r["category"]).lower() for r in rows if r.get("category") is not None}
    if "overall" in cats:
        rows = [r for r in rows if str(r.get("category", "")).lower() == "overall"]
    dates = [str(r["date"]) for r in rows if r.get("date")]
    if dates:
        newest = max(dates)
        rows = [r for r in rows if str(r.get("date")) == newest]
    return rows


@dataclass
class Evidence:
    cat: Catalogue
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    _seen_unknown: set[tuple[str, str]] = field(default_factory=set)
    _weakest: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for m in self.cat.models:
            pin = self.cat.pins_order.index(m.id) + 1 if m.id in self.cat.pins_order else None
            self.models[m.id] = {
                "provider": m.provider, "family": m.family, "tier": m.tier, "pin_rank": pin,
                "lmarena_text_rank": None, "lmarena_text_score": None,
                "lmarena_webdev_rank": None, "lmarena_webdev_score": None,
                "epoch": {}, "matched_names": [], "weakest_match": None, "available": None,
            }

    def resolve(self, name: str, organization: str | None, source: str) -> str | None:
        match = match_name(name, self.cat, organization=organization)
        if match is None:
            if (source, name) not in self._seen_unknown:
                self._seen_unknown.add((source, name))
                self.unknown.append({"name": name, "organization": organization, "source": source})
            return None
        entry = self.models[match.model_id]
        if name not in entry["matched_names"]:
            entry["matched_names"].append(name)
        if match.specificity < self._weakest.get(match.model_id, MATCH_SPECIFICITY["id"] + 1):
            self._weakest[match.model_id] = match.specificity
            entry["weakest_match"] = match.kind
        return match.model_id

    def apply_lmarena(self, subset: str, rows: list[dict[str, Any]] | None) -> None:
        if rows is None:
            return
        rank_key, score_key = f"lmarena_{subset}_rank", f"lmarena_{subset}_score"
        for row in select_leaderboard_rows(rows):
            name = row.get("model_name")
            if not isinstance(name, str) or not name.strip():
                continue
            org = row.get("organization")
            model_id = self.resolve(name, org if isinstance(org, str) else None, f"lmarena_{subset}")
            if model_id is None:
                continue
            rank = _to_int(row.get("rank"))
            entry = self.models[model_id]
            if rank is not None and (entry[rank_key] is None or rank < entry[rank_key]):
                entry[rank_key] = rank
                entry[score_key] = _to_float(row.get("score"))

    def apply_epoch(self, rows: list[EpochRow] | None) -> None:
        if rows is None:
            return
        for row in rows:
            model_id = self.resolve(row.model, row.organization, "epoch")
            if model_id is None:
                continue
            scores = self.models[model_id]["epoch"]
            scores[row.benchmark] = max(row.score, scores.get(row.benchmark, row.score))

    def apply_availability(self, model_ids: list[str] | None) -> None:
        if model_ids is None:
            return
        listed = set(model_ids)
        for m in self.cat.models:
            if m.provider == "codex":
                self.models[m.id]["available"] = m.id in listed

    def evidence_order(self) -> list[str]:
        pins = list(self.cat.pins_order)

        def key(model_id: str) -> tuple[int, float, int]:
            e = self.models[model_id]
            pin_index = pins.index(model_id) if model_id in pins else len(pins)
            return (e["lmarena_text_rank"], -(e["lmarena_text_score"] or 0.0), pin_index)

        ranked = [mid for mid, e in self.models.items() if e["lmarena_text_rank"] is not None]
        return sorted(ranked, key=key)


def disagreements(pins_order: Iterable[str], evidence_order: list[str]) -> list[dict[str, Any]]:
    """Models pinned above what the evidence supports (one entry per such model).

    Ranks are positions among the evidence-covered models only, so the two
    columns are comparable. Every pairwise inversion has exactly one
    over-pinned member, so this list is empty iff the two orders agree.
    """
    covered = [m for m in pins_order if m in evidence_order]
    out: list[dict[str, Any]] = []
    for pin_rank, model in enumerate(covered, 1):
        ev_rank = evidence_order.index(model) + 1
        if ev_rank <= pin_rank:
            continue
        ahead = [x for x in evidence_order[:ev_rank - 1] if covered.index(x) + 1 > pin_rank]
        out.append({
            "model": model, "pin_rank": pin_rank, "evidence_rank": ev_rank,
            "note": (f"pinned #{pin_rank} of {len(covered)} evidence-covered models, but LMArena text "
                     f"(overall) places it #{ev_rank}, behind {', '.join(ahead)}"),
        })
    return out


def build_ranking(cat: Catalogue, data: dict[str, Any], sources: list[dict[str, Any]],
                  now: datetime) -> dict[str, Any]:
    ev = Evidence(cat)
    for subset in LMARENA_SUBSETS:
        ev.apply_lmarena(subset, data.get(subset))
    ev.apply_epoch(data.get("epoch"))
    ev.apply_availability(data.get("model_ids"))
    evidence_order = ev.evidence_order()
    return {
        "generated_at": _iso(now),
        "models_toml": str(cat.path),
        "sources": sources,
        "models": ev.models,
        "pins_order": list(cat.pins_order),
        "evidence_order": evidence_order,
        "disagreements": disagreements(cat.pins_order, evidence_order),
        "unknown": ev.unknown,
    }


# --- proposal + pending -------------------------------------------------------

def proposal_hash(result: dict[str, Any]) -> str:
    """Hash of the decision content only — timestamps and source status excluded."""
    payload = {
        "pins_order": result["pins_order"],
        "evidence_order": result["evidence_order"],
        "disagreements": [(d["model"], d["pin_rank"], d["evidence_rank"]) for d in result["disagreements"]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def proposed_order(result: dict[str, Any]) -> list[str]:
    evidence = result["evidence_order"]
    return evidence + [m for m in result["pins_order"] if m not in evidence]


def render_proposal(result: dict[str, Any]) -> str:
    models = result["models"]
    lines = [
        "# ai-router ranking proposal",
        "",
        f"Generated {result['generated_at']} by `update_ranking.py` from `{result['models_toml']}`.",
        "Pins are operator input: this file proposes, it never edits models.toml.",
        "",
        "## Disagreements (pinned above what LMArena text evidence supports)",
        "",
        "| model | pin # | evidence # | note |",
        "|---|---|---|---|",
    ]
    lines += [f"| `{d['model']}` | {d['pin_rank']} | {d['evidence_rank']} | {d['note']} |"
              for d in result["disagreements"]]
    lines += ["", "## Evidence order (LMArena text, overall; best rank per model)", ""]
    for i, mid in enumerate(result["evidence_order"], 1):
        e = models[mid]
        score = f"{e['lmarena_text_score']:.4f}" if e["lmarena_text_score"] is not None else "n/a"
        webdev = e["lmarena_webdev_rank"] if e["lmarena_webdev_rank"] is not None else "n/a"
        eci = e["epoch"].get("epoch_capabilities_index")
        epoch = f"{eci:g}" if isinstance(eci, (int, float)) else "n/a"
        lines.append(f"{i}. `{mid}` — text rank {e['lmarena_text_rank']} (score {score}); webdev rank {webdev}; "
                     f"Epoch ECI {epoch}; matched via {e['weakest_match']}: {', '.join(e['matched_names'])}")
    lines += ["", "## Current pins order", ""]
    lines += [f"{i}. `{mid}`" for i, mid in enumerate(result["pins_order"], 1)]
    lines += ["", "## Proposed `[pins] order` (evidence order first, unranked pins keep their order)", "",
              "```toml", "order = [" + ", ".join(f'"{m}"' for m in proposed_order(result)) + "]", "```", "",
              "## Sources", ""]
    lines += [f"- {s['name']}: {'ok' if s['ok'] else 'FAILED — ' + str(s['error'])} ({s['url']})"
              for s in result["sources"]]
    lines += ["", "## Unmatched names (never guessed)", ""]
    lines += [f"- {u['name']} ({u['organization'] or 'unknown org'}) — {u['source']}" for u in result["unknown"]] or ["- none"]
    return "\n".join(lines) + "\n"


def pending_entry(result: dict[str, Any], proposal_path: Path, out_path: Path, now: datetime) -> str:
    """One blocker in the format of ~/.claude/templates/pending-for-human.md."""
    dis = result["disagreements"]
    summary = "; ".join(f"`{d['model']}` pinned #{d['pin_rank']}, evidence #{d['evidence_rank']}" for d in dis)
    ok = sum(1 for s in result["sources"] if s["ok"])
    status = ", ".join(f"{s['name']} {'ok' if s['ok'] else 'failed'}" for s in result["sources"])
    n = len(dis)
    return "\n".join([
        f"## {now.date().isoformat()}: ai-router ranking evidence disagrees with pins ({n} model{'s' if n != 1 else ''})",
        f"**Why blocked**: `update_ranking.py` found {n} disagreement{'s' if n != 1 else ''} between "
        f"`[pins] order` in `{result['models_toml']}` and LMArena text evidence ({summary}); "
        "pins are operator input and are never edited automatically.",
        "**Steps for human**:",
        f"1. Read `{proposal_path}` (evidence table and a proposed `order = [...]` to paste).",
        f"2. To accept: edit `[pins] order` in `{result['models_toml']}` through a worktree PR "
        "(`~/worktrees/claude-<slug>/` off fresh `origin/master`). To keep the current pins: change nothing.",
        "3. Delete this entry either way; the next run re-flags only when the evidence changes.",
        f"**Everything else done**: `{out_path}` written ({ok}/{len(result['sources'])} sources ok: {status}); "
        "proposal rendered; nothing outside the ai-router state dir and this entry was touched.",
    ]) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def insert_pending_entry(path: Path, entry: str) -> None:
    """Newest at the top, below a leading `# title` line when the file has one."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    entry = entry.rstrip("\n") + "\n"
    if existing.startswith("# "):
        title, _, rest = existing.partition("\n")
        text = f"{title}\n\n{entry}" + (f"\n{rest.lstrip(chr(10))}" if rest.strip() else "")
    else:
        text = entry + (f"\n{existing.lstrip(chr(10))}" if existing.strip() else "")
    atomic_write(path, text)


def handle_proposal(result: dict[str, Any], state: Path, proposal_path: Path, pending_path: Path,
                    out_path: Path, now: datetime) -> None:
    hash_path = state / HASH_NAME
    digest = proposal_hash(result)
    previous = hash_path.read_text(encoding="utf-8").strip() if hash_path.exists() else ""
    changed = digest != previous
    written = appended = False
    if result["disagreements"] and changed:
        atomic_write(proposal_path, render_proposal(result))
        insert_pending_entry(pending_path, pending_entry(result, proposal_path, out_path, now))
        written = appended = True
    if changed:
        atomic_write(hash_path, digest + "\n")
    result["proposal"] = {
        "path": str(proposal_path), "pending_path": str(pending_path), "hash": digest,
        "changed": changed, "written": written, "pending_appended": appended,
    }


# --- CLI ----------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="update_ranking.py",
                                description="Refresh ai-router ranking evidence; flag pin disagreements.")
    p.add_argument("--models-toml", type=Path, default=Path(__file__).resolve().parent / "models.toml")
    p.add_argument("--state", type=Path, default=None, help="state dir (default $AI_ROUTER_STATE)")
    p.add_argument("--out", type=Path, default=None, help=f"default <state>/{OUT_NAME}")
    p.add_argument("--proposal", type=Path, default=None, help=f"default <state>/{PROPOSAL_NAME}")
    p.add_argument("--pending", type=Path, default=None, help=f"default {DEFAULT_PENDING}")
    p.add_argument("--offline-fixture", type=Path, default=None, metavar="DIR",
                   help="read lmarena_text.json, lmarena_webdev.json, epoch.csv, model_list.json from DIR")
    p.add_argument("--timeout-s", type=float, default=20.0, help="per-request HTTP timeout")
    p.add_argument("--json", action="store_true", help="print the ranking object on stdout")
    return p.parse_args(argv)


def resolve_state_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get("AI_ROUTER_STATE")
    return Path(env).expanduser() if env else DEFAULT_STATE_DIR.expanduser()


def run(opts: argparse.Namespace, *, urlopen: UrlOpen = urllib.request.urlopen,
        spawn: Callable[..., Any] = subprocess.Popen, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cat = load_models(opts.models_toml.expanduser().resolve())
    state = resolve_state_dir(opts.state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_path = (opts.out or state / OUT_NAME).expanduser()
    proposal_path = (opts.proposal or state / PROPOSAL_NAME).expanduser()
    pending_path = (opts.pending or DEFAULT_PENDING).expanduser()

    if opts.offline_fixture is not None:
        data, sources = collect_offline(opts.offline_fixture.expanduser(), now)
    else:
        data, sources = collect_live(opts.timeout_s, urlopen, spawn, now)
    result = build_ranking(cat, data, sources, now)
    handle_proposal(result, state, proposal_path, pending_path, out_path, now)
    atomic_write(out_path, json.dumps(result, indent=2, sort_keys=False) + "\n")
    result["out"] = str(out_path)
    return result


def summary_line(result: dict[str, Any]) -> str:
    ok = sum(1 for s in result["sources"] if s["ok"])
    p = result["proposal"]
    proposal = ("written, pending entry added" if p["written"] else
                "unchanged" if result["disagreements"] else "none (pins agree with evidence)")
    n = len(result["disagreements"])
    return (f"ranking: {len(result['models'])} models, {len(result['evidence_order'])} with LMArena text "
            f"evidence, {n} disagreement{'s' if n != 1 else ''}; sources ok {ok}/{len(result['sources'])}; "
            f"proposal {proposal} -> {result['out']}")


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(opts)
    except SetupError as exc:
        if opts.json:
            print(json.dumps({"ok": False, "error_class": "setup", "message": str(exc)}))
        else:
            print(f"update_ranking: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if opts.json else summary_line(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

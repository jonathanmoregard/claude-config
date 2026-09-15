"""Quota probes, parsers and cache (contract §Quota snapshot schemas, §Codex
probe, §Claude probe).

Codex: ``codex app-server`` JSON-RPC (``account/rateLimits/read`` +
``model/list``), cached ``settings.codex_cache_s`` in ``quota/codex.json``;
on failure the cached snapshot is kept (``stale``), else the newest rollout
``token_count`` event, else ``unknown``.

Claude: ``quota/claude.json`` (raw statusline tee, written by the HOOKS
track) when fresher than ``settings.stale_after_s``; else the OAuth usage
endpoint when enabled, credentials are readable and no backoff marker is in
force. The OAuth fetch is only ever exercised with an injected ``urlopen`` in
tests. Nothing here raises for a provider failure; the answer degrades to a
stale or ``unknown`` snapshot.

Every provider answer is normalised to one shape (``normalised``) which
``quota --json`` prints and ``selection.pick`` consumes.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import VERSION
from .config import Config, atomic_write_json, atomic_write_text, read_json
from .pacing import bucket_status

RPC_TIMEOUT_S = 8.0
OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_TIMEOUT_S = 6.0
OAUTH_BACKOFF_S = 900
# User-Agent version when no statusline snapshot has told us the CLI version.
DEFAULT_CLI_VERSION = "2.1.270"
FAMILY_TOKENS = ("opus", "sonnet", "fable", "haiku")

RpcFn = Callable[[list[str]], dict[str, Any]]
UrlOpen = Callable[..., Any]


# --- files ---------------------------------------------------------------------

def quota_dir(state: Path) -> Path:
    d = state / "quota"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def snapshot_path(state: Path, provider: str) -> Path:
    return quota_dir(state) / f"{provider}.json"


def rejected_path(state: Path, provider: str) -> Path:
    return quota_dir(state) / f"{provider}.rejected.json"


def backoff_path(state: Path) -> Path:
    return quota_dir(state) / "claude.oauth-backoff"


# --- rejection markers ---------------------------------------------------------------

def mark_rejected(state: Path, provider: str, *, until: float | None, reason: str, source: str,
                  now: float) -> dict[str, Any]:
    obj = {"provider": provider, "until": int(until) if until is not None else None,
           "reason": reason[:300], "observed_at": int(now), "source": source}
    atomic_write_json(rejected_path(state, provider), obj)
    return obj


def rejection(cfg: Config, state: Path, provider: str, now: float) -> dict[str, Any] | None:
    """The rejected.json record if it is still in force, else None."""
    obj = read_json(rejected_path(state, provider))
    if not isinstance(obj, dict):
        return None
    until = obj.get("until")
    if until is None:
        until = float(obj.get("observed_at", 0)) + cfg.settings.rejected_default_ttl_s
    if now < float(until):
        return {**obj, "until": int(until)}
    return None


def live_supersedes(cfg: Config, rejected: dict[str, Any] | None, observed_at: Any,
                    buckets: list[dict[str, Any]], now: float) -> bool:
    """A rejection marker is a hint with a horizon, not a verdict.

    A snapshot observed AFTER the marker was written, whose applicable buckets
    all keep headroom at or above their floor, is newer evidence than the
    marker (the window rolled, credits were added, or the parsed "try again
    at" was wrong). The floor is the one selection applies per bucket
    (``pacing.bucket_status``: headroom under min(reserve, line) is
    ineligible), so a cleared provider is one `pick` would actually offer.
    Selection's near-reset relax is deliberately NOT mirrored: a capped bucket
    minutes from its reset keeps the marker (it lapses at that reset anyway),
    whereas honouring the descending floor matters for days. Buckets that
    apply to no model, and snapshots older than the marker, are not evidence
    either way.
    """
    if rejected is None or observed_at is None:
        return False
    if float(observed_at) <= float(rejected.get("observed_at") or 0):
        return False
    applicable = [b for b in buckets if b.get("applies_to") and b.get("used_pct") is not None]
    if not applicable:
        return False
    for b in applicable:
        room, _line, _relaxed, floor = bucket_status(cfg, b, now)
        if room < floor:
            return False
    return True


def _clear_superseded(cfg: Config, state: Path, provider: str, rejected: dict[str, Any] | None,
                      observed_at: Any, buckets: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """Drop the marker file when live evidence supersedes it; return the rejection still in force."""
    if not live_supersedes(cfg, rejected, observed_at, buckets, now):
        return rejected
    with contextlib.suppress(OSError):
        rejected_path(state, provider).unlink()
    return None


# --- normalisation ---------------------------------------------------------------

def _to_epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _window(d: dict[str, Any]) -> dict[str, Any] | None:
    """Read one codex window in either camelCase (app-server) or snake_case (rollout)."""
    if not isinstance(d, dict):
        return None
    used = _num(d.get("usedPercent", d.get("used_percent")))
    if used is None:
        return None
    window = d.get("windowDurationMins", d.get("window_duration_mins", d.get("window_minutes")))
    return {"used_pct": used, "resets_at": _to_epoch(d.get("resetsAt", d.get("resets_at"))),
            "window_min": int(window) if window is not None else None}


def _bucket(cfg: Config, provider: str, bucket_id: str, window: dict[str, Any],
            applies_default: list[str]) -> dict[str, Any]:
    configured = cfg.bucket_applies_to(provider, bucket_id)
    applies = list(configured) if configured is not None else applies_default
    return {"id": bucket_id, **window, "applies_to": applies}


def codex_buckets(cfg: Config, rate_limits: dict[str, Any] | None,
                  by_limit_id: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Buckets from a codex rateLimits result (app-server or rollout shape)."""
    out: list[dict[str, Any]] = []
    main_id = None
    if isinstance(rate_limits, dict):
        main_id = rate_limits.get("limitId", rate_limits.get("limit_id"))
        for name in ("primary", "secondary"):
            w = _window(rate_limits.get(name))
            if w:
                out.append(_bucket(cfg, "codex", name, w, ["*"]))
    for limit_id, entry in (by_limit_id or {}).items():
        if limit_id == main_id or not isinstance(entry, dict):
            continue
        for name in ("primary", "secondary"):
            w = _window(entry.get(name))
            if w:
                out.append({"id": f"{limit_id}.{name}", **w, "applies_to": []})
    return out


def _claude_applies(cfg: Config, bucket_id: str) -> list[str]:
    configured = cfg.bucket_applies_to("claude", bucket_id)
    if configured is not None:
        return list(configured)
    if bucket_id == "extra_usage":
        return []
    for tok in FAMILY_TOKENS:
        if tok in bucket_id:
            return [m.id for m in cfg.models if m.provider == "claude" and m.family == tok]
    return ["*"]


def claude_buckets(cfg: Config, rate_limits: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Buckets from statusline `rate_limits` or an OAuth usage payload (flat keys + limits[])."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(rate_limits, dict):
        return []
    for key, val in rate_limits.items():
        if key == "limits" or not isinstance(val, dict):
            continue
        used = _num(val.get("used_percentage", val.get("utilization", val.get("percent"))))
        if used is None:
            continue
        out[key] = {"id": key, "used_pct": used, "resets_at": _to_epoch(val.get("resets_at")),
                    "window_min": 300 if key == "five_hour" else 10080 if key.startswith("seven_day") else None,
                    "applies_to": _claude_applies(cfg, key)}
    for lim in rate_limits.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind")
        if kind == "session":
            bid = "five_hour"
        elif kind == "weekly_all":
            bid = "seven_day"
        elif kind == "weekly_scoped":
            display = str((((lim.get("scope") or {}).get("model") or {}).get("display_name")) or "").strip()
            if not display:
                continue
            bid = "seven_day_" + display.split()[0].lower()
        else:
            continue
        used = _num(lim.get("percent", lim.get("utilization")))
        if used is None or bid in out:
            continue
        out[bid] = {"id": bid, "used_pct": used, "resets_at": _to_epoch(lim.get("resets_at")),
                    "window_min": 300 if bid == "five_hour" else 10080, "applies_to": _claude_applies(cfg, bid)}
    return list(out.values())


def normalised(cfg: Config, provider: str, *, status: str, source: str, observed_at: float | None,
               now: float, plan: str | None = None, buckets: list[dict[str, Any]] | None = None,
               models: list[str] | None = None, rejected: dict[str, Any] | None = None,
               error: str | None = None) -> dict[str, Any]:
    age = int(now - observed_at) if observed_at is not None else None
    snap: dict[str, Any] = {
        "provider": provider, "status": status, "source": source,
        "observed_at": int(observed_at) if observed_at is not None else None,
        "age_s": age, "stale": bool(age is not None and age >= cfg.settings.stale_after_s),
        "plan": plan or cfg.plans.get(provider) or None, "buckets": buckets or [],
        "rejected_until": None, "reason": None, "error": error,
    }
    if provider == "codex":
        snap["models"] = models
    if rejected is not None:
        snap["status"] = "rejected"
        snap["rejected_until"] = rejected.get("until")
        snap["reason"] = rejected.get("reason")
    return snap


# --- codex app-server RPC ---------------------------------------------------------------

def _rpc_request(rid: int, method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"


def app_server_call(codex_bin: str, methods: list[str], timeout_s: float = RPC_TIMEOUT_S) -> dict[str, Any]:
    """Run `codex app-server`, do the handshake, request each method (ids 2, 3, …).

    Returns {method: result}. Raises RuntimeError on timeout, error response or
    a dead server; the process is always killed before returning.
    """
    proc = subprocess.Popen([codex_bin, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    wanted = {i + 2: m for i, m in enumerate(methods)}
    results: dict[str, Any] = {}
    errors: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            obj = None
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("id") not in wanted:
                continue
            method = wanted[obj["id"]]
            if "error" in obj:
                errors.append(f"{method}: {obj['error']}")
            else:
                results[method] = obj.get("result")
            if len(results) + len(errors) >= len(wanted):
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(_rpc_request(1, "initialize", {
            "clientInfo": {"name": "ai-router", "title": "ai-router", "version": VERSION}, "capabilities": {}}))
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        for rid, method in wanted.items():
            proc.stdin.write(_rpc_request(rid, method, {}))
        proc.stdin.flush()
        t.join(timeout_s)
        if t.is_alive():
            raise RuntimeError(f"codex app-server: no response within {timeout_s:g}s")
        if errors:
            raise RuntimeError("codex app-server: " + "; ".join(errors))
        missing = [m for m in methods if m not in results]
        if missing:
            raise RuntimeError("codex app-server exited before answering " + ", ".join(missing))
        return results
    except BrokenPipeError:
        raise RuntimeError("codex app-server closed its stdin") from None
    finally:
        # Best-effort reap: the RPC outcome is already decided above, and a
        # process that is gone or slow to exit must not turn it into an error.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=2)


def model_ids(model_list_result: Any) -> list[str] | None:
    if not isinstance(model_list_result, dict):
        return None
    data = model_list_result.get("data")
    if not isinstance(data, list):
        return None
    return [str(m["id"]) for m in data if isinstance(m, dict) and m.get("id") and not m.get("hidden")]


# --- codex rollout fallback ---------------------------------------------------------------

def newest_rollout(sessions_dir: Path) -> Path | None:
    """Most recently modified rollout-*.jsonl under YYYY/MM/DD (a live session keeps appending)."""
    best: tuple[float, Path] | None = None
    try:
        for p in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if best is None or m > best[0]:
                best = (m, p)
    except OSError:
        return None
    return best[1] if best else None


def rollout_rate_limits(path: Path) -> tuple[dict[str, Any], float] | None:
    """(payload.rate_limits, event epoch) of the last token_count event carrying rate_limits."""
    found: tuple[dict[str, Any], float] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"token_count"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") if isinstance(obj, dict) else None
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                rl = payload.get("rate_limits")
                if isinstance(rl, dict):
                    ts = _to_epoch(obj.get("timestamp")) or 0
                    found = (rl, float(ts))
    except OSError:
        return None
    return found


# --- codex probe ---------------------------------------------------------------

def _codex_from_cache(cfg: Config, state: Path, cached: dict[str, Any], now: float,
                      rejected: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    buckets = cached.get("buckets") or []
    rejected = _clear_superseded(cfg, state, "codex", rejected, cached.get("observed_at"), buckets, now)
    return normalised(cfg, "codex", status="ok", source=str(cached.get("source", "app-server")),
                      observed_at=cached.get("observed_at"), now=now, plan=cached.get("plan"),
                      buckets=buckets, models=cached.get("models"), rejected=rejected, error=error)


def probe_codex(cfg: Config, state: Path, *, now: float, fresh: bool = False, rpc: RpcFn | None = None,
                codex_bin: str | None = None, sessions_dir: Path | None = None,
                home: Path | None = None) -> dict[str, Any]:
    rejected = rejection(cfg, state, "codex", now)
    path = snapshot_path(state, "codex")
    cached = read_json(path)
    if not isinstance(cached, dict) or cached.get("observed_at") is None:
        cached = None
    if cached and not fresh and now - float(cached["observed_at"]) < cfg.settings.codex_cache_s:
        return _codex_from_cache(cfg, state, cached, now, rejected)

    error: str | None
    if rpc is None and codex_bin is None:
        error = "codex binary not found"
    else:
        call = rpc or (lambda methods: app_server_call(codex_bin or "codex", methods))
        try:
            res = call(["account/rateLimits/read", "model/list"])
            rl = res.get("account/rateLimits/read") or {}
            rate_limits = rl.get("rateLimits") if isinstance(rl, dict) else None
            record = {"provider": "codex", "source": "app-server", "observed_at": int(now),
                      "plan": (rate_limits or {}).get("planType") or cfg.plans.get("codex"),
                      "buckets": codex_buckets(cfg, rate_limits, rl.get("rateLimitsByLimitId") if isinstance(rl, dict) else None),
                      "models": model_ids(res.get("model/list")), "raw": rl}
            atomic_write_json(path, record)
            return _codex_from_cache(cfg, state, record, now, rejected)
        except Exception as exc:  # noqa: BLE001 — any probe failure degrades, never raises
            error = f"{type(exc).__name__}: {exc}"

    if cached:
        return _codex_from_cache(cfg, state, cached, now, rejected, error=error)

    sessions = sessions_dir if sessions_dir is not None else (home or Path.home()) / ".codex" / "sessions"
    newest = newest_rollout(sessions)
    hit = rollout_rate_limits(newest) if newest else None
    if hit:
        rl, ts = hit
        record = {"provider": "codex", "source": "rollout", "observed_at": int(ts or now),
                  "plan": rl.get("plan_type") or cfg.plans.get("codex"),
                  "buckets": codex_buckets(cfg, rl, None), "models": None, "raw": rl}
        atomic_write_json(path, record)
        return _codex_from_cache(cfg, state, record, now, rejected, error=error)
    return normalised(cfg, "codex", status="unknown", source="none", observed_at=None, now=now,
                      rejected=rejected, error=error)


# --- claude probe ---------------------------------------------------------------

def _claude_from_raw(cfg: Config, state: Path, raw: dict[str, Any], now: float,
                     rejected: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    buckets = claude_buckets(cfg, raw.get("rate_limits"))
    rejected = _clear_superseded(cfg, state, "claude", rejected, raw.get("observed_at"), buckets, now)
    return normalised(cfg, "claude", status="ok", source=str(raw.get("source", "statusline")),
                      observed_at=raw.get("observed_at"), now=now, buckets=buckets, rejected=rejected, error=error)


def _oauth_token(credentials_path: Path) -> str | None:
    creds = read_json(credentials_path)
    if not isinstance(creds, dict):
        return None
    token = (creds.get("claudeAiOauth") or {}).get("accessToken")
    return str(token) if token else None


def fetch_oauth_usage(token: str, cli_version: str, urlopen: UrlOpen, state: Path, now: float) -> dict[str, Any]:
    """GET the OAuth usage endpoint; on 429 write the backoff marker. Raises on failure."""
    req = urllib.request.Request(OAUTH_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-code/{cli_version}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=OAUTH_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            atomic_write_text(backoff_path(state), f"{int(now + OAUTH_BACKOFF_S)}\n")
        raise
    payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    if not isinstance(payload, dict):
        raise ValueError("oauth usage payload is not an object")
    return payload


def _oauth_backoff_active(state: Path, now: float) -> bool:
    try:
        return now < float(backoff_path(state).read_text().strip())
    except (OSError, ValueError):
        return False


def probe_claude(cfg: Config, state: Path, *, now: float, fresh: bool = False, urlopen: UrlOpen | None = None,
                 credentials_path: Path | None = None, home: Path | None = None) -> dict[str, Any]:
    rejected = rejection(cfg, state, "claude", now)
    path = snapshot_path(state, "claude")
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get("observed_at") is None:
        raw = None
    if raw and not fresh and now - float(raw["observed_at"]) < cfg.settings.stale_after_s:
        return _claude_from_raw(cfg, state, raw, now, rejected)

    error: str | None = None
    creds = credentials_path if credentials_path is not None else \
        (home or Path.home()) / ".claude" / ".credentials.json"
    if cfg.settings.claude_oauth_fallback and not _oauth_backoff_active(state, now):
        token = _oauth_token(creds) if os.access(creds, os.R_OK) else None
        if token:
            cli_version = str((raw or {}).get("cli_version") or DEFAULT_CLI_VERSION)
            try:
                payload = fetch_oauth_usage(token, cli_version, urlopen or urllib.request.urlopen, state, now)
                record = {"provider": "claude", "source": "oauth", "observed_at": int(now),
                          "cli_version": cli_version, "rate_limits": payload}
                atomic_write_json(path, record)
                return _claude_from_raw(cfg, state, record, now, rejected)
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                error = f"oauth: {type(exc).__name__}: {exc}"
        else:
            error = "oauth: no credentials"
    if raw:
        return _claude_from_raw(cfg, state, raw, now, rejected, error=error)
    return normalised(cfg, "claude", status="unknown", source="none", observed_at=None, now=now,
                      rejected=rejected, error=error)


# --- both ---------------------------------------------------------------

def all_snapshots(cfg: Config, state: Path, *, now: float, fresh: bool = False, provider: str | None = None,
                  codex_bin: str | None = None, rpc: RpcFn | None = None, urlopen: UrlOpen | None = None,
                  home: Path | None = None) -> dict[str, dict[str, Any]]:
    """Normalised snapshots keyed by provider. ``home`` locates ~/.codex and ~/.claude (injected, never
    ``Path.home()`` from the CLI, so an injected env isolates a test from the real machine)."""
    out: dict[str, dict[str, Any]] = {}
    if provider in (None, "codex"):
        out["codex"] = probe_codex(cfg, state, now=now, fresh=fresh, rpc=rpc, codex_bin=codex_bin, home=home)
    if provider in (None, "claude"):
        out["claude"] = probe_claude(cfg, state, now=now, fresh=fresh, urlopen=urlopen, home=home)
    return out


def blocking_reset(snapshot: dict[str, Any], model_id: str, family: str) -> int | None:
    """resets_at of the fullest bucket applying to the model, for rejected.json `until`."""
    best: tuple[float, int | None] | None = None
    for b in snapshot.get("buckets", []):
        targets = b.get("applies_to") or []
        if not any(t in ("*", model_id, family) for t in targets):
            continue
        used = b.get("used_pct")
        if used is None:
            continue
        if best is None or float(used) > best[0]:
            best = (float(used), b.get("resets_at"))
    return best[1] if best else None


def now_epoch() -> float:
    return time.time()

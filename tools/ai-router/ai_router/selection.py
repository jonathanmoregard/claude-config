"""Model selection (contract §Selection).

``pick`` is a pure, deterministic function of the config, the normalised
quota snapshots and an injected ``now``. It never probes, never reads the
state directory and never mutates its inputs; the CLI gathers snapshots and
hands them in.
"""

from __future__ import annotations

from typing import Any

from .config import TIERS, Config, Model

MODEL_LIST_MAX_AGE_S = 3600  # codex model/list is trusted for availability for 1 h


def detect_host(env: dict[str, str]) -> str | None:
    """Which CLI is running us: claude, codex or None (contract §Selection)."""
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    return None


def _applies(bucket: dict[str, Any], model: Model) -> bool:
    targets = bucket.get("applies_to") or []
    return any(t == "*" or t == model.id or t == model.family for t in targets)


def _headroom(cfg: Config, snap: dict[str, Any] | None, model: Model, now: float) -> tuple[float | None, bool, str]:
    """(headroom, reserve_test_applies, note).

    headroom = min(100 - used_pct) over applicable buckets that are not about
    to reset; buckets resetting within ``near_reset_relax_min`` are reported
    but excluded from the reserve test. Unknown snapshot → (None, False, …).
    """
    if not snap or snap.get("status") == "unknown":
        return None, False, "quota unknown"
    relax_s = cfg.settings.near_reset_relax_min * 60
    counted: list[float] = []
    relaxed: list[float] = []
    for b in snap.get("buckets", []):
        if not _applies(b, model):
            continue
        used = b.get("used_pct")
        if used is None:
            continue
        room = 100.0 - float(used)
        resets_at = b.get("resets_at")
        if resets_at is not None and (float(resets_at) - now) <= relax_s:
            relaxed.append(room)
        else:
            counted.append(room)
    if counted:
        return min(counted), True, ""
    if relaxed:
        return min(relaxed), False, "near reset, reserve relaxed"
    return None, False, "no applicable bucket"


def _available(snap: dict[str, Any] | None, model: Model, now: float) -> bool:
    """codex model/list availability when the cached list is < 1 h old."""
    if model.provider != "codex" or not snap:
        return True
    models = snap.get("models")
    observed = snap.get("observed_at")
    if not isinstance(models, list) or observed is None:
        return True
    if now - float(observed) >= MODEL_LIST_MAX_AGE_S:
        return True
    return model.id in models


def _fmt(n: float | None) -> str:
    if n is None:
        return "?"
    return f"{n:g}"


def _evaluate(cfg: Config, snapshots: dict[str, dict[str, Any]], model: Model, need: tuple[str, ...],
              provider: str | None, now: float) -> dict[str, Any]:
    snap = snapshots.get(model.provider)
    headroom, reserve_applies, note = _headroom(cfg, snap, model, now)
    reasons: list[str] = []
    if provider is not None and model.provider != provider:
        reasons.append(f"provider filter {provider}")
    missing = [c for c in need if c not in model.caps]
    if missing:
        reasons.append("lacks " + ",".join(missing))
    if snap and snap.get("status") == "rejected":
        until = snap.get("rejected_until")
        reasons.append(f"provider rejected until {until if until is not None else 'ttl'}")
    if headroom is None:
        if not cfg.settings.unknown_is_eligible:
            reasons.append(f"{note}, unknown_is_eligible=false")
    elif reserve_applies and headroom < cfg.settings.reserve_pct:
        reasons.append(f"headroom {_fmt(headroom)} % < reserve {_fmt(cfg.settings.reserve_pct)} %")
    if not _available(snap, model, now):
        reasons.append("unavailable per codex model/list")
    eligible = not reasons
    if eligible:
        why = f"headroom {_fmt(headroom)} %" if headroom is not None else note
        if note and headroom is not None:
            why += f" ({note})"
    else:
        why = "; ".join(reasons)
    return {"model": model.id, "provider": model.provider, "tier": model.tier, "eligible": eligible,
            "headroom": headroom, "why": why}


def _candidates(cfg: Config, tier: str, allow_downgrade: bool) -> list[Model]:
    tiers = [tier]
    if allow_downgrade:
        tiers += list(TIERS[TIERS.index(tier) + 1:])
    out: list[Model] = []
    for t in tiers:
        out.extend(cfg.by_tier(t))
    return out


def pick(cfg: Config, snapshots: dict[str, dict[str, Any]], tier: str, need: tuple[str, ...] | list[str] = (),
         provider: str | None = None, host: str | None = None, allow_downgrade: bool = False,
         now: float | None = None) -> dict[str, Any]:
    """Choose a model for ``tier`` (contract §Selection). Pure; inject ``now``."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    if now is None:
        raise ValueError("pick requires an explicit now")
    need_t = tuple(need)
    cands = _candidates(cfg, tier, allow_downgrade)
    if provider is not None:
        cands = [m for m in cands if m.provider == provider]
    rows = [_evaluate(cfg, snapshots, m, need_t, provider, now) for m in cands]

    chosen: dict[str, Any] | None = None
    if cfg.settings.policy == "headroom-weighted":
        # Within the first tier that has any eligible model, prefer max headroom;
        # None sorts last; ties keep pins order (stable sort).
        for t in [tier] + (list(TIERS[TIERS.index(tier) + 1:]) if allow_downgrade else []):
            elig = [r for r in rows if r["eligible"] and r["tier"] == t]
            if elig:
                chosen = sorted(elig, key=lambda r: (r["headroom"] is None, -(r["headroom"] or 0)))[0]
                break
    else:
        chosen = next((r for r in rows if r["eligible"]), None)

    if chosen is None:
        return {"ok": False, "reason": f"no eligible {tier} model" + (f" for provider {provider}" if provider else ""),
                "candidates": rows}
    model = cfg.model(chosen["model"])
    assert model is not None
    reason = f"{cfg.settings.policy}: {chosen['why']}"
    if chosen["headroom"] is None:
        reason += " (quota unknown, eligible by settings.unknown_is_eligible)"
    if model.tier != tier:
        reason += f"; downgraded from {tier} to {model.tier}"
    return {"ok": True, "provider": model.provider, "model": model.id, "effort": model.default_effort,
            "tier": model.tier, "native": host is not None and model.provider == host, "host": host,
            "headroom": chosen["headroom"], "reason": reason, "candidates": rows}

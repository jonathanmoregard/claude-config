"""models.toml loading and the state directory (contract §State directory,
§models.toml).

``load_config`` parses ``tools/ai-router/models.toml`` into typed records;
``state_dir`` resolves ``$AI_ROUTER_STATE`` (default
``~/.local/state/ai-router``) and creates it mode 0700. ``atomic_write_*``
implement the "write ``<file>.tmp`` then ``os.replace``" rule every writer in
the package follows.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIERS: tuple[str, ...] = ("strongest", "standard", "fast")
PROVIDERS: tuple[str, ...] = ("codex", "claude")
SANDBOXES: tuple[str, ...] = ("read-only", "workspace-write")

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MODELS_TOML = PACKAGE_DIR.parent / "models.toml"


class ConfigError(ValueError):
    """models.toml is missing, malformed or internally inconsistent."""


@dataclass(frozen=True)
class Settings:
    reserve_pct: float = 15
    stale_after_s: int = 900
    policy: str = "rank-first"
    near_reset_relax_min: int = 120
    rejected_default_ttl_s: int = 3600
    unknown_is_eligible: bool = True
    claude_oauth_fallback: bool = True
    codex_cache_s: int = 60
    job_stall_min: int = 10
    job_timeout_s: int = 3600


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    family: str
    tier: str
    default_effort: str
    caps: tuple[str, ...]
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    settings: Settings
    models: tuple[Model, ...]
    pins: tuple[str, ...]
    buckets: dict[str, dict[str, tuple[str, ...]]]
    efforts: dict[str, tuple[str, ...]]
    plans: dict[str, str]
    path: Path
    _by_name: dict[str, Model] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        by_name: dict[str, Model] = {}
        for m in self.models:
            by_name[m.id] = m
            for alias in m.aliases:
                by_name.setdefault(alias, m)
        object.__setattr__(self, "_by_name", by_name)

    def model(self, name: str) -> Model | None:
        """Resolve an exact id or alias (case-insensitive) to its Model."""
        return self._by_name.get(name) or self._by_name.get(name.lower())

    def pinned(self) -> tuple[Model, ...]:
        """Models in operator pin order (models absent from pins come last)."""
        ordered = [self._by_name[i] for i in self.pins if i in self._by_name]
        rest = [m for m in self.models if m.id not in self.pins]
        return tuple(ordered + rest)

    def by_tier(self, tier: str) -> tuple[Model, ...]:
        return tuple(m for m in self.pinned() if m.tier == tier)

    def bucket_applies_to(self, provider: str, bucket_id: str) -> tuple[str, ...] | None:
        """Configured `applies_to` for a bucket, or None when not configured."""
        return self.buckets.get(provider, {}).get(bucket_id)


def _settings(raw: dict[str, Any]) -> Settings:
    known = {f for f in Settings.__dataclass_fields__}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown [settings] keys: {', '.join(unknown)}")
    s = Settings(**raw)
    if s.policy not in ("rank-first", "headroom-weighted"):
        raise ConfigError(f"settings.policy must be rank-first|headroom-weighted, got {s.policy!r}")
    return s


def _model(raw: dict[str, Any], efforts: dict[str, tuple[str, ...]]) -> Model:
    try:
        m = Model(
            id=str(raw["id"]),
            provider=str(raw["provider"]),
            family=str(raw["family"]),
            tier=str(raw["tier"]),
            default_effort=str(raw["default_effort"]),
            caps=tuple(raw.get("caps", ())),
            aliases=tuple(raw.get("aliases", ())),
        )
    except KeyError as exc:
        raise ConfigError(f"model entry missing key {exc}") from None
    if m.provider not in PROVIDERS:
        raise ConfigError(f"model {m.id}: unknown provider {m.provider!r}")
    if m.tier not in TIERS:
        raise ConfigError(f"model {m.id}: unknown tier {m.tier!r}")
    if m.default_effort not in efforts.get(m.provider, ()):
        raise ConfigError(f"model {m.id}: default_effort {m.default_effort!r} not in provider efforts")
    return m


def load_config(path: Path | str | None = None) -> Config:
    """Parse models.toml. Raises ConfigError on any structural problem."""
    p = Path(path) if path is not None else DEFAULT_MODELS_TOML
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"models.toml not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"models.toml parse error: {exc}") from None
    if raw.get("version") != 1:
        raise ConfigError(f"models.toml version must be 1, got {raw.get('version')!r}")

    providers = raw.get("providers", {})
    efforts = {name: tuple(v.get("efforts", ())) for name, v in providers.items()}
    plans = {name: str(v.get("plan", "")) for name, v in providers.items()}
    settings = _settings(raw.get("settings", {}))
    models = tuple(_model(m, efforts) for m in raw.get("models", ()))
    ids = [m.id for m in models]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate model ids in models.toml")
    pins = tuple(raw.get("pins", {}).get("order", ()))
    for pin in pins:
        if pin not in ids:
            raise ConfigError(f"pins.order names unknown model {pin!r}")
    buckets = {
        provider: {bucket: tuple(targets) for bucket, targets in table.items()}
        for provider, table in raw.get("buckets", {}).items()
    }
    return Config(settings=settings, models=models, pins=pins, buckets=buckets,
                  efforts=efforts, plans=plans, path=p)


def state_dir(env: dict[str, str] | None = None) -> Path:
    """Resolve and create the state directory (mode 0700)."""
    e = os.environ if env is None else env
    raw = e.get("AI_ROUTER_STATE") or os.path.join(e.get("HOME", os.path.expanduser("~")),
                                                   ".local", "state", "ai-router")
    p = Path(raw).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


def read_json(path: Path) -> Any | None:
    """Best-effort JSON read: missing or corrupt file → None."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

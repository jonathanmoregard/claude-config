"""Secret redaction shared by the PostToolUse output hooks.

``redact_value(value, gitleaks=..., timeout=...)`` returns ``value`` with every
secret in its strings replaced by ``[REDACTED:<rule>:<n>chars]``. Detection is
gitleaks (``gitleaks_driver.py``) with ``secret-rules.toml``: gitleaks' own
rules plus the formats it lacks.

Invariants:
- Any scanner fault raises ``ScanError``; callers WITHHOLD. There is no partial result.
- A secret found in one string is redacted in every string of the value.
- Redaction is idempotent: a marker is never redacted again.
- Binary payloads (image/PDF base64, MCP image data, blobs) are neither scanned nor changed.
- Only string values change: the result has the input's exact shape. Dict keys are
  scanned but never rewritten: a secret in a key raises ScanError (the caller withholds).
- Nothing deeper than MAX_DEPTH levels is walked: that raises ScanError too.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gitleaks_driver  # noqa: E402

RULES = os.path.join(HERE, "secret-rules.toml")
# Past this many characters a result cannot be scanned inside the hooks' deadlines.
MAX_SCAN_CHARS = 8 * 1024 * 1024
# Each distinct secret is one str.find pass over the text; 1,000 over 8 MB take ~1 s.
MAX_TARGETS = 1000
# Deeper results are withheld unwalked (fail closed): no tool's own output nests near this.
MAX_DEPTH = 100

ScanError = gitleaks_driver.GitleaksError

BINARY_KEYS = frozenset({"base64", "blob", "image_data"})
BINARY_BLOCK_TYPES = frozenset({"image", "audio", "video"})  # MCP blocks whose `data` is binary


def _is_binary_slot(container: dict, key) -> bool:
    return (
        key in BINARY_KEYS
        or (key == "data" and container.get("type") in BINARY_BLOCK_TYPES)
        or (key == "content" and container.get("isBase64") is True)
    )


def map_strings(value, fn, *, keys=lambda key: key, too_deep=None):
    """``value`` with ``fn`` applied to every string value and ``keys`` to every dict key,
    skipping binary slots, as a new value. A subtree past MAX_DEPTH levels becomes
    ``too_deep()``, or raises ScanError when ``too_deep`` is None."""
    # Hand-rolled on purpose: glom and boltons' remap are not importable under the hooks'
    # `python3 -S -E`, and scanning json.dumps(value) instead hides tokens from gitleaks
    # (`\nghp_...` serialises as `\\nghp_...`, so rules anchored on `\b` stop matching).
    def walk(v, depth: int):
        if depth > MAX_DEPTH:
            if too_deep is None:
                raise ScanError(f"the result nests deeper than {MAX_DEPTH} levels")
            return too_deep()
        if isinstance(v, str):
            return fn(v)
        if isinstance(v, list):
            return [walk(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {keys(k): x if _is_binary_slot(v, k) else walk(x, depth + 1) for k, x in v.items()}
        return v

    return walk(value, 0)


def redact_value(value, *, gitleaks: str, timeout: float):
    """A copy of ``value`` with every secret in its strings redacted. Raises ScanError."""
    texts: list[str] = []
    keys: list[str] = []
    map_strings(value, texts.append, keys=keys.append)
    if sum(map(len, texts + keys)) > MAX_SCAN_CHARS:
        raise ScanError(f"the output is past the {MAX_SCAN_CHARS}-character scan limit")
    found = gitleaks_driver.find_secrets(texts + keys, gitleaks=gitleaks, rules=RULES, timeout=timeout)
    rule_of = {target: rule for per_text in found for target, rule in per_text.items()}
    if len(rule_of) > MAX_TARGETS:
        raise ScanError(f"{len(rule_of)} distinct secrets, more than the {MAX_TARGETS} redacted in time")
    if any(target in key for key in keys for target in rule_of):
        raise ScanError("a secret is used as a field name, which redaction does not rewrite")
    return map_strings(value, lambda s: gitleaks_driver.redact(s, rule_of, _marker))


def _marker(rule: str, length: int) -> str:
    return f"[REDACTED:{rule}:{length}chars]"

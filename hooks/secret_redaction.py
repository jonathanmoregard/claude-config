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
- Only string contents change: the result has the input's exact shape.
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
# map_strings recurses once per level; json.loads accepts ~52,000 levels.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 60_000))

ScanError = gitleaks_driver.GitleaksError

BINARY_KEYS = frozenset({"base64", "blob", "image_data"})
BINARY_BLOCK_TYPES = frozenset({"image", "audio", "video"})  # MCP blocks whose `data` is binary


def _is_binary_slot(container: dict, key) -> bool:
    return (
        key in BINARY_KEYS
        or (key == "data" and container.get("type") in BINARY_BLOCK_TYPES)
        or (key == "content" and container.get("isBase64") is True)
    )


def map_strings(value, fn):
    """``value`` with ``fn`` applied to every string outside a binary slot, as a new value."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [map_strings(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: v if _is_binary_slot(value, k) else map_strings(v, fn) for k, v in value.items()}
    return value


def redact_value(value, *, gitleaks: str, timeout: float):
    """A copy of ``value`` with every secret in its strings redacted. Raises ScanError."""
    texts: list[str] = []
    map_strings(value, texts.append)
    if sum(map(len, texts)) > MAX_SCAN_CHARS:
        raise ScanError(f"the output is past the {MAX_SCAN_CHARS}-character scan limit")
    found = gitleaks_driver.find_secrets(texts, gitleaks=gitleaks, rules=RULES, timeout=timeout)
    rule_of = {target: rule for per_text in found for target, rule in per_text.items()}
    if len(rule_of) > MAX_TARGETS:
        raise ScanError(f"{len(rule_of)} distinct secrets, more than the {MAX_TARGETS} redacted in time")
    return map_strings(value, lambda s: _redact(s, rule_of))


def _redact(text: str, rule_of: dict[str, str]) -> str:
    """``text`` with every occurrence of every target replaced by a marker.

    Overlapping occurrences (two findings sharing characters) become one marker,
    so no fragment of either survives between them.
    """
    spans = sorted(((i, i + len(t), rule) for t, rule in rule_of.items() for i in _find_all(text, t)),
                   key=lambda span: (span[0], -span[1]))  # at one start, the longest names the marker
    merged: list[list] = []
    for start, end, rule in spans:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, rule])
    out, pos = [], 0
    for start, end, rule in merged:
        out += [text[pos:start], f"[REDACTED:{rule}:{end - start}chars]"]
        pos = end
    return "".join(out) + text[pos:]


def _find_all(text: str, target: str):
    i = text.find(target)
    while i != -1:
        yield i
        i = text.find(target, i + 1)

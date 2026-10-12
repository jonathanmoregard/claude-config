"""Secret redaction shared by the PostToolUse output hooks.

``redact_value(value, cli=..., timeout=...)`` returns ``value`` with every secret
in its strings replaced by ``[REDACTED:<rule>:<n>chars]``. Detection is the
``secret-redact`` CLI (nixos-config, ``pkgs.secret-redact``): gitleaks with
pinned rules, called once per value.

Invariants:
- Any fault (the CLI missing, failing or timing out) raises ``ScanError``; callers
  WITHHOLD. There is no partial result.
- Each string is scanned and redacted on its own findings (the CLI's contract).
- Binary payloads (image/PDF base64, MCP image data, blobs) are neither scanned nor changed.
- Only string values change: the result has the input's exact shape. Dict keys are
  scanned but never rewritten: a secret in a key raises ScanError (the caller withholds).
- Nothing deeper than MAX_DEPTH levels is walked: that raises ScanError too.
"""
from __future__ import annotations

import json
import subprocess


class ScanError(Exception):
    """The value is NOT known to be clean. Never carries the value's text."""


# Past this many characters a result cannot be scanned inside the hooks' deadlines.
MAX_SCAN_CHARS = 8 * 1024 * 1024
# Deeper results are withheld unwalked (fail closed): no tool's own output nests near this.
MAX_DEPTH = 100

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


def redact_value(value, *, cli: str, timeout: float):
    """A copy of ``value`` with every secret in its strings redacted, by one ``cli`` call
    that must finish within ``timeout`` seconds. Raises ScanError."""
    texts: list[str] = []
    keys: list[str] = []
    map_strings(value, texts.append, keys=keys.append)
    if sum(map(len, texts + keys)) > MAX_SCAN_CHARS:
        raise ScanError(f"the output is past the {MAX_SCAN_CHARS}-character scan limit")
    redacted = _run(cli, texts + keys, timeout)
    if redacted[len(texts):] != keys:
        raise ScanError("a secret is used as a field name, which redaction does not rewrite")
    values = iter(redacted[: len(texts)])
    return map_strings(value, lambda s: next(values))


def _run(cli: str, texts: list[str], timeout: float) -> list[str]:
    """The CLI's redacted texts. Its own timeout ends first; the extra second lets it exit."""
    request = json.dumps({"texts": texts, "timeout": timeout})
    try:
        proc = subprocess.run([cli], input=request.encode(),
                              capture_output=True, timeout=timeout + 1, check=False)
    except subprocess.TimeoutExpired:
        raise ScanError(f"{cli} did not finish in {timeout + 1:.1f}s") from None
    except OSError as e:
        raise ScanError(f"{cli} could not run: {e.strerror or type(e).__name__}") from None
    if proc.returncode != 0:
        reason = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise ScanError(f"{cli} exited {proc.returncode}: {reason}")
    try:
        redacted = json.loads(proc.stdout)["texts"]
    except (ValueError, KeyError, TypeError):
        raise ScanError(f"{cli} wrote no readable result") from None
    if not isinstance(redacted, list) or len(redacted) != len(texts) or not all(
            isinstance(t, str) for t in redacted):
        raise ScanError(f"{cli} returned a result of the wrong shape")
    return redacted

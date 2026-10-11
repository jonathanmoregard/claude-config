"""Secret redaction shared by the PostToolUse output hooks.

``redact_value(value, deadline)`` returns ``value`` with every secret in its
strings replaced by ``[REDACTED:<rule>:<n>chars]``. Two layers, both always on:

1. The floor: regexes for the prefixes this host holds, plus armored private
   keys. Pure Python; it cannot be missing.
2. gitleaks (``gitleaks_driver.py`` with ``secret-rules.toml``), over the
   floor's output.

Invariants:
- A gitleaks fault raises ``ScanError``; callers WITHHOLD. Never "floor only".
- A finding is redacted everywhere its string occurs in the value.
- Redaction is idempotent: a marker is never redacted again.
- Binary payloads (image/PDF base64, MCP image data, blobs) are never touched.
- The result has the input's exact shape: only string contents change.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gitleaks_driver  # noqa: E402

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret-rules.toml")
# Overrides where gitleaks is found (tests point it at a fake or a missing file).
GITLEAKS_ENV = "SECRET_REDACTION_GITLEAKS"
# Past this many characters a result cannot be scanned inside the hooks' deadlines.
MAX_SCAN_BYTES = 8 * 1024 * 1024

ScanError = gitleaks_driver.GitleaksError

# ---- the floor -----------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("anthropic-api", re.compile(r"sk-ant-(?:api|oat)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("openai-project", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai-legacy", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\bgh[psoure]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("slack-token", re.compile(r"\bxox[boaprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws-access", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("tavily-key", re.compile(r"\btvly-[A-Za-z0-9_\-]{20,}\b")),
    ("cloudflare-token", re.compile(r"\bcf(?:ut|at|k)_[A-Za-z0-9]{40,}\b")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{24,}")),
]

# Private keys: find the MARKERS, never a regex spanning a body. The obvious
# BEGIN..END regex is quadratic on BEGINs without an END (616 KB took 44 s),
# and a hook past the harness timeout shows the original output.
# An orphan BEGIN (END truncated away) redacts to the end of the text; an
# orphan END (BEGIN not in view) redacts from the segment start. Both
# over-redact, which is the accepted direction.
_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")
_KEY_END_RE = re.compile(r"-----END (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")


def redact_private_keys(body: str) -> str:
    """Every armored private-key block, terminated or not, in one linear pass."""
    out: list[str] = []
    pos = 0
    begin = _KEY_BEGIN_RE.search(body, pos)
    while True:
        end = _KEY_END_RE.search(body, pos)
        if end is not None and (begin is None or end.start() < begin.start()):
            out.append(f"[REDACTED:private-key:{end.end() - pos}chars]")  # orphan END
            pos = end.end()
            if begin is not None and begin.start() < pos:
                begin = _KEY_BEGIN_RE.search(body, pos)
            continue
        if begin is None:
            out.append(body[pos:])
            return "".join(out)
        out.append(body[pos : begin.start()])
        if end is not None and end.start() < begin.end():
            end = _KEY_END_RE.search(body, begin.end())
        stop = end.end() if end else len(body)  # no END: truncated, redact to the end
        out.append(f"[REDACTED:private-key:{stop - begin.start()}chars]")
        pos = stop
        begin = _KEY_BEGIN_RE.search(body, pos)


def floor_redact(body: str) -> str:
    """Keys first, so no prefix pattern nibbles at a key body before it goes whole."""
    body = redact_private_keys(body)
    for label, pat in SECRET_PATTERNS:
        body = pat.sub(lambda m, _l=label: f"[REDACTED:{_l}:{len(m.group(0))}chars]", body)
    return body


# ---- strings -------------------------------------------------------------
def redact_strings(strings: list[str], deadline: float) -> list[str]:
    """Floor each string, scan them all with gitleaks in one call, redact every finding."""
    floored = [floor_redact(s) for s in strings]
    if sum(len(s) for s in floored) > MAX_SCAN_BYTES:
        raise ScanError(f"the output is past the {MAX_SCAN_BYTES}-character scan limit")
    binary = gitleaks_driver.gitleaks_binary(GITLEAKS_ENV)
    if binary is None:
        raise ScanError("gitleaks is not installed or not on PATH")
    targets: dict[str, str] = {}
    for found in gitleaks_driver.find_secrets(
        floored, rules=RULES_PATH, binary=binary, deadline=deadline
    ):
        for target, rule in found.items():
            targets.setdefault(target, rule)
    if not targets:
        return floored
    pattern = _target_pattern(targets)
    return [pattern.sub(lambda m: f"[REDACTED:{targets[m[0]]}:{len(m[0])}chars]", s) for s in floored]


# Longer targets (a whole key block) skip the trie, whose build recurses per character.
_TRIE_MAX = 400


def _target_pattern(targets) -> "re.Pattern[str]":
    """One regex matching every target, longest match first, in time linear in the text.

    A flat alternation of N literals tries all N at every position (12,000
    findings over 3 MB took past the deadline); factored into a trie, each
    position follows one branch per character.
    """
    trie: dict = {}
    for t in (t for t in targets if len(t) <= _TRIE_MAX):
        node = trie
        for ch in t:
            node = node.setdefault(ch, {})
        node[""] = {}

    def branch(node: dict) -> str:
        alts = [re.escape(ch) + branch(child) for ch, child in node.items() if ch]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{body})?" if "" in node else body  # greedy: the longer target wins

    longest = sorted((t for t in targets if len(t) > _TRIE_MAX), key=len, reverse=True)
    return re.compile("|".join([re.escape(t) for t in longest] + ([branch(trie)] if trie else [])))


# ---- values (tool results) -------------------------------------------------
BINARY_KEYS = frozenset({"base64", "blob", "image_data"})
BINARY_BLOCK_TYPES = frozenset({"image", "audio", "video"})  # MCP blocks whose `data` is binary


def _is_binary_slot(container: dict, key) -> bool:
    return (
        key in BINARY_KEYS
        or (key == "data" and container.get("type") in BINARY_BLOCK_TYPES)
        or (key == "content" and container.get("isBase64") is True)
    )


def map_strings(value, fn):
    """``value`` with ``fn`` applied to every non-binary string, in document order.

    Shape unchanged, ``value`` untouched (containers are copied). Iterative, so
    any depth ``json.loads`` accepts is walked.
    """
    root = [value]
    stack = [(root, 0, False)]  # (container, key, inside a binary slot)
    while stack:
        parent, key, binary = stack.pop()
        item = parent[key]
        if isinstance(item, str):
            if not binary:
                parent[key] = fn(item)
        elif isinstance(item, dict):
            parent[key] = copy = dict(item)
            stack.extend((copy, k, binary or _is_binary_slot(item, k)) for k in reversed(list(copy)))
        elif isinstance(item, list):
            parent[key] = copy = list(item)
            stack.extend((copy, i, binary) for i in reversed(range(len(copy))))
    return root[0]


def redact_value(value, deadline: float):
    """``value`` with every secret in its strings redacted. Raises ScanError."""
    leaves: list[str] = []

    def collect(s: str) -> str:
        leaves.append(s)
        return s

    map_strings(value, collect)
    if not any(s.strip() for s in leaves):
        return value
    redacted = iter(redact_strings(leaves, deadline))
    return map_strings(value, lambda s: next(redacted))

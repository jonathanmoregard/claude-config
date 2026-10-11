"""Shared secret redaction for the PostToolUse output hooks.

Used by ``bash-output-guard.py`` (Bash) and ``tool-output-redact.py`` (every
other tool whose result carries text). One entry point, ``redact_strings``,
takes a batch of strings and returns them with every secret replaced by
``[REDACTED:<rule>:<len>chars]``.

TWO LAYERS, BOTH ALWAYS ON
--------------------------
1. The FLOOR: a handful of Python regexes for the prefixes this host actually
   holds, plus armored private-key blocks. Pure Python, no dependency, cannot
   be missing. It runs first.
2. GITLEAKS (``gitleaks stdin`` with ``secret-rules.toml`` next to this file:
   every default rule of the installed gitleaks, ~220 providers, plus formats
   it lacks). It runs over the floor's output, once per batch, so a tool
   result with forty strings costs one scan, not forty.

The floor is not a fallback for gitleaks. A gitleaks that is missing, exits
with an unexpected status, prints unparseable JSON or overruns the deadline
raises ``ScanError``, and each hook turns that into a WITHHOLD — a broken
scanner never degrades silently to "floor only".

HOW A FINDING IS REDACTED
-------------------------
Each finding's ``Secret`` (its ``Match`` when ``Secret`` is shorter than
``MIN_TARGET_LEN``) is redacted EVERYWHERE it occurs in the batch, not only
at the reported position: the same token pasted twice is the same leak twice,
and gitleaks' line/column report is not reliable enough to slice by (columns
on every line after the first are off by one in 8.30.1, and they are byte
offsets, not characters).

A finding gitleaks made by DECODING (tag ``decoded:base64`` / ``hex`` /
``percent``) carries the decoded secret, which does not occur in the text.
The encoded run on the reported line that decodes to contain it is redacted
instead; if no run on that line can be shown to, the whole line goes.
Over-redaction is the accepted direction here, as everywhere in these hooks.

A finding whose target already contains ``REDACTED`` is a match ON a marker
(``api_key = [REDACTED:...]`` re-read by the generic rule) and is dropped,
which is what makes redaction idempotent.

CHUNKING AND THE DEADLINE
-------------------------
gitleaks costs ~2.3 s per MB of dense JSON-ish text, single-threaded
(measured 2026-10-11, gitleaks 8.30.1). The batch is cut at line boundaries
into ``CHUNK_BYTES`` pieces scanned by up to ``MAX_WORKERS`` gitleaks
processes in parallel, so a large result costs wall time / workers. Every
process gets the time left before the caller's deadline and is killed past
it, which raises ``ScanError`` — the caller withholds rather than let the
harness timeout discard its rewrite (a discarded rewrite shows the ORIGINAL
output). A batch over ``MAX_SCAN_BYTES`` is refused outright for the same
reason: it could not be scanned inside any deadline these hooks run under.

Rules are cut at line boundaries only, so no single-line secret can straddle
two chunks. A multi-line secret (a PEM block) is the floor's job and the
floor scans the whole string.

HERMETIC SCAN
-------------
``--ignore-gitleaks-allow`` because a ``gitleaks:allow`` comment on the same
line suppresses a finding, and the text being scanned is exactly the text an
attacker or a stray file controls. ``--gitleaks-ignore-path`` names a path
that does not exist and the process runs in ``/``, so a ``.gitleaksignore``
in the session's cwd cannot suppress findings either (gitleaks reads one from
``.`` by default). ``--config`` is explicit, so ``GITLEAKS_CONFIG`` in the
environment cannot replace the rules.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

# base64/binascii/urllib.parse (decoded findings only) and concurrent.futures
# (multi-chunk scans only) are imported where they are used: together they
# cost ~150 ms of interpreter start-up on EVERY tool call, measured, for paths
# almost no call takes.

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "secret-rules.toml")

# Where gitleaks lives when the hook's PATH does not say. Headless agents
# started from systemd units run with a trimmed PATH, and a gitleaks that
# resolves in an interactive shell but not there would withhold every tool
# result those agents read. Checked in order after PATH.
GITLEAKS_FALLBACKS = (
    "/etc/profiles/per-user/{user}/bin/gitleaks",
    "{home}/.nix-profile/bin/gitleaks",
    "/run/current-system/sw/bin/gitleaks",
    "/usr/local/bin/gitleaks",
    "/usr/bin/gitleaks",
)

CHUNK_BYTES = 256 * 1024
MAX_WORKERS = max(1, min(8, os.cpu_count() or 1))
# 8 MiB at ~2.3 s/MB over 8 workers is ~2.5 s of wall time: inside the
# tighter of the two hooks' deadlines (6 s, the Bash guard) with room for
# Python. Real tool results are capped by the CLI far below this.
MAX_SCAN_BYTES = 8 * 1024 * 1024

# A Secret shorter than this is redacted via its Match instead: replacing a
# short string "everywhere" would hit unrelated text.
MIN_TARGET_LEN = 8

MARKER_WORD = "REDACTED"

# Must NOT exist: gitleaks silently skips a missing ignore path, and any real
# file here could suppress findings.
NO_IGNORE_FILE = "/nonexistent/secret-redaction-has-no-gitleaksignore"


class ScanError(Exception):
    """The scanner could not vouch for the batch. Callers WITHHOLD."""


# ----------------------------------------------------------------------
# Layer 1: the floor
# ----------------------------------------------------------------------
# High-entropy secret prefixes. Prefixes only — generic high-entropy
# matching is too false-positive prone (hashes, nix store paths, base64).
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
    # Formats gitleaks' defaults lack, mirrored from secret-rules.toml so the
    # floor still holds them on its own. Bodies are loose on purpose.
    ("cloudflare-token", re.compile(r"\bcf(?:ut|at|k)_[A-Za-z0-9]{40,}\b")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{24,}")),
]


# ---- armored private keys: MARKERS matched, bodies never spanned ----------
#
# WHY THIS IS NOT A REGEX. The first version of this was the obvious pair —
# `BEGIN...-----[\s\S]+?-----END...` plus a greedy `[\s\S]*` catcher for the
# truncated case — and it was QUADRATIC. Each BEGIN with no END behind it makes
# the engine scan to end-of-string before failing, so cost grows with
# (BEGIN count x body length). MEASURED on this host, terminated pattern alone:
#
#   154 KB / 2000 BEGINs ->  2.6 s
#   308 KB / 4000 BEGINs -> 11.9 s
#   616 KB / 8000 BEGINs -> 44.2 s
#
# A hook overrunning the harness timeout has its output DISCARDED, so the
# result reaches the model unrewritten — and a SIGALRM watchdog cannot save
# it, because CPython runs a Python-level signal handler only when the
# interpreter regains control and `re` does not return to it mid-match. A body
# shaped like this is therefore a fail-OPEN, reachable by any command whose
# output an attacker influences. The bound must come from the algorithm.
#
# So: find MARKERS (bounded, no body spanning, linear), then slice between
# them with str.find. One pass, no backtracking, no dependency.
#
# TRUNCATION IS SYMMETRIC, AND SO IS THE CONTRACT. Redaction runs AFTER the
# harness truncates, and a cut can land on either side of a block:
#
#   * orphan BEGIN — the END was truncated away — redact from the BEGIN
#     marker to end-of-string;
#   * orphan END — the BEGIN is not in view: the tail of a key file,
#     `grep -A`, head-side truncation — redact from the start of the
#     current unredacted segment through the END marker.
#
# Both orphan rules OVER-redact. That is the accepted direction: a false
# positive costs the reader a visible [REDACTED] marker and a re-run, a false
# negative is a leaked key.
#
# `(?: BLOCK)?` covers PGP armor (`-----BEGIN PGP PRIVATE KEY BLOCK-----`);
# `[A-Z0-9 ]*` covers PKCS#8, ENCRYPTED, RSA, DSA, EC, OPENSSH and SSH2.
# KNOWN, DELIBERATE SCOPE: the 4-dash spaced SSH2 form and PuTTY's
# `PuTTY-User-Key-File-2:` header are different shapes and are NOT matched.
_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")
_KEY_END_RE = re.compile(r"-----END (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")


def redact_private_keys(body: str) -> str:
    """Replace every armored private-key block, terminated or not, in ONE pass.

    LINEARITY is load-bearing — the quadratic regex this replaced was a
    fail-open (see above). ``pos`` only moves forward, and the next BEGIN is
    CACHED and re-searched only once the scan has consumed it.
    """
    out: list[str] = []
    pos = 0
    begin = _KEY_BEGIN_RE.search(body, pos)
    while True:
        end = _KEY_END_RE.search(body, pos)
        if end is not None and (begin is None or end.start() < begin.start()):
            # Orphaned END: the bytes in front of the marker are the tail of
            # a key whose head is not in this body.
            out.append(f"[REDACTED:private-key:{end.end() - pos}chars]")
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
        # No END means truncation cut it off: everything from BEGIN onward goes.
        stop = end.end() if end else len(body)
        out.append(f"[REDACTED:private-key:{stop - begin.start()}chars]")
        pos = stop
        begin = _KEY_BEGIN_RE.search(body, pos)


def floor_redact(body: str) -> str:
    """The always-on regex layer. Keys first, so no prefix pattern nibbles
    at the interior of a key block before it is replaced whole."""
    body = redact_private_keys(body)
    for label, pat in SECRET_PATTERNS:
        body = pat.sub(
            lambda m, _label=label: f"[REDACTED:{_label}:{len(m.group(0))}chars]",
            body,
        )
    return body


# ----------------------------------------------------------------------
# Layer 2: gitleaks
# ----------------------------------------------------------------------
def gitleaks_binary() -> str | None:
    found = shutil.which("gitleaks")
    if found:
        return found
    user = os.environ.get("USER") or ""
    home = os.path.expanduser("~")
    for template in GITLEAKS_FALLBACKS:
        path = template.format(user=user, home=home)
        if "{" not in path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _chunks(text: str) -> list[str]:
    """Cut at line boundaries into pieces of about CHUNK_BYTES.

    Measured in characters: a cheap bound, and gitleaks' cost tracks it.
    A single line longer than the chunk size stays whole.
    """
    if len(text) <= CHUNK_BYTES:
        return [text]
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        stop = start + CHUNK_BYTES
        if stop >= n:
            out.append(text[start:])
            break
        nl = text.rfind("\n", start, stop)
        if nl <= start:
            nl = text.find("\n", stop)
            if nl == -1:
                out.append(text[start:])
                break
        out.append(text[start:nl])
        start = nl + 1
    return out


def _scan_chunk(binary: str, chunk: str, deadline: float) -> list[dict]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScanError("the deadline passed before the secret scan could start")
    cmd = [
        binary, "stdin",
        "--no-banner",
        "--config", RULES_PATH,
        "--report-path", "/dev/stdout",
        "--report-format", "json",
        "--log-level", "error",
        "--ignore-gitleaks-allow",
        "--gitleaks-ignore-path", NO_IGNORE_FILE,
        "--exit-code", "1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=chunk.encode("utf-8", "surrogatepass"),
            capture_output=True,
            timeout=remaining,
            cwd="/",
        )
    except subprocess.TimeoutExpired as exc:
        raise ScanError(
            f"gitleaks did not finish inside the hook's deadline ({len(chunk)} chars)"
        ) from exc
    except OSError as exc:
        raise ScanError(f"gitleaks could not be started ({type(exc).__name__}: {exc})") from exc
    if proc.returncode not in (0, 1):
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScanError(
            f"gitleaks exited {proc.returncode}: {(err[-1] if err else '')[:300]}"
        )
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        if proc.returncode == 1:
            raise ScanError("gitleaks reported findings but printed no report")
        return []
    try:
        findings = json.loads(raw)
    except ValueError as exc:
        raise ScanError(f"gitleaks printed a report that is not JSON ({exc})") from exc
    if not isinstance(findings, list):
        raise ScanError("gitleaks printed a report that is not a JSON list")
    if proc.returncode == 1 and not findings:
        raise ScanError("gitleaks exited 1 (leaks found) with an empty report")
    for f in findings:
        if isinstance(f, dict):
            f["_chunk"] = chunk
    return [f for f in findings if isinstance(f, dict)]


def gitleaks_findings(text: str, deadline: float) -> list[dict]:
    """Every gitleaks finding in ``text``, or ``ScanError``."""
    if not text.strip():
        return []
    if len(text) > MAX_SCAN_BYTES:
        raise ScanError(
            f"the output is {len(text)} characters, past the {MAX_SCAN_BYTES}-character "
            "limit the secret scan can cover inside the hook's deadline"
        )
    if not os.path.isfile(RULES_PATH):
        raise ScanError(f"the gitleaks rules file is missing ({RULES_PATH})")
    binary = gitleaks_binary()
    if binary is None:
        raise ScanError("gitleaks is not installed or not on PATH")
    if os.path.exists(NO_IGNORE_FILE):
        raise ScanError(f"{NO_IGNORE_FILE} exists and could suppress findings")
    chunks = _chunks(text)
    if len(chunks) == 1:
        return _scan_chunk(binary, chunks[0], deadline)
    import concurrent.futures

    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = [pool.submit(_scan_chunk, binary, c, deadline) for c in chunks]
        for fut in futures:
            out.extend(fut.result())
    return out


# ---- decoded findings ------------------------------------------------
_ENCODED_RUN_RE = re.compile(r"[A-Za-z0-9+/=_%\-]{16,}")


def _decodings(run: str, depth: int = 0) -> list[str]:
    """Every plausible decoding of an encoded run, recursively (gitleaks
    itself decodes up to five levels deep)."""
    import base64
    import binascii
    import urllib.parse

    if depth >= 5:
        return []
    found: list[str] = []
    candidates: list[bytes] = []
    padded = run + "=" * (-len(run) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            candidates.append(decoder(padded))
        # arftl-allow: swallowed-exception — a run that is not valid base64 is simply not a base64 candidate; other decodings and the whole-line fallback still apply.
        except (binascii.Error, ValueError):
            continue
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", run):
        try:
            candidates.append(bytes.fromhex(run))
        # arftl-allow: swallowed-exception — not valid hex means not a hex candidate; the fullmatch above makes this unreachable in practice.
        except ValueError:
            candidates.append(b"")
    if "%" in run:
        candidates.append(urllib.parse.unquote(run).encode("utf-8", "replace"))
    for raw in candidates:
        text = raw.decode("utf-8", "replace")
        found.append(text)
        for inner in _ENCODED_RUN_RE.findall(text):
            found.extend(_decodings(inner, depth + 1))
    return found


def _decoded_targets(finding: dict) -> list[str]:
    """The text to redact for a finding gitleaks made by decoding."""
    secret = finding.get("Secret") or finding.get("Match") or ""
    chunk = finding.get("_chunk") or ""
    try:
        line_no = int(finding.get("StartLine") or 0)
    except (TypeError, ValueError):
        line_no = 0
    lines = chunk.split("\n")
    if not 1 <= line_no <= len(lines):
        # Cannot place it. Every encoded run that decodes to it, anywhere.
        candidates = lines
    else:
        candidates = [lines[line_no - 1]]
    targets: list[str] = []
    for line in candidates:
        for run in _ENCODED_RUN_RE.findall(line):
            if secret and any(secret in d for d in _decodings(run)):
                targets.append(run)
    if targets:
        return targets
    if 1 <= line_no <= len(lines) and len(lines[line_no - 1]) >= MIN_TARGET_LEN:
        return [lines[line_no - 1]]
    raise ScanError(
        "gitleaks found an encoded secret this redactor could not locate in the text"
    )


def _targets(findings: list[dict]) -> dict[str, str]:
    """``{text_to_redact: rule_id}`` for every finding."""
    targets: dict[str, str] = {}
    for f in findings:
        rule = str(f.get("RuleID") or "gitleaks")
        rule = re.sub(r"[^A-Za-z0-9_.\-]", "-", rule)[:64]
        tags = f.get("Tags") or []
        if any(isinstance(t, str) and t.startswith("decoded:") for t in tags):
            found = _decoded_targets(f)
        else:
            secret = f.get("Secret") or ""
            if len(secret) < MIN_TARGET_LEN:
                secret = f.get("Match") or secret
            found = [secret] if secret else []
        for t in found:
            if not t or MARKER_WORD in t:
                continue
            targets.setdefault(t, rule)
    return targets


def _apply(text: str, targets: dict[str, str], pattern) -> str:
    if pattern is None:
        return text
    return pattern.sub(
        lambda m: f"[REDACTED:{targets[m.group(0)]}:{len(m.group(0))}chars]", text
    )


def redact_strings(strings: list[str], deadline: float) -> list[str]:
    """Floor, then ONE gitleaks scan over the batch, then apply its findings.

    Raises ``ScanError`` if gitleaks cannot vouch for the batch; the caller
    withholds. The output list is the same length, order and type as the input.
    """
    floored = [floor_redact(s) for s in strings]
    findings = gitleaks_findings("\n".join(floored), deadline)
    if not findings:
        return floored
    targets = _targets(findings)
    if not targets:
        return floored
    # One alternation, longest first, applied in ONE pass: a marker this pass
    # inserts is never re-scanned by a later target.
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(targets, key=len, reverse=True))
    )
    return [_apply(s, targets, pattern) for s in floored]


def redact_text(text: str, deadline: float) -> str:
    return redact_strings([text], deadline)[0]

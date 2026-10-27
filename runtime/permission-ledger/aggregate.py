#!/usr/bin/env python3
"""Stage 2 of the permission friction ledger: deterministic aggregator.

Reads the JSONL ledger written by ``hook.py``, joins denials onto requests,
groups by ``pattern_key``, drops noise and already-decided patterns, runs the
sampled inputs through the thorough secret/injection scanners, and prints a
small bounded JSON digest to stdout.

No model is involved here. That is the point: the nightly evaluator sees a
scanned, capped document instead of raw ledger lines.

Everything in the ledger is untrusted data. This module never interprets a
field as an instruction, never executes anything derived from a record, and
never writes to the ledger directory.

Usage:
    python3 aggregate.py [--ledger-dir DIR] [--days N] [--min-count N]
                         [--decided PATH] [--scan-script PATH] [--no-scan]

Exit codes:
    0 = digest written to stdout (including the empty-ledger case)
    2 = unusable arguments (e.g. --days 0)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Matches hook.py: the ledger is state, and ~/.claude is read-only under the
# runtime projection.
DEFAULT_LEDGER_DIR = "~/.local/state/permission-ledger"

# Ordered candidates for RSI's scan_content.py; the first one that exists wins.
# There is no single canonical location: a direct RSI install puts it under
# ~/.claude/recursive-self-improvement/, while installing the plugin from the
# marketplace puts it under ~/.claude/plugins/marketplaces/<owner>/plugins/.
# Pointing at only the first (which does not exist on this machine) made stage-2
# scanning silently do nothing. An explicit --scan-script still overrides.
DEFAULT_SCAN_SCRIPT_CANDIDATES = (
    "~/.claude/recursive-self-improvement/scripts/scan_content.py",
    "~/.claude/plugins/marketplaces/jonathanmoregard/plugins"
    "/recursive-self-improvement/scripts/scan_content.py",
)
NO_SCAN_SCRIPT_STATUS = "unavailable: no scan_content.py found at any known path"

DEFAULT_DAYS = 7
DEFAULT_MIN_COUNT = 3

MAX_GROUPS = 40
MAX_SAMPLES = 5
MAX_SAMPLE_CHARS = 400
MAX_SUGGESTIONS = 10
TRUNCATION_MARKER = "…[truncated]"
SECRET_PLACEHOLDER = "[REDACTED:detect-secrets]"

REASK_MULTIPLIER = 3
SUPPRESSING_DECISIONS = ("rejected", "implemented")

SCAN_CONTENT_TIMEOUT = 120
# scan_content.py prints this on stderr when it exits 1 for a detection.
SCAN_VERDICT = "Prompt injection detected"
DETECT_SECRETS_TIMEOUT = 30

MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")

DIGEST_TOP_LEVEL_KEYS = [
    "generated_at",
    "window_days",
    "total_records",
    "malformed_lines",
    "scan_status",
    "groups",
]

DIGEST_GROUP_KEYS = [
    "pattern_key",
    "count",
    "approved",
    "denied",
    "projects",
    "sessions",
    "agent_types",
    "permission_modes",
    "entrypoints",
    "tool_names",
    "first_seen",
    "last_seen",
    "samples",
    "suggestions",
    "protected_path",
    "reask",
]
# "prior_decision" is added only on groups where reask is true.
# "injection_flagged" (always true), "injection_score" (float, or None when
# the scanner could not attribute its verdict) and "injection_flagged_samples"
# (indices into "samples") are added only on groups the injection pass flagged.
INJECTION_GROUP_KEYS = [
    "injection_flagged",
    "injection_score",
    "injection_flagged_samples",
]


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def parse_ts(value):
    """Parse an RFC3339 timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def month_files_in_window(ledger_dir, cutoff):
    """Month files whose month could hold a record at or after `cutoff`.

    Months strictly before the cutoff's month cannot contain in-window
    records and are not opened at all. Later months are always included, so
    clock skew or a future-dated record never silently drops data.
    """
    try:
        names = os.listdir(ledger_dir)
    except OSError:
        return []
    floor = (cutoff.year, cutoff.month)
    picked = []
    for name in names:
        m = MONTH_FILE_RE.match(name)
        if not m:
            continue
        if (int(m.group(1)), int(m.group(2))) < floor:
            continue
        picked.append(os.path.join(ledger_dir, name))
    return sorted(picked)


def _usable_record(obj):
    """True if the parsed line has the fields the aggregator needs."""
    if not isinstance(obj, dict):
        return False
    if obj.get("event") not in ("request", "denied"):
        return False
    if not isinstance(obj.get("pattern_key"), str) or not obj["pattern_key"]:
        return False
    return parse_ts(obj.get("ts")) is not None


def load_records(ledger_dir, cutoff):
    """Return (in-window records, malformed line count).

    A corrupt ledger must never abort the run: unreadable files and
    unparseable lines are skipped, the lines counted.
    """
    records = []
    malformed = 0
    for path in month_files_in_window(ledger_dir, cutoff):
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue  # blank padding is not a malformed record
                    try:
                        obj = json.loads(line)
                    except (ValueError, RecursionError):
                        malformed += 1
                        continue
                    if not _usable_record(obj):
                        malformed += 1
                        continue
                    ts = parse_ts(obj["ts"])
                    if ts < cutoff:
                        continue
                    obj["_ts"] = ts
                    records.append(obj)
        except OSError:
            continue
    records.sort(key=lambda r: r["_ts"])
    return records, malformed


# --------------------------------------------------------------------------
# join + grouping
# --------------------------------------------------------------------------


def _join_key(rec):
    return (rec.get("session_id"), rec.get("input_digest"))


def join_outcomes(records):
    """Turn raw records into occurrences with an approved/denied outcome.

    A request is denied if a `denied` record in the same session carries the
    same `input_digest`; denials are consumed one-for-one so three identical
    requests plus one denial are 2 approved + 1 denied. A denial with no
    matching request is still friction, so it becomes its own occurrence and
    counts as denied.
    """
    denials = {}
    for rec in records:
        if rec.get("event") == "denied":
            denials.setdefault(_join_key(rec), []).append(rec)

    consumed = {}
    occurrences = []
    for rec in records:
        if rec.get("event") != "request":
            continue
        key = _join_key(rec)
        available = len(denials.get(key, ()))
        used = consumed.get(key, 0)
        if used < available:
            consumed[key] = used + 1
            occurrences.append((rec, "denied"))
        else:
            occurrences.append((rec, "approved"))

    for key, recs in denials.items():
        for rec in recs[consumed.get(key, 0):]:
            occurrences.append((rec, "denied"))

    occurrences.sort(key=lambda pair: pair[0]["_ts"])
    return occurrences


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sample_text(tool_input):
    """Serialize a redacted tool_input into one sample string, or None."""
    if tool_input is None:
        return None
    if isinstance(tool_input, str):
        text = tool_input
    else:
        try:
            text = _canonical(tool_input)
        except (TypeError, ValueError):
            text = repr(tool_input)
    text = text.strip()
    return text or None


def truncate_sample(text):
    if len(text) <= MAX_SAMPLE_CHARS:
        return text
    return text[:MAX_SAMPLE_CHARS] + TRUNCATION_MARKER


def _dedupe(seq):
    seen = set()
    out = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


PROTECTED_COMPONENTS = (".claude", ".git")


def is_protected_path(pattern_key):
    """True when a path-keyed group sits under a `.claude` or `.git` component.

    Claude Code gates those directories in a layer outside the allow/deny
    lists, so no allow rule silences the prompt. The user config root
    `~/.claude` itself is measured NOT to be gated, so it is stripped before
    the component test; a nested `.claude`/`.git` below it still counts.
    """
    _, sep, path = pattern_key.partition(":")
    if not sep or not path.startswith("/"):
        return False
    config_root = os.path.join(os.path.expanduser("~"), ".claude")
    if path == config_root or path.startswith(config_root + "/"):
        path = path[len(config_root):]
    return any(part in PROTECTED_COMPONENTS for part in path.split("/"))


def build_groups(occurrences):
    """Group occurrences by pattern_key into digest-shaped dicts."""
    acc = {}
    for rec, outcome in occurrences:
        key = rec["pattern_key"]
        g = acc.get(key)
        if g is None:
            g = acc[key] = {
                "pattern_key": key,
                "count": 0,
                "approved": 0,
                "denied": 0,
                "projects": set(),
                "session_ids": set(),
                "agent_types": set(),
                "permission_modes": set(),
                "entrypoints": set(),
                "tool_names": set(),
                "first_seen": rec["ts"],
                "last_seen": rec["ts"],
                "_first_dt": rec["_ts"],
                "_last_dt": rec["_ts"],
                "samples": [],
                "_sample_seen": set(),
                "suggestions": [],
                "_suggestion_seen": set(),
            }
        g["count"] += 1
        g[outcome] += 1
        for field, bucket in (
            ("project", "projects"),
            ("agent_type", "agent_types"),
            ("permission_mode", "permission_modes"),
            ("entrypoint", "entrypoints"),
            ("tool_name", "tool_names"),
        ):
            value = rec.get(field)
            if isinstance(value, str) and value:
                g[bucket].add(value)
        session = rec.get("session_id")
        if isinstance(session, str) and session:
            g["session_ids"].add(session)
        if rec["_ts"] < g["_first_dt"]:
            g["_first_dt"], g["first_seen"] = rec["_ts"], rec["ts"]
        if rec["_ts"] > g["_last_dt"]:
            g["_last_dt"], g["last_seen"] = rec["_ts"], rec["ts"]

        if len(g["samples"]) < MAX_SAMPLES:
            text = _sample_text(rec.get("tool_input"))
            if text is not None and text not in g["_sample_seen"]:
                g["_sample_seen"].add(text)
                g["samples"].append(text)

        suggestions = rec.get("suggestions")
        if isinstance(suggestions, list):
            for suggestion in suggestions:
                if len(g["suggestions"]) >= MAX_SUGGESTIONS:
                    break
                try:
                    sig = _canonical(suggestion)
                except (TypeError, ValueError):
                    continue
                if sig in g["_suggestion_seen"]:
                    continue
                g["_suggestion_seen"].add(sig)
                g["suggestions"].append(suggestion)

    groups = []
    for g in acc.values():
        groups.append(
            {
                "pattern_key": g["pattern_key"],
                "count": g["count"],
                "approved": g["approved"],
                "denied": g["denied"],
                "projects": sorted(g["projects"]),
                "sessions": len(g["session_ids"]),
                "agent_types": sorted(g["agent_types"]),
                "permission_modes": sorted(g["permission_modes"]),
                "entrypoints": sorted(g["entrypoints"]),
                "tool_names": sorted(g["tool_names"]),
                "first_seen": g["first_seen"],
                "last_seen": g["last_seen"],
                "samples": _dedupe(truncate_sample(s) for s in g["samples"]),
                "suggestions": g["suggestions"],
                "protected_path": is_protected_path(g["pattern_key"]),
                "reask": False,
            }
        )
    groups.sort(key=lambda x: (-x["count"], x["pattern_key"]))
    return groups


# --------------------------------------------------------------------------
# decided.json
# --------------------------------------------------------------------------


def load_decided(path):
    """Read decided.json. Missing or malformed is treated as empty.

    Empty is not harmless — every already-rejected pattern returns as a fresh
    proposal — but this stays silent on purpose. The contract is one JSON
    document on stdout and nothing on stderr, and a caller that cares which
    case it got can look at the path it passed in.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def apply_decided(groups, decided):
    """Drop groups already decided, unless friction has tripled since then.

    A `rejected` or `implemented` pattern is suppressed. It earns exactly one
    re-ask once its current count reaches 3x the count recorded at decision
    time; the re-emitted group carries `reask: true` plus the prior decision.
    An entry with a missing or non-numeric `count_at_decision` can never
    trigger a re-ask (there is no baseline to triple), so it stays suppressed.
    """
    kept = []
    for group in groups:
        entry = decided.get(group["pattern_key"])
        if not entry:
            kept.append(group)
            continue
        decision = entry.get("decision")
        if decision not in SUPPRESSING_DECISIONS:
            kept.append(group)
            continue
        baseline = entry.get("count_at_decision")
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
            continue
        if baseline <= 0:
            continue
        if group["count"] >= REASK_MULTIPLIER * baseline:
            group["reask"] = True
            group["prior_decision"] = entry
            kept.append(group)
    return kept


PENDING_STATUSES = ("pending", "open")


def load_pending_keys(pending_dir):
    """pattern_keys of undecided proposals in the sink's permissions dir.

    A pattern that already waits in the review queue is suppressed like a
    decided one: re-evaluating it nightly only mints duplicate proposals.
    Top-level `*.md` files only (README*/dotfiles/archived/ skipped); a file
    with no `status:` line counts as pending, matching the SessionStart count.
    """
    keys = set()
    try:
        names = os.listdir(pending_dir)
    except OSError:
        return keys
    for name in names:
        if not name.endswith(".md") or name.startswith((".", "README")):
            continue
        try:
            with open(os.path.join(pending_dir, name), "r", encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        if not lines or lines[0].strip() != "---":
            continue
        status, key = None, None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            field, sep, value = line.partition(":")
            if not sep or field != field.strip():
                continue
            value = value.strip()
            if field == "status":
                status = value
            elif field == "pattern_key":
                if value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        continue
                key = value
        if key and (status is None or status in PENDING_STATUSES):
            keys.add(key)
    return keys


# --------------------------------------------------------------------------
# scanning (best effort, never fatal)
# --------------------------------------------------------------------------


def resolve_scan_script(candidates=None):
    """First existing candidate path wins; None when none of them exists.

    Only used when --scan-script was not given: an explicit path is taken as
    the caller's decision, existing or not, so a typo is reported rather than
    silently swapped for a different scanner.
    """
    for candidate in candidates if candidates is not None else DEFAULT_SCAN_SCRIPT_CANDIDATES:
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return path
    return None


def _all_samples(groups):
    return [s for g in groups for s in g["samples"]]


# One JSON string literal; the closing quote may be missing when the sample
# was truncated mid-string.
_JSON_STRING = re.compile(r'"((?:[^"\\]|\\.)*)("?)', re.DOTALL)
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.-]*\Z")
# prompt-injection-scan scores documents separated by such lines one by one.
_DOC_SEPARATOR = re.compile(r"^[ \t]*---[ \t]*$", re.MULTILINE)


def scan_text(sample):
    """The part of a sample a prompt-injection classifier should judge.

    Samples are canonical JSON (possibly truncated). The classifier is
    trained on prose, and JSON keys and punctuation alone push it toward
    "injection" on plain file reads, so it gets the string VALUES, unescaped,
    one per line. A sample that is not a JSON object/array passes through.
    """
    text = sample
    if text.endswith(TRUNCATION_MARKER):
        text = text[: -len(TRUNCATION_MARKER)]
    if not text.startswith(("{", "[")):
        return text
    values = []
    for m in _JSON_STRING.finditer(text):
        literal = m.group(1)
        try:
            value = json.loads('"%s"' % literal)
        except ValueError:
            # Truncation cut an escape sequence in half; keep the raw text.
            value = literal
        if m.group(2) and text[m.end():].lstrip().startswith(":"):
            if _IDENTIFIER.match(value):
                continue  # a structural key (file_path, command, ...)
        values.append(value)
    return "\n".join(v for v in values if v.strip())


def _scan_document(text):
    """A sample as one scanner document: it may not draw `---` boundaries."""
    return _DOC_SEPARATOR.sub("- - -", text)


def _attribution(stdout, n_samples):
    """{sample index: score} from scan_content's --json verdict, or None.

    None whenever the verdict cannot be trusted to name the samples: not
    JSON, a document count that does not match what was sent, an index out
    of range, or a detection that names no document at all.
    """
    try:
        verdict = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(verdict, dict) or verdict.get("documents") != n_samples:
        return None
    flagged, scores = verdict.get("flagged"), verdict.get("scores")
    if not isinstance(flagged, list) or not flagged:
        return None
    out = {}
    for i in flagged:
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n_samples:
            return None
        score = scores[i] if isinstance(scores, list) and len(scores) == n_samples else None
        out[i] = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return out


def _mark_flagged(groups, attribution):
    """Put the verdict on the groups (and sample indices) that carried it.

    attribution None means "flagged, but not attributable": every group is
    marked, so the conservative reading is the default one.
    """
    flat = [(g, j) for g in groups for j in range(len(g["samples"]))]
    for gi, group in enumerate(groups):
        if attribution is None:
            hits = {}
        else:
            hits = {j: attribution[k] for k, (g, j) in enumerate(flat)
                    if g is group and k in attribution}
            if not hits:
                continue
        scores = [v for v in hits.values() if v is not None]
        group["injection_flagged"] = True
        group["injection_score"] = round(max(scores), 4) if scores else None
        group["injection_flagged_samples"] = sorted(hits)


def run_injection_scan(groups, scan_script):
    """Run RSI's scan_content.py over the samples about to be emitted.

    Returns a status string, and marks each group holding a flagged sample
    (see INJECTION_GROUP_KEYS). Samples are always emitted verbatim: the
    stage 3 prompt has to *see* a directive-shaped payload to classify the
    group `sensitive`, so this pass reports rather than rewrites.
    """
    samples = _all_samples(groups)
    if not samples:
        return "skipped (no samples)"
    if not scan_script:
        return NO_SCAN_SCRIPT_STATUS
    if not os.path.isfile(scan_script):
        return "unavailable: no scanner script at %s" % scan_script
    payload = "\n---\n".join(_scan_document(scan_text(s)) for s in samples)
    # Over stdin: one argv string caps at 128 KiB and cannot hold a NUL.
    data = payload.encode("utf-8", "replace")
    try:
        # Own session, so a timeout kills the scanner's children too (the
        # classifier it runs would otherwise outlive it, model in memory).
        proc = subprocess.Popen(
            [sys.executable, scan_script, "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return "error: %s: %s" % (type(exc).__name__, exc)
    try:
        stdout, stderr = proc.communicate(input=data, timeout=SCAN_CONTENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        # Not str(exc): that quotes the argv, i.e. the samples themselves.
        return "error: scanner timed out after %ds; samples emitted unscanned" % (
            SCAN_CONTENT_TIMEOUT
        )
    if proc.returncode == 0:
        return "ok"
    # A traceback exits 1 too: only exit 1 WITH the scanner's verdict line is
    # a detection. Anything else is a failed scan and must not read as "ok".
    if proc.returncode == 1 and SCAN_VERDICT in stderr.decode("utf-8", "replace"):
        attribution = _attribution(stdout, len(samples))
        _mark_flagged(groups, attribution)
        if attribution is None:
            return (
                "ok (injection patterns flagged in samples, but the scanner did "
                "not say which, so every group carries injection_flagged: true; "
                "samples emitted verbatim as inert data)"
            )
        marked = sum(1 for g in groups if g.get("injection_flagged"))
        return (
            "ok (injection patterns flagged in %d of %d samples, in %d of %d "
            "groups; those groups carry injection_flagged: true; samples "
            "emitted verbatim as inert data)"
            % (len(attribution), len(samples), marked, len(groups))
        )
    if proc.returncode == 2:
        return "degraded: injection scanner unavailable (exit 2); samples emitted unscanned"
    return "error: scanner exited %d; samples emitted unscanned" % proc.returncode


_DETECTOR_HIT = re.compile(r":\s*True\b")


def _detect_secrets_hit(binary, text):
    # --no-verify: by default 1.5.0 sends each hit to its issuer (AWS STS,
    # Slack auth.test, ...) and prints `False` when that fails — a revoked key
    # or no network then passes through unredacted, and the nightly job would
    # be shipping the user's credentials over the wire to check them.
    proc = subprocess.run(
        [binary, "scan", "--no-verify", "--string", text],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=DETECT_SECRETS_TIMEOUT,
    )
    out = proc.stdout.decode("utf-8", "replace")
    # 1.5.0 prints `AWSKeyDetector          : True  (unverified)` — padded,
    # with a qualifier after True — so match the field, not the line end.
    return any(_DETECTOR_HIT.search(line) for line in out.splitlines())


def run_secret_scan(groups):
    """Replace any sample `detect-secrets` flags with a placeholder."""
    samples = _all_samples(groups)
    if not samples:
        return "skipped (no samples)"
    binary = shutil.which("detect-secrets")
    if not binary:
        return "unavailable: detect-secrets not on PATH; samples emitted unscanned"
    redacted = 0
    try:
        for group in groups:
            cleaned = []
            for sample in group["samples"]:
                if _detect_secrets_hit(binary, sample):
                    redacted += 1
                    cleaned.append(SECRET_PLACEHOLDER)
                else:
                    cleaned.append(sample)
            group["samples"] = _dedupe(cleaned)
    except (OSError, subprocess.SubprocessError) as exc:
        return "error: %s: %s; remaining samples emitted unscanned" % (
            type(exc).__name__,
            exc,
        )
    if redacted:
        return "ok (redacted %d sample(s) flagged as containing secrets)" % redacted
    return "ok"


def scan_samples(groups, no_scan, scan_script):
    if no_scan:
        return {
            "scan_content": "skipped (--no-scan)",
            "detect_secrets": "skipped (--no-scan)",
        }
    # Secrets first: redaction replaces and dedupes samples, and the
    # injection pass's per-sample indices must point at what is emitted.
    detect_secrets = run_secret_scan(groups)
    return {
        "scan_content": run_injection_scan(groups, scan_script),
        "detect_secrets": detect_secrets,
    }


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------


def build_digest(
    ledger_dir,
    days=DEFAULT_DAYS,
    min_count=DEFAULT_MIN_COUNT,
    decided_path=None,
    scan_script=None,
    no_scan=False,
    now=None,
    pending_dir=None,
):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    records, malformed = load_records(ledger_dir, cutoff)
    groups = build_groups(join_outcomes(records))
    groups = [g for g in groups if g["count"] >= min_count]
    groups = apply_decided(groups, load_decided(decided_path) if decided_path else {})
    if pending_dir:
        pending = load_pending_keys(pending_dir)
        groups = [g for g in groups if g["pattern_key"] not in pending]
    groups = groups[:MAX_GROUPS]
    scan_status = scan_samples(groups, no_scan, scan_script)

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S.")
        + "%03dZ" % (now.microsecond // 1000),
        "window_days": days,
        "total_records": len(records),
        "malformed_lines": malformed,
        "scan_status": scan_status,
        "groups": groups,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Aggregate the permission friction ledger into a JSON digest."
    )
    parser.add_argument("--ledger-dir", default=DEFAULT_LEDGER_DIR)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT)
    parser.add_argument(
        "--decided", default=None, help="default: <ledger-dir>/decided.json"
    )
    parser.add_argument(
        "--pending-dir",
        default=None,
        help="proposals dir; groups with an undecided proposal there are dropped",
    )
    parser.add_argument(
        "--scan-script",
        default=None,
        help=(
            "RSI scan_content.py; default: the first of these that exists: %s"
            % ", ".join(DEFAULT_SCAN_SCRIPT_CANDIDATES)
        ),
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="skip the secret/injection subprocess pass",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.days <= 0:
        print("--days must be a positive integer", file=sys.stderr)
        return 2
    if args.min_count < 0:
        print("--min-count must be zero or greater", file=sys.stderr)
        return 2

    ledger_dir = os.path.abspath(os.path.expanduser(args.ledger_dir))
    decided_path = (
        os.path.expanduser(args.decided)
        if args.decided
        else os.path.join(ledger_dir, "decided.json")
    )
    scan_script = (
        os.path.expanduser(args.scan_script)
        if args.scan_script
        else resolve_scan_script()
    )

    digest = build_digest(
        ledger_dir=ledger_dir,
        days=args.days,
        min_count=args.min_count,
        decided_path=decided_path,
        scan_script=scan_script,
        no_scan=args.no_scan,
        pending_dir=os.path.expanduser(args.pending_dir) if args.pending_dir else None,
    )
    print(json.dumps(digest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

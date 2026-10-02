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
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_LEDGER_DIR = "~/.claude/permission-ledger"

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
    "reask",
]
# "prior_decision" is added only on groups where reask is true.


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
                "reask": False,
            }
        )
    groups.sort(key=lambda x: (-x["count"], x["pattern_key"]))
    return groups


# --------------------------------------------------------------------------
# decided.json
# --------------------------------------------------------------------------


def load_decided(path):
    """Read decided.json. Missing or malformed is treated as empty."""
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


def run_injection_scan(groups, scan_script):
    """Run RSI's scan_content.py over the samples about to be emitted.

    Returns a status string. Samples are always emitted verbatim: the stage 3
    prompt has to *see* a directive-shaped payload to classify the group
    `sensitive`, so this pass reports rather than rewrites.
    """
    samples = _all_samples(groups)
    if not samples:
        return "skipped (no samples)"
    if not scan_script:
        return NO_SCAN_SCRIPT_STATUS
    if not os.path.isfile(scan_script):
        return "unavailable: no scanner script at %s" % scan_script
    payload = "\n---\n".join(samples)
    try:
        proc = subprocess.run(
            [sys.executable, scan_script, "--text", payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SCAN_CONTENT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "error: %s: %s" % (type(exc).__name__, exc)
    if proc.returncode == 0:
        return "ok"
    if proc.returncode == 1:
        return (
            "ok (injection patterns flagged in samples; samples emitted "
            "verbatim as inert data)"
        )
    if proc.returncode == 2:
        return "degraded: llm-guard unavailable (exit 2); samples emitted unscanned"
    return "error: scanner exited %d; samples emitted unscanned" % proc.returncode


def _detect_secrets_hit(binary, text):
    proc = subprocess.run(
        [binary, "scan", "--string", text],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=DETECT_SECRETS_TIMEOUT,
    )
    out = proc.stdout.decode("utf-8", "replace")
    return any(line.strip().endswith(": True") for line in out.splitlines())


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
    return {
        "scan_content": run_injection_scan(groups, scan_script),
        "detect_secrets": run_secret_scan(groups),
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

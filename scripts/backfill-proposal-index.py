#!/usr/bin/env python3
"""Backfill the proposal sink's dedup ledger for proposals that predate it.

WHY THIS EXISTS
---------------
The nightly RSI reviewer's sink (`rsi-proposal-sink`, a nix
`writePython3Bin` in nixos-config `home/jonathan-linux.nix`) refuses any
proposal whose content-derived id is already in
`~/.local/state/claude-proposals/.index.jsonl`, at ANY status —
`rejected` and `implemented` included. That is the guarantee that stops
the queue from cycling forever on observations already turned down
(Renovate's documented failure mode).

The guarantee is only as strong as the ledger, and the ledger started
empty. Every proposal written before it existed is invisible to dedup, so
a rejected proposal that gets drained and its file deleted regenerates the
next night under a fresh `YYYY-MM-DD-` prefix: filename dedup misses it
because the name differs, and id dedup misses it because the id was never
recorded. This script records them.

THE ALGORITHM IS COPIED VERBATIM, NOT REIMPLEMENTED
---------------------------------------------------
`fm_re`, `date_prefix`, `problem_re`, `session_re`, `split_fm`,
`fm_field`, `content_id` and `evidence` below are byte-for-byte copies of
the shipped sink's, comments included. A backfill whose hashes differ from
the sink's by one whitespace rule is worse than no backfill: it looks
done and it silently protects nothing. `--check-algorithm` (and the test
suite) compares this module's text against the real sink script and fails
on any drift, so the copy cannot rot unnoticed.

Do not "clean up" those definitions. Their exact text is the contract.

USAGE
-----
    scripts/backfill-proposal-index.py [--sink PATH] [--dry-run]
                                       [--categories a,b,c]
                                       [--check-algorithm]

Re-runnable and idempotent: it reconciles against the ledger BY ID, so a
second run appends nothing. It never edits, moves or deletes a proposal.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap

# The cross-chunk contract: consumers resolve the sink from the RSI config,
# falling back to this literal. Never by walking the ~/.claude compatibility
# symlinks — a writer that depends on a symlink existing is a writer that
# fails silently when it does not.
SINK_LITERAL = "~/.local/state/claude-proposals"
RSI_CONFIG = "~/.claude/recursive-self-improvement/config/config.json"

# Category subdirs whose proposals go into the shared ledger.
#
#   rsi           the dir `rsi-proposal-sink` writes and the ONLY dir the
#                 nightly reviewer regenerates from. The whole reason the
#                 ledger exists.
#   archived      the TTL destination for untouched rsi proposals, and the
#                 resting place of already-adjudicated ones. An archived
#                 proposal has exactly the same regeneration exposure as a
#                 drained-and-deleted rejected one: the file is no longer in
#                 rsi/ for the reviewer to read, so the ledger row is all
#                 that stops it coming back.
#   manual        hand-written proposals in the same lifecycle; a reviewer
#                 re-raising one has raised something already on the table.
#   research      same, with `status: open` rather than `pending`. The
#                 enforcement half of the ledger ignores status anyway.
#   clv2          currently empty; walked so a proposal appearing there
#                 later is picked up without editing this list.
#   from-research
#   router
INDEXED_CATEGORIES = (
    "rsi", "archived", "manual", "research", "clv2", "from-research", "router",
)

# Deliberately NOT indexed, with the reason printed on every run.
SKIPPED_CATEGORIES = {
    "permissions": (
        "written by permission-ledger/proposal-sink.py, which has its own "
        "dedup and never reads this ledger; a permission pattern that gets "
        "denied again is a fresh signal, not a duplicate, so a row saying "
        "'never raise this again' would be wrong"
    ),
}

# Non-proposal files that live in the category dirs.
EXCLUDED_FILES = re.compile(r"^(\.|README|on-decision)")

LEDGER_NAME = ".index.jsonl"


# --------------------------------------------------------------------------
# VERBATIM from the shipped `rsi-proposal-sink`. See the module docstring.
# Nothing below this line down to the end marker may be reformatted,
# renamed, commented or "improved" — `algorithm_drift()` compares it
# character for character against the sink that actually runs.
# --------------------------------------------------------------------------
fm_re = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
date_prefix = re.compile(r"^\d{4}-\d{2}-\d{2}-")
problem_re = re.compile(
    r"^#+[ \t]*Problem[ \t]*\n(.*?)(?=\n#|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
session_re = re.compile(
    r"^source_sessions:[ \t]*\n[ \t]*-[ \t]*(\S+)", re.MULTILINE
)


def split_fm(text):
    m = fm_re.match(text)
    return (m.group(0), text[m.end():]) if m else ("", text)


def fm_field(fm, key):
    pat = r"^%s:[ \t]*(.*?)[ \t]*$" % re.escape(key)
    m = re.search(pat, fm, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else ""


def content_id(fname, text):
    stem = fname[:-3] if fname.endswith(".md") else fname
    title = date_prefix.sub("", stem)
    norm = re.sub(r"[^a-z0-9 ]+", " ", split_fm(text)[1].lower())
    payload = "%s\n\n%s" % (title, " ".join(norm.split()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def evidence(text):
    # A verbatim excerpt, never a transcript path: Claude Code keys
    # project storage on a slug derived from the literal working
    # directory string, so a moved or renamed repo orphans the path
    # while the excerpt stays readable forever.
    body = split_fm(text)[1]
    m = problem_re.search(body)
    return " ".join((m.group(1) if m else body).split())[:240]


# --------------------------------------------------------------------------
# END VERBATIM BLOCK
# --------------------------------------------------------------------------

SHARED_ALGORITHM = (
    "fm_re", "date_prefix", "problem_re", "session_re",
    "split_fm", "fm_field", "content_id", "evidence",
)


def _top_level_block(source, name):
    """The `def name(...)` block or `name = ...` statement, verbatim.

    Both this module and the sink script define these at column 0, so a
    block runs from its opening line to the next non-blank line at column
    0. Returns None when the name is absent.
    """
    lines = source.split("\n")
    starts = ("def %s(" % name, "%s = " % name)
    for i, line in enumerate(lines):
        if not line.startswith(starts):
            continue
        out = [line]
        for nxt in lines[i + 1:]:
            if nxt.strip() and not nxt[:1].isspace():
                break
            out.append(nxt)
        return "\n".join(out).rstrip()
    return None


def algorithm_drift(shipped_source):
    """Names whose text differs from `shipped_source`'s, or are missing.

    Empty list means this module's id algorithm is character-identical to
    the sink's, which is the only property the backfilled ledger's value
    rests on.
    """
    mine = _read(os.path.abspath(__file__))
    drift = []
    for name in SHARED_ALGORITHM:
        theirs = _top_level_block(shipped_source, name)
        if theirs is None:
            drift.append("%s: absent from the shipped sink" % name)
        elif theirs != _top_level_block(mine, name):
            drift.append("%s: differs" % name)
    return drift


# --------------------------------------------------------------------------
# Locating the sink that actually ships.
# --------------------------------------------------------------------------
NIX_SINK_RE = re.compile(
    r"writePython3Bin\s+\"rsi-proposal-sink\"[^\n]*''\n(.*?)\n\s*'';",
    re.DOTALL,
)

NIXOS_CONFIG_GLOBS = (
    "/etc/nixos",
    "~/Repos/nixos-config",
    "~/Repos/nixos-config-worktrees/*",
    "~/worktrees/nixos-*",
)


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _materialize(script):
    """Cache `script` as an executable under the temp dir, keyed by hash."""
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    out = os.path.join(tempfile.gettempdir(), "rsi-proposal-sink-%s" % digest)
    if not os.path.exists(out):
        fd, tmp = tempfile.mkstemp(prefix=".rsi-sink-",
                                   dir=tempfile.gettempdir())
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        os.chmod(tmp, 0o755)
        os.replace(tmp, out)
    return out


def _sinks_from_nix_source():
    """Executable copies of the sink extracted from nixos-config checkouts."""
    out = []
    for pattern in NIXOS_CONFIG_GLOBS:
        for root in sorted(glob.glob(os.path.expanduser(pattern))):
            nix = os.path.join(root, "home", "jonathan-linux.nix")
            if not os.path.isfile(nix):
                continue
            m = NIX_SINK_RE.search(_read(nix))
            if not m:
                continue
            # Nix indented-string dedent, then its two escapes.
            body = textwrap.dedent(m.group(1))
            body = body.replace("'''", "''").replace("''${", "${")
            out.append(_materialize("#!%s\n%s\n" % (sys.executable, body)))
    return out


def shipped_sink_candidates():
    """Every runnable copy of the sink this box can offer, best first.

    An explicit override, then the deployed binary, then every
    nixos-config checkout's source, then every built store path. There is
    deliberately no single answer: the box carries several vintages at
    once (older builds, sibling worktrees, and the branch under review),
    and only the ones carrying the ledger algorithm at all are returned.
    """
    found, seen = [], set()

    def add(path):
        if path and os.path.isfile(path) and path not in seen:
            seen.add(path)
            if "def content_id(" in _read(path):
                found.append(path)

    add(os.environ.get("RSI_PROPOSAL_SINK"))
    add(shutil.which("rsi-proposal-sink"))
    for path in _sinks_from_nix_source():
        add(path)
    for path in sorted(glob.glob(
            "/nix/store/*-rsi-proposal-sink/bin/rsi-proposal-sink")):
        add(path)
    return found


def find_shipped_sink():
    """Path to an executable copy of the sink that ships, or None.

    The first candidate whose id algorithm agrees with this module's; if
    none agrees, the first candidate anyway, so `algorithm_drift()` can
    report the disagreement loudly instead of the caller silently finding
    nothing. Which vintage got picked is not load-bearing on its own — the
    test suite also pins the ids against golden vectors captured from the
    build that was byte-verified against the reviewed nix source.
    """
    candidates = shipped_sink_candidates()
    for path in candidates:
        if not algorithm_drift(_read(path)):
            return path
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# The backfill.
# --------------------------------------------------------------------------
def default_sink_root():
    try:
        cfg = json.loads(_read(os.path.expanduser(RSI_CONFIG)))
        folder = (cfg.get("proposals") or {}).get("folder")
        if folder:
            return os.path.abspath(os.path.expanduser(folder))
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.abspath(os.path.expanduser(SINK_LITERAL))


def read_ledger(path):
    """(raw text, {id: row}, unparseable line count).

    The raw text is kept verbatim so republishing preserves every existing
    line exactly, including ones this script cannot parse — a hand-edited
    or torn line is data, not litter.
    """
    try:
        raw = _read(path)
    except FileNotFoundError:
        return "", {}, 0
    known, bad = {}, 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict) and row.get("id"):
            known[row["id"]] = row
    return raw, known, bad


def filename_date(fname):
    m = date_prefix.match(fname)
    return m.group(0)[:-1] if m else None


def build_record(category, fname, text):
    """One ledger row for a historical proposal.

    Same fields the live sink writes. Where the sink defaults a missing
    field to today's date or the string "unknown", this records `null`
    instead: for a proposal written months ago those defaults would be
    fabrications, and the sink's reader already coerces a null status to
    "unknown" on the way back in. `category` and `backfilled` are added so
    a human reading the ledger can tell a reconstructed row from one the
    nightly run wrote; nothing reads them.
    """
    fm = split_fm(text)[0]
    session = session_re.search(fm)
    excerpt = evidence(text)
    return {
        "id": content_id(fname, text),
        "file": fname,
        "date": fm_field(fm, "date") or filename_date(fname) or None,
        "status": fm_field(fm, "status") or None,
        "session": session.group(1) if session else None,
        "evidence": excerpt or None,
        "category": category,
        "backfilled": True,
    }


def publish(path, raw, new_lines):
    """Replace the ledger with `raw` + `new_lines`, atomically.

    tmp beside the destination, fsynced, then `rename(2)`. Unlike the
    sink's per-proposal publish there is nothing here to protect against
    clobbering — this rewrites a file it has just read — so `os.replace`
    is right where the sink needs `link(2)`. The pre-publish re-read is an
    optimistic concurrency check: if a nightly run appended while this was
    working, its rows would be silently dropped by the rename, so bail
    instead.
    """
    body = raw
    if body and not body.endswith("\n"):
        body += "\n"
    body += "".join(line + "\n" for line in new_lines)
    mode = 0o600
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=LEDGER_NAME + ".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        current = ""
        try:
            current = _read(path)
        except FileNotFoundError:
            pass
        if current != raw:
            raise RuntimeError(
                "%s changed while the backfill was running; nothing written. "
                "Re-run when the nightly sink is idle." % path)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def scan(root, categories):
    """(records, skip messages, scanned count) for `categories` under root."""
    records, skips, scanned = [], [], 0
    for category in categories:
        directory = os.path.join(root, category)
        if not os.path.isdir(directory):
            skips.append("skipped category %s (no such directory)" % category)
            continue
        for fname in sorted(os.listdir(directory)):
            path = os.path.join(directory, fname)
            if not os.path.isfile(path):
                continue
            if not fname.endswith(".md"):
                skips.append("skipped %s/%s (not a .md proposal)"
                             % (category, fname))
                continue
            if EXCLUDED_FILES.match(fname):
                skips.append("skipped %s/%s (not a proposal)"
                             % (category, fname))
                continue
            try:
                text = _read(path)
            except OSError as exc:
                skips.append("skipped %s/%s (unreadable: %s)"
                             % (category, fname, exc))
                continue
            scanned += 1
            records.append(build_record(category, fname, text))
    return records, skips, scanned


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill the proposal sink's dedup ledger.")
    parser.add_argument("--sink", default=None,
                        help="sink root (default: proposals.folder from the "
                             "RSI config, else %s)" % SINK_LITERAL)
    parser.add_argument("--categories", default=None,
                        help="comma-separated category subdirs to index "
                             "(default: %s)" % ",".join(INDEXED_CATEGORIES))
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be added, write nothing")
    parser.add_argument("--check-algorithm", action="store_true",
                        help="compare the id algorithm against the shipped "
                             "sink and exit")
    args = parser.parse_args(argv)

    if args.check_algorithm:
        path = find_shipped_sink()
        if not path:
            print("backfill: no rsi-proposal-sink found to compare against")
            return 2
        drift = algorithm_drift(_read(path))
        if drift:
            print("backfill: ALGORITHM DRIFT against %s" % path)
            for item in drift:
                print("backfill:   %s" % item)
            return 1
        print("backfill: id algorithm is identical to %s" % path)
        return 0

    root = os.path.abspath(os.path.expanduser(args.sink)) \
        if args.sink else default_sink_root()
    if not os.path.isdir(root):
        print("backfill: no sink at %s" % root, file=sys.stderr)
        return 1
    categories = ([c.strip() for c in args.categories.split(",") if c.strip()]
                  if args.categories else list(INDEXED_CATEGORIES))

    ledger = os.path.join(root, LEDGER_NAME)
    raw, known, unparseable = read_ledger(ledger)
    if unparseable:
        print("backfill: %d unparseable ledger line(s) left untouched"
              % unparseable)

    skips = []
    for category, reason in sorted(SKIPPED_CATEGORIES.items()):
        if category in categories:
            categories.remove(category)
        if os.path.isdir(os.path.join(root, category)):
            skips.append("skipped category %s (%s)" % (category, reason))

    records, scan_skips, scanned = scan(root, categories)
    skips.extend(scan_skips)

    added, present, seen = [], 0, set()
    for record in records:
        cid = record["id"]
        if cid in known:
            present += 1
            continue
        if cid in seen:
            skips.append("skipped %s/%s (id %s already added this run — "
                         "identical content to an earlier file)"
                         % (record["category"], record["file"], cid))
            continue
        seen.add(cid)
        added.append(record)

    for message in skips:
        print("backfill: %s" % message)

    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in added]
    if lines and not args.dry_run:
        publish(ledger, raw, lines)
    if args.dry_run:
        print("backfill: dry run, nothing written")

    print("backfill: %s — scanned %d, added %d, already present %d, skipped %d"
          % (root, scanned, len(added), present, len(skips)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

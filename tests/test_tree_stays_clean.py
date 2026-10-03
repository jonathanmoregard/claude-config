#!/usr/bin/env python3
"""Nothing may leave an untracked file behind in the live config repo.

WHAT THIS GUARDS. ``~/.claude`` is a git checkout the harness reads at
runtime, and it is also a directory a dozen unrelated things write into. Every
time one of them drops a file the ignore rules do not cover, the checkout goes
dirty; the autosync committer then either commits runtime state into a repo
that will one day be public, or reports a permanently dirty worktree and stops
being trusted. Measured 2026-08-31, four such files were sitting in it.

The bug has one recognisable shape and it recurred three times before anyone
named it: **an ignore rule pinned to the one filename or extension that was on
disk the day it was written, while the writer beside it emits another.**

  * ``logs/*.log`` was the rule; the RSI reviewer emitted
    ``logs/rsi-raw-failed-*.txt``.  (nixos PR #203)
  * the same rule; ``permission-ledger/run-evaluate.sh`` emitted
    ``logs/permission-ledger-raw-*.txt``, and ``proposal-intake-gate.py``
    emitted ``logs/gate-scorer-raw-*.txt``.  (this change)
  * ``daemon.log`` + ``daemon/`` were the rules; the harness itself writes
    ``daemon.status.json``, ``daemon-auth-status.json`` and
    ``daemon-auth-cooldown`` into the same directory.  (this change)

So this suite deliberately does NOT assert "the tree is clean right now".
That assertion passes vacuously on any morning nothing has run, which is
exactly when a regression gets merged. It asserts instead that for every
KNOWN writer, the shape it emits is accounted for — either relocated out of
the repo entirely, or matched by a live ignore rule. Two disciplines, and the
choice between them is not free-form:

  * a writer WE control gets moved to ``~/.local/state/<thing>/``. An ignore
    rule for it would be papering over a fixable defect.
  * a writer we do NOT control — the ``claude`` binary — can only be
    gitignored, and then the list has to come from the harness's own state
    registry rather than from whatever happened to show up in git status.

Run either way::

    python3 tests/test_tree_stays_clean.py
    nix-shell -p python3Packages.pytest --run 'pytest tests/test_tree_stays_clean.py'
"""
from __future__ import annotations

import mmap
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Registry entries that are genuine user config and MUST stay trackable. The
# harness reads them from the same directory it drops state into, so the two
# classes are only separable by hand.
HARNESS_CONFIG_ALLOWLIST = {"keybindings.json", "CLAUDE.md", "settings.json"}

# How the harness names the files it keeps in its config home. Up to 2.1.251
# that was a literal `new Map([["daemon-status","daemon.status.json"], ...])`;
# by 2.1.285 the Map is gone and each path is built inline as
# `join(<configHome>(), "<name>")`. The getter's minified name changes per
# build, so it is learned from one call we know exists (daemon.status.json)
# and every other literal joined onto the same getter is then collected.
REGISTRY_ANCHOR = b'(),"daemon.status.json")'
REGISTRY_NAME = re.compile(rb'"([^"]+)"\)')
GETTER_NAME = re.compile(rb'([A-Za-z_$][\w$]*)$')
IDENT_BYTE = re.compile(rb'[\w$]')

# Writers we own that used to write into the repo and now must not. Each row:
# (source file, a fragment that marks the lines doing the writing, the
# state-dir root the file must name, substrings those lines must NOT contain).
RELOCATED_WRITERS = [
    (
        "permission-ledger/run-evaluate.sh",
        "permission-ledger-raw-",
        "$HOME/.local/state/permission-ledger",
        ("LOG_DIR", ".claude/logs"),
    ),
    (
        "scripts/proposal-intake-gate.py",
        "gate-scorer-raw-",
        "~/.local/state/proposal-intake-gate",
        (".claude/logs", ".claude/"),
    ),
    (
        "scripts/proposal-intake-gate.py",
        "gate.lock",
        "~/.local/state/proposal-intake-gate",
        (".claude/logs", ".claude/"),
    ),
    # classifier.log + one cooldown-<session> stamp per session. The corpus
    # (labels.jsonl, backtest-round-*.txt) stays tracked in ~/.claude; only
    # the runtime moved.
    (
        "continue-nudge-detector.py",
        "STATE_DIR =",
        '".local" / "state" / "continue-nudge"',
        ('".claude" / "continue-nudge"',),
    ),
    (
        "continue-nudge-classifier.py",
        "STATE_DIR =",
        '".local" / "state" / "continue-nudge"',
        ('".claude" / "continue-nudge"',),
    ),
]

# The detector recognises its own sandbox subprocess by comparing cwd against
# STATE_DIR, and the classifier is what sets that cwd. Two constants, one
# invariant: if they drift the recursion guard silently stops guarding.
STATE_DIR_PAIR = ("continue-nudge-detector.py", "continue-nudge-classifier.py")
STATE_DIR_RE = re.compile(r"^STATE_DIR\s*=\s*(.+)$", re.M)

# Shapes that legitimately land inside the repo and therefore need a rule.
# Representative filenames, not globs: check-ignore answers about paths.
MUST_BE_IGNORED = [
    # Same-directory tmp file of an atomic rename — it cannot move out of the
    # repo without breaking the rename, so here gitignore IS the fix.
    "permission-ledger/decided.json.tmp",
    # run-evaluate.sh's per-process scratch. The EXIT trap removes these on
    # every path except the ones that matter (SIGKILL, OOM, reboot).
    "permission-ledger/.digest-latest.json.12345",
    "permission-ledger/.digest-prev.json.12345",
    "permission-ledger/.digest-latest.err.12345",
    "permission-ledger/.agent-raw.12345",
    # Runtime logs the cron entries append to.
    "logs/permission-ledger.log",
    "logs/habit-tracker.log",
    # claude-cl-sync's staging tree. Nothing in it is ours to relocate — the
    # nix unit declares this path — and its two sibling ReadWritePaths
    # (homunculus/, dev-container/.venv/) are already ignored here.
    "container-staging/whatever-gets-staged-next",
    # The continue-nudge experiment's corpus, which legitimately stays in the
    # repo but is regenerated by the harvester.
    "continue-nudge/labels.jsonl",
]

# Source trees that are vendored, generated, or someone else's.
SKIP_DIRS = {
    ".git", "plugins", "projects", "homunculus", "reflections", "sessions",
    "cache", "node_modules", "__pycache__", ".venv", ".pytest_cache",
    "downloads", "backups", "file-history", "shell-snapshots", "telemetry",
    "tmp", "docs", "stage", "workers", "loops", "token-optimizer",
}

# A literal path under ~/.claude/logs, or a $LOG_DIR/ reference (run-evaluate's
# spelling of the same directory), with the basename captured.
LOGS_WRITE = re.compile(
    r"""(?:~|\$HOME|\$\{?HOME\}?|\$CLAUDE_HOME|\$\{?CLAUDE_HOME\}?|/home/[^/\s"']+)"""
    r"""/\.claude/logs/([^\s"'`)]+)"""
    r"""|\$\{?LOG_DIR\}?/([^\s"'`)]+)"""
)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=False,
    )


def is_ignored(relpath: str) -> tuple[bool, str]:
    """(matched, the rule that matched) for a path relative to the repo."""
    proc = git("check-ignore", "-v", "--", relpath)
    return proc.returncode == 0, proc.stdout.strip()


def find_claude_binary() -> Path | None:
    """The harness binary, if this machine has one. The launcher shim keeps
    real builds under ~/.local/share/claude/versions/<version>."""
    versions = Path.home() / ".local/share/claude/versions"
    if versions.is_dir():
        builds = [p for p in versions.iterdir() if p.is_file() and os.access(p, os.X_OK)]
        if builds:
            return max(builds, key=lambda p: p.stat().st_mtime)
    which = shutil.which("claude")
    if which:
        resolved = Path(which).resolve()
        # A shell shim is not the bundle; only accept something binary-sized.
        if resolved.is_file() and resolved.stat().st_size > 1_000_000:
            return resolved
    return None


def harness_config_home_names(binary: Path) -> set[str]:
    """Every literal name the bundle joins directly onto its config home."""
    names: set[str] = set()
    with open(binary, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            anchor = data.find(REGISTRY_ANCHOR)
            if anchor == -1:
                return names
            getter = GETTER_NAME.search(data[max(0, anchor - 64):anchor])
            if getter is None:
                return names
            needle = getter.group(1) + b'(),"'
            pos = data.find(needle)
            while pos != -1:
                # `xwe()` is another function, not `we()`.
                if not IDENT_BYTE.match(data[pos - 1:pos]):
                    start = pos + len(needle) - 1
                    m = REGISTRY_NAME.match(data[start:start + 256])
                    if m:
                        names.add(m.group(1).decode())
                pos = data.find(needle, pos + 1)
    return names


def harness_state_files(binary: Path) -> set[str]:
    """The harness's state FILES in ~/.claude (directories have their own
    rules, as subdir entries did under the old registry)."""
    return {n for n in harness_config_home_names(binary)
            if "/" not in n and ("." in n or n.startswith("."))}


def source_files() -> list[Path]:
    out = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith((".py", ".sh", ".nix", ".bash")):
                out.append(Path(root) / name)
    return out


class HarnessStateIsIgnored(unittest.TestCase):
    """The `claude` binary writes these; we cannot relocate them, so the
    ignore list has to be complete rather than reactive."""

    def test_registry_is_readable(self):
        binary = find_claude_binary()
        if binary is None:
            self.skipTest("no claude bundle on this machine")
        names = harness_config_home_names(binary)
        self.assertIn("daemon.status.json", names)
        self.assertIn("settings.json", names)
        # A registry that suddenly parses to a handful of entries means the
        # bundle layout moved and this test stopped guarding anything.
        self.assertGreaterEqual(
            len(names), 20,
            "state registry parsed to %d entries — parser is out of date "
            "against the bundle, not a real shrink" % len(names))
        self.assertGreaterEqual(len(harness_state_files(binary)), 10)

    def test_every_harness_state_file_is_ignored(self):
        binary = find_claude_binary()
        if binary is None:
            self.skipTest("no claude bundle on this machine")
        missing = []
        for name in sorted(harness_state_files(binary)):
            if name in HARNESS_CONFIG_ALLOWLIST:
                continue
            matched, _rule = is_ignored(name)
            if not matched:
                missing.append(name)
        self.assertEqual(
            [], missing,
            "harness writes these into ~/.claude and no ignore rule covers "
            "them; each will show up as untracked the first time the harness "
            "touches it:\n  " + "\n  ".join(missing))

    def test_allowlisted_config_stays_trackable(self):
        # The inverse failure: a broad rule that swallows real config.
        for name in sorted(HARNESS_CONFIG_ALLOWLIST):
            matched, rule = is_ignored(name)
            self.assertFalse(
                matched,
                "%s is user config and must stay trackable, but %s ignores it"
                % (name, rule))


class OwnWritersAreRelocated(unittest.TestCase):
    """Writers we control get moved out of the repo, never ignored into it."""

    def test_relocated_writers_name_the_state_dir(self):
        for rel, marker, state_root, _forbidden in RELOCATED_WRITERS:
            with self.subTest(source=rel, emits=marker):
                text = (REPO / rel).read_text(encoding="utf-8")
                self.assertIn(
                    state_root, text,
                    "%s emits %s* but never names %s" % (rel, marker, state_root))

    def test_relocated_writers_do_not_target_the_repo(self):
        for rel, marker, _state_root, forbidden in RELOCATED_WRITERS:
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = 0
            for lineno, line in enumerate(text.splitlines(), 1):
                if marker not in line or line.lstrip().startswith("#"):
                    continue
                hits += 1
                for bad in forbidden:
                    with self.subTest(source=rel, line=lineno, forbidden=bad):
                        self.assertNotIn(
                            bad, line,
                            "%s:%d puts %s back inside the config repo"
                            % (rel, lineno, marker))
            with self.subTest(source=rel, emits=marker):
                # A marker nobody matches means the writer was renamed and this
                # row stopped guarding anything.
                self.assertGreater(
                    hits, 0, "no line in %s carries %r any more" % (rel, marker))

    def test_continue_nudge_state_dirs_stay_identical(self):
        values = {}
        for rel in STATE_DIR_PAIR:
            text = (REPO / rel).read_text(encoding="utf-8")
            found = STATE_DIR_RE.search(text)
            self.assertIsNotNone(found, "%s no longer defines STATE_DIR" % rel)
            values[rel] = found.group(1).strip()
        self.assertEqual(
            len(set(values.values())), 1,
            "the detector matches its sandbox subprocess by cwd == STATE_DIR; "
            "these two must not drift: %r" % values)


class LogsDirRuleStaysHonest(unittest.TestCase):
    """`logs/*.log` is left narrow ON PURPOSE: widening it to `logs/*` would
    silently absorb the next mis-targeted writer instead of surfacing it. That
    only holds while nothing in the repo writes a non-`.log` name there."""

    def test_no_source_writes_a_non_log_name_into_claude_logs(self):
        offenders = []
        for path in source_files():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for match in LOGS_WRITE.finditer(line):
                    name = match.group(1) or match.group(2)
                    # Trailing sentence punctuation: these paths turn up in
                    # prose ("check ~/.claude/logs/foo.log.") as often as in
                    # code, and a full stop is never part of a filename.
                    base = name.rstrip('"\'`,;:).')
                    if base.endswith(".log") or base in ("", "/"):
                        continue
                    offenders.append(
                        "%s:%d -> logs/%s"
                        % (path.relative_to(REPO), lineno, base))
        self.assertEqual(
            [], offenders,
            "these write a non-.log file into ~/.claude/logs, which "
            "`logs/*.log` does not cover — move the output to "
            "~/.local/state/<thing>/ rather than widening the rule:\n  "
            + "\n  ".join(offenders))


class InRepoRuntimeIsIgnored(unittest.TestCase):
    def test_shapes_that_must_stay_ignored(self):
        for rel in MUST_BE_IGNORED:
            with self.subTest(path=rel):
                matched, rule = is_ignored(rel)
                self.assertTrue(matched, "no ignore rule matches %s" % rel)
                self.assertTrue(rule, "check-ignore gave no rule for %s" % rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)

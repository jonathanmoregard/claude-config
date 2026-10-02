#!/usr/bin/env python3
"""Every runtime path the `claude` binary keeps in ~/.claude is gitignored.

The binary carries its own answer to "which entries in the config home are
runtime, not config": the exclusion set it applies when it snapshots a host
config dir (the code beside the ``.host-config-snapshot-`` prefix). It is a
literal ``new Set([".claude.json", ".claude.json.backup", ...])``. This suite
reads that set out of the installed bundle and asserts each entry is ignored
here, so a directory the harness starts writing next release (``chrome/``
showed up untracked on 2026-10-02) is caught by the test, not by git status.

Two inverse invariants keep the rules from overreaching: no tracked file may
become ignored, and ``memory/`` / ``agent-memory/`` stay trackable (the binary
deliberately leaves those out of its exclusion set: they are user content).

Run::

    python3 tests/test_harness_runtime_dirs_ignored.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SET_ANCHOR = b'new Set([".claude.json",'

# Entries of the exclusion set that ARE tracked here on purpose and are
# governed by narrower rules of their own (logs/*.log, with hand-made
# crontab backups and review-agent.log tracked).
TRACKED_ON_PURPOSE = {"logs"}

# User content the harness keeps beside its runtime state; must never be
# swallowed by a broad runtime rule.
MUST_STAY_TRACKABLE = ["memory/MEMORY.md", "agent-memory/notes.md"]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=False)


def find_claude_binary() -> Path | None:
    versions = Path.home() / ".local/share/claude/versions"
    if versions.is_dir():
        builds = [p for p in versions.iterdir()
                  if p.is_file() and os.access(p, os.X_OK)]
        if builds:
            return max(builds, key=lambda p: p.stat().st_mtime)
    which = shutil.which("claude")
    if which:
        resolved = Path(which).resolve()
        if resolved.is_file() and resolved.stat().st_size > 1_000_000:
            return resolved
    return None


def snapshot_exclusions(data: bytes) -> list[str]:
    """Top-level string literals of the snapshot-exclusion Set.

    Bracket-aware: the set contains a nested spread
    (``...["policy-limits.json", ...].flatMap(...)``) whose literals are
    already listed at top level, and computed identifiers we cannot resolve
    statically. Only depth-1 literals are returned."""
    start = data.find(SET_ANCHOR)
    if start == -1:
        return []
    i = start + len(b"new Set(")
    depth = 0
    names: list[str] = []
    while i < len(data):
        c = data[i:i + 1]
        if c == b'"':
            end = data.find(b'"', i + 1)
            if depth == 1:
                names.append(data[i + 1:end].decode())
            i = end + 1
            continue
        if c in b"([{":
            depth += 1
        elif c in b")]}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return names


def probe_path(name: str) -> str:
    """check-ignore answers about paths. A dotless entry is a directory; probe
    a file inside it so a `name/` rule is exercised the way git applies it."""
    return name if "." in name else name + "/probe"


class HarnessRuntimeIsIgnored(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = find_claude_binary()
        if binary is None:
            raise unittest.SkipTest("no claude bundle on this machine")
        cls.names = snapshot_exclusions(binary.read_bytes())

    def test_exclusion_set_is_readable(self):
        # A set that parses to a handful of entries means the bundle layout
        # moved and the guard below silently stopped guarding.
        self.assertIn("projects", self.names)
        self.assertIn(".credentials.json", self.names)
        self.assertGreaterEqual(len(self.names), 40, self.names)

    def test_every_exclusion_is_ignored(self):
        missing = []
        for name in sorted(set(self.names) - TRACKED_ON_PURPOSE):
            proc = git("check-ignore", "-q", "--no-index", "--",
                       probe_path(name))
            if proc.returncode != 0:
                missing.append(name)
        self.assertEqual(
            [], missing,
            "the claude binary treats these as runtime state but no ignore "
            "rule covers them:\n  " + "\n  ".join(missing))

    def test_user_memory_is_not_runtime(self):
        for rel in MUST_STAY_TRACKABLE:
            top = rel.split("/")[0]
            with self.subTest(path=rel):
                self.assertNotIn(top, self.names)
                proc = git("check-ignore", "-v", "--no-index", "--", rel)
                self.assertNotEqual(
                    0, proc.returncode,
                    "%s must stay trackable but %s ignores it"
                    % (rel, proc.stdout.strip()))


class NoTrackedFileIsIgnored(unittest.TestCase):
    def test_ls_files_ignored_is_empty(self):
        proc = git("ls-files", "-ci", "--exclude-standard")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("", proc.stdout.strip(),
                         "tracked files now matched by an ignore rule")


if __name__ == "__main__":
    unittest.main(verbosity=2)

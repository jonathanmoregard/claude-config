"""Relocation of the proposals spine out of ~/.claude and into the sink.

Proposals used to live inside the live config repo (`~/.claude/proposals/` plus
`~/.claude/recursive-self-improvement/proposals/`, wired to each other by a
`proposals/rsi` symlink that resolved BACK into the repo). Every nightly RSI
run therefore dirtied the checkout the running harness reads. They now live in
one git-backed sink at `~/.local/state/claude-proposals/`, with the two old
names kept alive as absolute compatibility symlinks FOR READERS ONLY — every
writer resolves the sink directly, because a writer that depends on a symlink
existing is a writer that fails silently when it does not.

This suite covers the five things the relocation can break silently:

  * symlink resolution — both legacy names resolve to the sink, absolute;
  * gate spine resolution — the intake gate finds, walks and logs the sink;
  * the on-decision callback containment trap — `run_callback` refuses any
    callback realpath-resolving outside `~/.claude`, so a callback living at
    `~/.claude/proposals/permissions/on-decision` DIES the moment `proposals/`
    becomes a symlink into the sink, taking the permission-ledger decision
    drain with it;
  * dedup — `rejected` counts as settled, and the reviewer still reads the
    rejected corpus so it does not regenerate what was already turned down;
  * atomic write — a crashed run must never leave a truncated proposal that
    triage reads as evidence.

Everything runs against throwaway fixture trees. Nothing here touches the real
`~/.claude`, the real sink, or the real permission ledger.

Run:  python3 tests/test_proposals_sink.py      (stdlib unittest, always works)
      python3 -m pytest -q tests/test_proposals_sink.py

pytest is not installed in this box's system python3 (no pip, no venv, no CI),
so the pytest invocation needs one that has it:

      nix-shell -p python3Packages.pytest \\
        --run 'python3 -m pytest -q tests/test_proposals_sink.py'
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SINK_LITERAL = "~/.local/state/claude-proposals"
SINK_ABS = "/home/jonathan/.local/state/claude-proposals"

GATE_PATH = os.path.join(REPO_ROOT, "scripts", "proposal-intake-gate.py")
SINK_PY_PATH = os.path.join(REPO_ROOT, "permission-ledger", "proposal-sink.py")
RSI_CONFIG_PATH = os.path.join(
    REPO_ROOT, "recursive-self-improvement", "config", "config.json")
RSI_PROMPT_PATH = os.path.join(
    REPO_ROOT, "recursive-self-improvement", "config", "prompt.md")
PUSH_SCRIPT = os.path.join(REPO_ROOT, "push-proposals.sh")
CUTOVER_SCRIPT = os.path.join(REPO_ROOT, "scripts", "cutover-proposals.sh")
ON_DECISION = os.path.join(REPO_ROOT, "permission-ledger", "on-decision")
RUN_EVALUATE = os.path.join(REPO_ROOT, "permission-ledger", "run-evaluate.sh")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_module("proposal_intake_gate", GATE_PATH)
psink = load_module("permission_ledger_proposal_sink", SINK_PY_PATH)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def git(*args, cwd, env=None):
    """Run git, raising with the full output when it fails."""
    proc = subprocess.run(("git",) + args, cwd=cwd, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s failed in %s: %s%s"
                             % (" ".join(args), cwd, proc.stdout, proc.stderr))
    return proc.stdout


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Proposals Sink Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Proposals Sink Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


class FixtureHome:
    """A throwaway $HOME holding a `.claude` checkout and a sink git repo.

    Mirrors the production shapes closely enough that the real scripts run
    unmodified against it: `$HOME/.claude` is a git repo, the sink lives at
    `$HOME/.local/state/claude-proposals` and is a git repo of its own, and the
    RSI config carries `proposals.folder`.
    """

    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "home")
        self.claude = os.path.join(self.home, ".claude")
        self.sink = os.path.join(self.home, ".local", "state",
                                 "claude-proposals")
        self.backup = os.path.join(self.home, ".local", "state",
                                   "claude-proposals-backup")

    # -- construction ----------------------------------------------------
    def build(self, legacy_dirs=True, rsi_config=True):
        for d in (self.claude,
                  os.path.join(self.claude, "scripts"),
                  os.path.join(self.claude, "logs"),
                  os.path.join(self.claude, "permission-ledger"),
                  os.path.join(self.claude, "recursive-self-improvement",
                               "config")):
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o755)
        os.chmod(self.home, 0o755)
        os.chmod(self.root, 0o755)

        for name in ("rsi", "archived", "manual", "research", "permissions",
                     "clv2", "from-research", "router"):
            os.makedirs(os.path.join(self.sink, name), exist_ok=True)
        with open(os.path.join(self.sink, ".index.jsonl"), "w",
                  encoding="utf-8"):
            pass
        with open(os.path.join(self.sink, ".gitignore"), "w",
                  encoding="utf-8") as fh:
            fh.write(".gate.lock\n")
        self.write_gate_config()
        # A settled proposal so the gate has nothing eligible to dispatch on.
        self.write_proposal("rsi", "2026-08-01-settled.md",
                            "---\nstatus: implemented\n---\n\nbody\n")

        shutil.copy2(GATE_PATH, os.path.join(self.claude, "scripts",
                                             "proposal-intake-gate.py"))
        # The repo's REAL ignore rules, so "the live checkout stays clean
        # after the cutover" is tested against what actually ships.
        shutil.copy2(os.path.join(REPO_ROOT, ".gitignore"),
                     os.path.join(self.claude, ".gitignore"))
        if os.path.exists(ON_DECISION):
            dst = os.path.join(self.claude, "permission-ledger", "on-decision")
            shutil.copy2(ON_DECISION, dst)
            os.chmod(dst, 0o755)
        self.write_record_decision_shim()
        if rsi_config:
            self.write_rsi_config()

        if legacy_dirs:
            os.makedirs(os.path.join(self.claude, "proposals"), exist_ok=True)
            os.makedirs(os.path.join(self.claude,
                                     "recursive-self-improvement",
                                     "proposals"), exist_ok=True)

        self.init_git(self.sink)
        self.init_git(self.claude)
        return self

    def init_git(self, path):
        env = dict(os.environ, **GIT_IDENTITY)
        git("init", "-q", "-b", "main", cwd=path, env=env)
        git("add", "-A", cwd=path, env=env)
        git("commit", "-q", "-m", "fixture", "--allow-empty", cwd=path,
            env=env)

    # -- content helpers -------------------------------------------------
    def write_gate_config(self, on_decision=None):
        cfg = {
            "spine_root": SINK_LITERAL,
            "subdirs": [],
            "excluded_subdirs": [".*", "archived"],
            "excluded_files": ["README*", ".*", "on-decision", "on-decision.*"],
            "scorer": {
                "model": "claude-fable-5",
                "fallback_model": "opus",
                "allowed_tools": "Read Grep Glob",
                "timeout_seconds": 600,
            },
            "search_paths": ["~/.claude"],
            "pr_context": {"repo_roots": ["~/Repos"], "extra_repos": {},
                           "merged_limit": 30, "timeout_seconds": 30},
            "on_decision": {
                "permissions": on_decision
                or os.path.join(self.claude, "permission-ledger",
                                "on-decision"),
            },
            "gate_log": SINK_LITERAL + "/gate-log.jsonl",
        }
        path = os.path.join(self.sink, "gate-config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        return path

    def write_rsi_config(self):
        path = os.path.join(self.claude, "recursive-self-improvement",
                            "config", "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"components": {"daily_review": False},
                       "daily_proposal_limit": 2,
                       "proposals": {"folder": SINK_LITERAL,
                                     "uncapped_subdirs": ["permissions"]}},
                      fh, indent=2)
        return path

    def write_record_decision_shim(self):
        """A record-decision.py that records its argv instead of touching a
        real ledger. The drain 'firing' is exactly this file appearing."""
        path = os.path.join(self.claude, "permission-ledger",
                            "record-decision.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\n"
                     "import json, os, sys\n"
                     "marker = os.path.join(os.path.dirname(\n"
                     "    os.path.abspath(__file__)), 'drain-fired.json')\n"
                     "with open(marker, 'w', encoding='utf-8') as f:\n"
                     "    json.dump(sys.argv[1:], f)\n")
        os.chmod(path, 0o755)
        return path

    def write_proposal(self, subdir, name, text):
        d = os.path.join(self.sink, subdir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def env(self, **extra):
        e = dict(os.environ, **GIT_IDENTITY)
        e["HOME"] = self.home
        e.pop("PROPOSAL_GATE_CONFIG", None)
        e.pop("PERMISSION_LEDGER_DIR", None)
        e.update(extra)
        return e

    def drain_marker(self):
        return os.path.join(self.claude, "permission-ledger",
                            "drain-fired.json")

    def make_symlinks(self):
        os.symlink(self.sink, os.path.join(self.claude, "proposals"))
        os.symlink(os.path.join(self.sink, "rsi"),
                   os.path.join(self.claude, "recursive-self-improvement",
                                "proposals"))


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="proposals-sink-test-")
        os.chmod(self._tmp, 0o755)
        self.fx = FixtureHome(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def run_script(self, script, *args, env=None, expect=None):
        proc = subprocess.run(["bash", script, *args],
                              env=env or self.fx.env(),
                              capture_output=True, text=True)
        if expect is not None and proc.returncode != expect:
            self.fail("%s exited %d (want %d)\nSTDOUT:\n%s\nSTDERR:\n%s"
                      % (os.path.basename(script), proc.returncode, expect,
                         proc.stdout, proc.stderr))
        return proc


# --------------------------------------------------------------------------
# 1. The cross-chunk contract: one source of truth for the sink path.
# --------------------------------------------------------------------------
class TestSinkPathContract(unittest.TestCase):
    def test_rsi_config_declares_the_sink_folder(self):
        cfg = json.loads(read(RSI_CONFIG_PATH))
        self.assertEqual(cfg["proposals"]["folder"], SINK_LITERAL,
                         "recursive-self-improvement/config/config.json must "
                         "declare proposals.folder — it is the single source "
                         "of truth every chunk resolves the sink from")

    def test_existing_caps_are_untouched(self):
        cfg = json.loads(read(RSI_CONFIG_PATH))
        self.assertEqual(cfg["daily_proposal_limit"], 2)
        self.assertEqual(cfg["proposals"]["uncapped_subdirs"], ["permissions"])

    def test_proposal_paths_are_untracked_and_ignored(self):
        tracked = subprocess.run(
            ["git", "ls-files", "--", "proposals",
             "recursive-self-improvement/proposals"],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
        self.assertEqual(tracked, [],
                         "the proposals spine must be untracked in ~/.claude — "
                         "still tracked: %r" % (tracked[:5],))
        for path in ("proposals", "recursive-self-improvement/proposals"):
            rc = subprocess.run(["git", "check-ignore", "-q", path],
                                cwd=REPO_ROOT).returncode
            self.assertEqual(rc, 0, "%s must be gitignored, otherwise the "
                                    "cutover symlink dirties the tree" % path)


# --------------------------------------------------------------------------
# 2. Gate spine resolution.
# --------------------------------------------------------------------------
class TestGateSpineResolution(FixtureCase):
    def test_gate_default_spine_root_is_the_sink(self):
        self.assertEqual(gate.DEFAULTS["spine_root"], SINK_LITERAL)
        self.assertEqual(gate.DEFAULTS["gate_log"],
                         SINK_LITERAL + "/gate-log.jsonl")
        self.assertEqual(gate.DEFAULT_CONFIG_PATH,
                         SINK_LITERAL + "/gate-config.json")

    def test_gate_lock_stays_outside_the_spine(self):
        # Deliberate: push-proposals.sh commits the sink wholesale, so a lock
        # inside it would land in the pushed history every run.
        self.assertEqual(gate.GATE_LOCK_PATH, "~/.claude/logs/gate.lock")

    def test_sink_path_survives_require_under_home(self):
        # ~/.local/state/... is under $HOME, so the config guard must accept
        # it. Asserted rather than assumed.
        gate.require_under_home(os.path.expanduser(SINK_LITERAL), "spine_root")
        self.assertTrue(gate.contained(os.path.expanduser(SINK_LITERAL),
                                       os.path.expanduser("~")))

    def test_load_config_and_discovery_walk_the_sink(self):
        self.fx.build()
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        old_home = os.environ["HOME"]
        os.environ["HOME"] = self.fx.home
        try:
            cfg = gate.load_config(cfg_path, explicit=True)
            self.assertEqual(cfg["spine_root"], self.fx.sink)
            names = [n for n, _ in gate.discover_subdirs(cfg)]
        finally:
            os.environ["HOME"] = old_home
        for expected in ("rsi", "permissions", "manual", "research"):
            self.assertIn(expected, names)
        self.assertNotIn("archived", names)  # excluded_subdirs

    def test_gate_annotates_and_logs_inside_the_sink(self):
        """--list must see a sink proposal and the gate log must be sink-side."""
        self.fx.build()
        self.fx.write_proposal("rsi", "2026-08-30-open.md",
                               "---\nstatus: pending\n---\n\nbody\n")
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        old_home = os.environ["HOME"]
        os.environ["HOME"] = self.fx.home
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            rc = gate.main(["--config", cfg_path, "--list"])
        finally:
            sys.stdout = old_stdout
            os.environ["HOME"] = old_home
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn(os.path.join(self.fx.sink, "rsi", "2026-08-30-open.md"),
                      buf.getvalue())
        self.assertNotIn("would block push", buf.getvalue())


# --------------------------------------------------------------------------
# 3. Symlink resolution (both legacy names, absolute targets).
# --------------------------------------------------------------------------
class TestSymlinkResolution(FixtureCase):
    def test_cutover_creates_absolute_symlinks_to_the_sink(self):
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)

        pairs = [
            (os.path.join(self.fx.claude, "proposals"), self.fx.sink),
            (os.path.join(self.fx.claude, "recursive-self-improvement",
                          "proposals"), os.path.join(self.fx.sink, "rsi")),
        ]
        for link, want in pairs:
            self.assertTrue(os.path.islink(link), "%s is not a symlink" % link)
            target = os.readlink(link)
            self.assertTrue(target.startswith("/"),
                            "%s -> %s must be an absolute target" % (link, target))
            self.assertEqual(target, want)
            self.assertEqual(os.path.realpath(link), os.path.realpath(want))

    def test_gate_discovery_still_works_through_the_shim_symlink(self):
        """The legacy names stay usable BY READERS after the cutover."""
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        old_home = os.environ["HOME"]
        os.environ["HOME"] = self.fx.home
        try:
            cfg = gate.load_config(cfg_path, explicit=True)
            cfg["spine_root"] = os.path.join(self.fx.claude, "proposals")
            names = [n for n, _ in gate.discover_subdirs(cfg)]
        finally:
            os.environ["HOME"] = old_home
        self.assertIn("rsi", names)
        self.assertIn("permissions", names)


# --------------------------------------------------------------------------
# 4. The on-decision containment trap.
# --------------------------------------------------------------------------
class TestCallbackContainment(FixtureCase):
    def test_callback_file_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(ON_DECISION), ON_DECISION)
        self.assertFalse(os.path.islink(ON_DECISION))
        mode = os.stat(ON_DECISION).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "on-decision must be executable")
        self.assertFalse(mode & 0o022, "on-decision must not be group/other "
                                       "writable — run_callback refuses it")

    def test_drain_fires_end_to_end_after_the_cutover(self):
        """The acceptance test for A18: not that the path exists, that the
        permission-ledger drain actually runs with the right arguments while
        proposals/ is a symlink into the sink."""
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        proposal = self.fx.write_proposal(
            "permissions", "2026-08-30-bash-git-status.md",
            "---\nstatus: pending\npattern_key: Bash:git status\n---\n\n"
            "## Friction\n\n42 prompts in the window\n")
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")

        proc = subprocess.run(
            [sys.executable, GATE_PATH, "--config", cfg_path,
             "--run-callback", "permissions", proposal, "implemented"],
            env=self.fx.env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         "callback refused:\n%s\n%s" % (proc.stdout, proc.stderr))
        marker = self.fx.drain_marker()
        self.assertTrue(os.path.exists(marker),
                        "the permission-ledger drain never fired; gate said:\n%s"
                        % proc.stdout)
        self.assertEqual(json.loads(read(marker)),
                         ["Bash:git status", "implemented", "--count", "42"])

    def test_callback_outside_claude_is_refused_and_says_why(self):
        """The containment rule is a real safety property — a proposal file
        must not be able to nominate an arbitrary executable. Refusing is
        correct; refusing with a bare 1 is not."""
        self.fx.build()
        stray = os.path.join(self.fx.home, "stray-on-decision")
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(stray, 0o755)
        cfg_path = self.fx.write_gate_config(on_decision=stray)
        proposal = self.fx.write_proposal(
            "permissions", "2026-08-30-stray.md",
            "---\nstatus: pending\npattern_key: X\n---\n")

        proc = subprocess.run(
            [sys.executable, GATE_PATH, "--config", cfg_path,
             "--run-callback", "permissions", proposal, "implemented"],
            env=self.fx.env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        out = proc.stdout + proc.stderr
        self.assertIn("resolves outside", out)
        self.assertIn("containment rule", out.lower(),
                      "the refusal must name the containment rule and the "
                      "remedy, so the next person is not debugging a bare 1; "
                      "got:\n%s" % out)
        self.assertIn("permission-ledger", out,
                      "the refusal should point at the supported location for "
                      "a callback")


# --------------------------------------------------------------------------
# 5. Dedup: `rejected` is settled, and the reviewer still reads it.
# --------------------------------------------------------------------------
class TestDedupCoversRejected(unittest.TestCase):
    def test_rejected_is_a_settled_status(self):
        self.assertIn("rejected", gate.SETTLED_STATUSES)
        for value in ("rejected", '"rejected"', "rejected  # turned down"):
            self.assertEqual(
                gate.candidate_state("---\nstatus: %s\n---\n\nbody\n" % value),
                "settled", "status %r must not be re-gated" % value)

    def test_pending_is_not_settled(self):
        self.assertEqual(gate.candidate_state("---\nstatus: pending\n---\n"),
                         "pending")

    def test_reviewer_reads_the_rejected_corpus_from_the_sink(self):
        """prompt.md is the reviewer's ONLY semantic dedup signal. The nix
        wrapper overrides the write path but not this read path, so if it
        still points at the emptied legacy dir every rejected proposal
        regenerates the next night."""
        prompt = read(RSI_PROMPT_PATH)
        self.assertIn(SINK_LITERAL, prompt,
                      "prompt.md must read existing proposals from the sink")
        self.assertIn("rejected", prompt)
        stale = "`~/.claude/recursive-self-improvement/proposals/`"
        self.assertNotIn(stale, prompt,
                         "prompt.md still instructs reading/writing the legacy "
                         "dir (%s); after the relocation that dir is a "
                         "compatibility symlink, and the reviewer must resolve "
                         "the sink directly" % stale)

    def test_reviewer_push_step_still_names_the_push_script(self):
        self.assertIn("push-proposals.sh", read(RSI_PROMPT_PATH))


# --------------------------------------------------------------------------
# 6. Atomic proposal write in the permission-ledger sink.
# --------------------------------------------------------------------------
class _BrokenWriter:
    """A file object whose write() fails, standing in for a crash / ENOSPC
    part-way through persisting a proposal body."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False

    def write(self, data):
        raise OSError("simulated write failure")

    def flush(self):
        pass

    def fileno(self):
        return self._real.fileno()

    def close(self):
        self._real.close()


class TestAtomicProposalWrite(FixtureCase):
    BODY = ("===PROPOSAL: 2026-08-30-example.md===\n"
            "---\nstatus: pending\npattern_key: Bash:ls\n---\n\nbody line\n"
            "===END PROPOSAL===\n")

    def _run_sink(self, dest, stdin_text, fdopen=None):
        argv = ["proposal-sink.py", dest]
        old_argv, old_stdin, old_stdout = sys.argv, sys.stdin, sys.stdout
        old_fdopen = os.fdopen
        sys.argv = argv
        sys.stdin = io.TextIOWrapper(
            io.BytesIO(stdin_text.encode("utf-8")), encoding="utf-8")
        sys.stdout = io.StringIO()
        if fdopen is not None:
            os.fdopen = fdopen
        try:
            try:
                rc = psink.main()
            except OSError:
                rc = None
            return rc, sys.stdout.getvalue()
        finally:
            os.fdopen = old_fdopen
            sys.argv, sys.stdin, sys.stdout = old_argv, old_stdin, old_stdout

    def test_writes_a_complete_proposal(self):
        dest = os.path.join(self._tmp, "permissions")
        rc, out = self._run_sink(dest, self.BODY)
        self.assertEqual(rc, 0, out)
        written = os.path.join(dest, "2026-08-30-example.md")
        self.assertIn("body line", read(written))
        self.assertEqual(stat.S_IMODE(os.stat(written).st_mode), 0o600)

    def test_existing_proposal_is_never_overwritten(self):
        dest = os.path.join(self._tmp, "permissions")
        os.makedirs(dest)
        keep = os.path.join(dest, "2026-08-30-example.md")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL\n")
        rc, out = self._run_sink(dest, self.BODY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(keep), "ORIGINAL\n")
        self.assertIn("skipped existing", out)

    def test_cap_of_five_survives(self):
        dest = os.path.join(self._tmp, "permissions")
        blocks = "".join(
            "===PROPOSAL: 2026-08-30-p%d.md===\nbody\n===END PROPOSAL===\n" % i
            for i in range(8))
        rc, out = self._run_sink(dest, blocks)
        self.assertEqual(rc, 2, out)  # anomaly: cap reached
        self.assertEqual(
            len([f for f in os.listdir(dest) if f.endswith(".md")]), 5, out)

    def test_a_failed_write_leaves_no_partial_proposal(self):
        """O_EXCL makes the create race-safe; it does NOT make the body write
        crash-safe. A crash between create and write used to leave an empty
        file at the destination that the next run skips as 'already there' and
        triage reads as truncated evidence."""
        dest = os.path.join(self._tmp, "permissions")
        real_fdopen = os.fdopen

        def broken(*args, **kwargs):
            return _BrokenWriter(real_fdopen(*args, **kwargs))

        rc, out = self._run_sink(dest, self.BODY, fdopen=broken)
        final = os.path.join(dest, "2026-08-30-example.md")
        self.assertFalse(
            os.path.exists(final),
            "a failed body write left %s behind; the destination must only "
            "ever hold a complete proposal (tmp file + os.replace)" % final)
        leftovers = [f for f in os.listdir(dest)] if os.path.isdir(dest) else []
        self.assertEqual(leftovers, [],
                         "temp files leaked into the proposals dir: %r"
                         % leftovers)
        self.assertNotEqual(rc, 0,
                            "a failed write must be reported as an anomaly")


class TestPermissionLedgerWritesDirectly(unittest.TestCase):
    def test_run_evaluate_targets_the_sink_not_the_symlink(self):
        text = read(RUN_EVALUATE)
        self.assertIn(".local/state/claude-proposals", text,
                      "run-evaluate.sh must resolve the sink directly — a "
                      "writer that depends on a ~/.claude symlink existing is "
                      "a writer that fails silently when it does not")
        self.assertNotIn('PROPOSALS_DIR="$CLAUDE_HOME/proposals/permissions"',
                         text)


# --------------------------------------------------------------------------
# 7. push-proposals.sh — its job moved to the sink repo.
# --------------------------------------------------------------------------
class TestPushProposals(FixtureCase):
    def test_commits_in_the_sink_and_never_stages_through_a_symlink(self):
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.fx.write_proposal("rsi", "2026-08-30-new.md",
                               "---\nstatus: implemented\n---\n\nfresh\n")
        before = git("rev-parse", "HEAD", cwd=self.fx.sink,
                     env=self.fx.env()).strip()

        proc = self.run_script(PUSH_SCRIPT, expect=0)

        after = git("rev-parse", "HEAD", cwd=self.fx.sink,
                    env=self.fx.env()).strip()
        self.assertNotEqual(before, after,
                            "push-proposals.sh must commit in the SINK repo\n%s\n%s"
                            % (proc.stdout, proc.stderr))
        tracked = git("ls-files", "rsi/2026-08-30-new.md", cwd=self.fx.sink,
                      env=self.fx.env()).strip()
        self.assertEqual(tracked, "rsi/2026-08-30-new.md")
        # ~/.claude must be left alone entirely.
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.claude,
                             env=self.fx.env()).strip(), "")

    def test_missing_remote_is_a_notice_not_a_failure(self):
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.fx.write_proposal("rsi", "2026-08-30-noremote.md",
                               "---\nstatus: implemented\n---\n\nx\n")
        proc = self.run_script(PUSH_SCRIPT, expect=0)
        out = (proc.stdout + proc.stderr).lower()
        self.assertIn("no", out)
        self.assertIn("remote", out,
                      "a sink with no remote must produce a plain one-line "
                      "notice, not a failure:\n%s%s" % (proc.stdout, proc.stderr))

    def test_no_changes_is_a_clean_no_op(self):
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.run_script(PUSH_SCRIPT, expect=0)

    def test_guard_names_the_real_cause_when_a_shim_points_elsewhere(self):
        self.fx.build()
        elsewhere = os.path.join(self.fx.home, "elsewhere")
        os.makedirs(elsewhere)
        os.rmdir(os.path.join(self.fx.claude, "proposals"))
        os.symlink(elsewhere, os.path.join(self.fx.claude, "proposals"))

        proc = self.run_script(PUSH_SCRIPT)
        self.assertNotEqual(proc.returncode, 0)
        out = proc.stdout + proc.stderr
        self.assertIn(self.fx.sink, out,
                      "the guard must name the sink it expected:\n%s" % out)
        self.assertNotIn("install.sh", out,
                         "the old remedy sent operators chasing the wrong "
                         "thing:\n%s" % out)

    def test_guard_fails_loudly_when_the_sink_is_absent(self):
        self.fx.build()
        shutil.rmtree(self.fx.sink)
        proc = self.run_script(PUSH_SCRIPT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(self.fx.sink, proc.stdout + proc.stderr)


# --------------------------------------------------------------------------
# 8. The cutover script.
# --------------------------------------------------------------------------
class TestCutoverScript(FixtureCase):
    def test_refuses_while_the_paths_are_still_tracked(self):
        self.fx.build()
        legacy = os.path.join(self.fx.claude, "recursive-self-improvement",
                              "proposals", "2026-08-01-tracked.md")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("---\nstatus: implemented\n---\n\ntracked\n")
        env = self.fx.env()
        git("add", "-f", "recursive-self-improvement/proposals",
            cwd=self.fx.claude, env=env)
        git("commit", "-q", "-m", "track it", cwd=self.fx.claude, env=env)

        proc = self.run_script(CUTOVER_SCRIPT, env=env)
        self.assertNotEqual(proc.returncode, 0)
        out = (proc.stdout + proc.stderr).lower()
        self.assertIn("merge", out,
                      "the refusal must say the PR has to merge first:\n%s%s"
                      % (proc.stdout, proc.stderr))
        self.assertTrue(os.path.isfile(legacy), "nothing may be removed")

    def test_is_idempotent(self):
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        second = self.run_script(CUTOVER_SCRIPT, expect=0)
        self.assertIn("already", (second.stdout + second.stderr).lower())

    def test_syncs_live_only_content_into_the_sink_and_commits_it(self):
        self.fx.build()
        live = os.path.join(self.fx.claude, "recursive-self-improvement",
                            "proposals", "2026-08-02-live-only.md")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("---\nstatus: rejected\n---\n\nonly here\n")
        self.run_script(CUTOVER_SCRIPT, expect=0)

        copied = os.path.join(self.fx.sink, "rsi", "2026-08-02-live-only.md")
        self.assertTrue(os.path.isfile(copied),
                        "live-only content must be copied into the sink, "
                        "never dropped")
        self.assertEqual(read(copied), "---\nstatus: rejected\n---\n\nonly here\n")
        env = self.fx.env()
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.sink,
                             env=env).strip(), "",
                         "the sync must be committed in the sink")
        self.assertIn("rsi/2026-08-02-live-only.md",
                      git("ls-files", cwd=self.fx.sink, env=env))

    def test_legacy_gitignore_never_overwrites_the_sinks_own(self):
        """The sink's ignore policy is the opposite of the legacy one — out
        there everything is tracked, which is the point of the move."""
        self.fx.build()
        legacy = os.path.join(self.fx.claude, "proposals", ".gitignore")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("permissions/\ngate-log.jsonl\n")
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.assertEqual(read(os.path.join(self.fx.sink, ".gitignore")),
                         ".gate.lock\n")

    def test_installs_the_relocation_keys_into_the_sink_gate_config(self):
        """gate-config.json is spine config, and the move invalidates exactly
        three of its keys. The on_decision one is the trap: a callback under
        the spine now realpath-resolves outside ~/.claude, and run_callback
        refuses it — silently killing the permission-ledger drain."""
        self.fx.build()
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        legacy = {
            "spine_root": "~/.claude/proposals",
            "gate_log": "~/.claude/proposals/gate-log.jsonl",
            "on_decision": {
                "permissions": os.path.join(self.fx.claude, "proposals",
                                            "permissions", "on-decision"),
            },
            "scorer": {"model": "claude-fable-5", "batch_size": 7},
            "excluded_subdirs": [".*", "archived", "mine"],
        }
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh, indent=2)

        self.run_script(CUTOVER_SCRIPT, expect=0)

        cfg = json.loads(read(cfg_path))
        self.assertEqual(cfg["spine_root"], self.fx.sink)
        self.assertEqual(cfg["gate_log"], self.fx.sink + "/gate-log.jsonl")
        self.assertEqual(cfg["on_decision"]["permissions"],
                         os.path.join(self.fx.claude, "permission-ledger",
                                      "on-decision"),
                         "the callback is code, not data: it must live beside "
                         "record-decision.py inside ~/.claude, or the "
                         "containment check kills the drain")
        # Every other key is the user's and survives verbatim.
        self.assertEqual(cfg["scorer"], {"model": "claude-fable-5",
                                         "batch_size": 7})
        self.assertEqual(cfg["excluded_subdirs"], [".*", "archived", "mine"])
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.sink,
                             env=self.fx.env()).strip(), "")

    def test_handles_the_production_inverted_layout(self):
        """The live layout is inverted from its names: proposals/rsi is a
        symlink pointing BACK into recursive-self-improvement/proposals, which
        is the real directory. Both names have to end up pointing at the sink,
        and the rsi content must be absorbed exactly once."""
        self.fx.build()
        legacy_rsi = os.path.join(self.fx.claude, "recursive-self-improvement",
                                  "proposals")
        with open(os.path.join(legacy_rsi, "2026-08-04-inverted.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("---\nstatus: rejected\n---\n\ninverted\n")
        os.symlink(legacy_rsi, os.path.join(self.fx.claude, "proposals", "rsi"))

        self.run_script(CUTOVER_SCRIPT, expect=0)

        absorbed = os.path.join(self.fx.sink, "rsi", "2026-08-04-inverted.md")
        self.assertTrue(os.path.isfile(absorbed))
        self.assertFalse(os.path.islink(os.path.join(self.fx.sink, "rsi")),
                         "the sink's rsi must stay a real directory")
        self.assertEqual(
            os.readlink(os.path.join(self.fx.claude, "proposals")),
            self.fx.sink)
        self.assertEqual(os.readlink(legacy_rsi),
                         os.path.join(self.fx.sink, "rsi"))
        # The stale inner symlink went to the backup with its directory; it
        # was never copied into the sink as a file.
        self.assertFalse(os.path.exists(os.path.join(self.fx.sink, "rsi", "rsi")))

    def test_never_deletes_the_live_content(self):
        self.fx.build()
        live = os.path.join(self.fx.claude, "recursive-self-improvement",
                            "proposals", "2026-08-03-keepme.md")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("keep me\n")
        self.run_script(CUTOVER_SCRIPT, expect=0)
        backups = []
        for base, _dirs, files in os.walk(self.fx.backup):
            backups.extend(os.path.join(base, f) for f in files)
        self.assertTrue(any(f.endswith("2026-08-03-keepme.md") for f in backups),
                        "the replaced directories must be preserved, not "
                        "deleted; backup tree held %r" % backups)

    def test_aborts_without_touching_anything_when_the_sink_is_not_a_repo(self):
        self.fx.build()
        shutil.rmtree(os.path.join(self.fx.sink, ".git"))
        live_dir = os.path.join(self.fx.claude, "recursive-self-improvement",
                                "proposals")
        proc = self.run_script(CUTOVER_SCRIPT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.islink(live_dir),
                         "nothing may be swapped when the sink cannot be "
                         "verified")
        self.assertTrue(os.path.isdir(live_dir))

    def test_prints_what_it_did(self):
        self.fx.build()
        proc = self.run_script(CUTOVER_SCRIPT, expect=0)
        out = proc.stdout + proc.stderr
        self.assertIn(self.fx.sink, out)
        self.assertIn("proposals", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

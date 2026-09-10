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
  * ledger backfill — the dedup guarantee is only as strong as
    `.index.jsonl`, and every proposal predating the ledger has to be
    recorded in it with the SHIPPED sink's own id algorithm, or a drained
    rejection regenerates the next night under a fresh date prefix.

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
PUSHER_PATH = os.path.join(REPO_ROOT, "scripts", "claude-proposals-push.sh")
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
        # The sink's throwaway origin, and an empty directory that stands in
        # for this machine's global git hooks. Both live OUTSIDE $HOME so no
        # tree-cleanliness assertion has to know about them.
        self.sink_origin = os.path.join(root, "sink-origin.git")
        self.nohooks = os.path.join(root, "nohooks")

    # -- construction ----------------------------------------------------
    def build(self, legacy_dirs=True, rsi_config=True):
        os.makedirs(self.nohooks, exist_ok=True)
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
        # push-proposals.sh delegates its push here, so the fixture has to
        # carry it or every push test measures a missing file rather than the
        # behaviour it names.
        pusher = os.path.join(self.claude, "scripts", "claude-proposals-push.sh")
        shutil.copy2(PUSHER_PATH, pusher)
        os.chmod(pusher, 0o755)
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
        self.give_sink_a_remote()
        return self

    def init_git(self, path):
        env = dict(os.environ, **GIT_IDENTITY)
        git("init", "-q", "-b", "main", cwd=path, env=env)
        # Neutralise this machine's GLOBAL hooks (core.hooksPath in
        # nixos-config home/jonathan.nix runs gitleaks on every commit). The
        # fixture must measure the scripts, not the host's ambient config, and
        # a per-commit scan makes the suite crawl.
        git("config", "core.hooksPath", self.nohooks, cwd=path, env=env)
        git("add", "-A", cwd=path, env=env)
        git("commit", "-q", "-m", "fixture", "--allow-empty", cwd=path,
            env=env)

    def give_sink_a_remote(self):
        """A throwaway bare origin for the sink.

        The sink acquired a private GitHub remote on 2026-09-03, and
        push-proposals.sh now delegates its push to
        scripts/claude-proposals-push.sh. A remote-less fixture would therefore
        exercise only the refusal path and never the push itself — and would
        keep asserting a "no remote is fine" contract that stopped being true
        the day the sink got one.
        """
        env = dict(os.environ, **GIT_IDENTITY)
        git("init", "-q", "--bare", "-b", "main", self.sink_origin,
            cwd=self.root, env=env)
        git("remote", "add", "origin", self.sink_origin, cwd=self.sink, env=env)
        git("push", "-q", "-u", "origin", "main", cwd=self.sink, env=env)

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
        # Two independent properties, asserted as properties rather than as one
        # literal path. Pinning the literal is what made this test break when
        # the lock moved out of ~/.claude/logs into the state dir: the path was
        # incidental, the guarantees are not.
        #
        #   1. Not in the spine — push-proposals.sh commits the sink wholesale,
        #      so a lock inside it would land in the pushed history every run.
        #   2. Not in ~/.claude either — that is the live config repo, and
        #      runtime state there dirties the checkout. It survived under
        #      logs/ only because a `*.lock` catch-all happened to cover it.
        lock = os.path.expanduser(gate.GATE_LOCK_PATH)
        self.assertFalse(
            gate.contained(lock, os.path.expanduser(SINK_LITERAL)),
            f"gate lock {lock} is inside the spine; it would be committed",
        )
        self.assertFalse(
            gate.contained(lock, os.path.expanduser("~/.claude")),
            f"gate lock {lock} is inside the live config repo",
        )

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

    def test_gate_refuses_a_config_that_points_away_from_its_own_spine(self):
        """gate-config.json ships INSIDE the sink, and push-proposals.sh
        commits that sink. A spine_root pointing anywhere else means the gate
        scores one tree while the commit ships another — the sink goes out
        ungated and nothing says so.

        Reproduced against the real box on 2026-08-31: the sink's own
        gate-config still carried the pre-move `~/.claude/proposals`, so
        `proposal-intake-gate.py --list` printed 15 `would gate:` lines under
        /home/jonathan/.claude/proposals while push-proposals.sh committed
        ~/.local/state/claude-proposals.
        """
        self.fx.build()
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        cfg = json.loads(read(cfg_path))
        cfg["spine_root"] = os.path.join(self.fx.claude, "proposals")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

        proc = subprocess.run([sys.executable, GATE_PATH, "--list"],
                              env=self.fx.env(), capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2,
                         "a spine mismatch must be a config error, not a "
                         "silent walk of the wrong tree:\n%s" % out)
        self.assertNotIn("would gate:", out,
                         "nothing may be gated against the wrong spine")
        self.assertIn("cutover-proposals.sh", out,
                      "the refusal must name the step that fixes it:\n%s" % out)

    def test_gate_refuses_a_gate_log_outside_the_spine(self):
        """Same class: a gate_log still under ~/.claude writes the gate's own
        audit trail into the live config checkout every run."""
        self.fx.build()
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        cfg = json.loads(read(cfg_path))
        cfg["gate_log"] = os.path.join(self.fx.claude, "proposals",
                                       "gate-log.jsonl")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

        proc = subprocess.run([sys.executable, GATE_PATH, "--list"],
                              env=self.fx.env(), capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, out)
        self.assertIn("cutover-proposals.sh", out, out)

    def test_the_shim_spine_root_is_still_accepted_after_the_cutover(self):
        """The check compares realpaths, so a config that names the legacy
        `~/.claude/proposals` keeps working once that name is a symlink into
        the sink. Refusing it would break readers the shim exists for."""
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        cfg_path = os.path.join(self.fx.sink, "gate-config.json")
        cfg = json.loads(read(cfg_path))
        cfg["spine_root"] = os.path.join(self.fx.claude, "proposals")
        cfg["gate_log"] = os.path.join(self.fx.claude, "proposals",
                                       "gate-log.jsonl")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

        proc = subprocess.run([sys.executable, GATE_PATH, "--list"],
                              env=self.fx.env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

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

    def test_daily_review_uses_bounded_completed_reflections(self):
        prompt = read(RSI_PROMPT_PATH)
        self.assertIn("~/.claude/reflections/sessions/*/final.json", prompt)
        self.assertIn("last 24 hours", prompt)
        self.assertIn("12 newest", prompt)
        self.assertIn("32 KiB", prompt)
        self.assertIn('modes.sessionend.status == "complete"', prompt)
        self.assertIn("subagents/", prompt)
        self.assertIn("never follow `source_transcript`", prompt.lower())
        self.assertNotIn("Find all `.jsonl` files modified", prompt)
        self.assertIn("Do not preload the proposal corpus", prompt)
        self.assertNotIn("proposal filenames, frontmatter, and headings", prompt)

        with open(RSI_CONFIG_PATH, encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertIs(config["components"]["daily_review"], True,
                      "bounded reflection input is required before enabling "
                      "the scheduled daily review")


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

    def test_the_commit_reaches_the_remote(self):
        """Committing locally is half the job; the sink exists off-machine too.

        push-proposals.sh no longer pushes itself — it delegates to
        scripts/claude-proposals-push.sh so there is exactly one pusher and
        therefore exactly one mandatory gitleaks gate. This asserts the
        delegation actually happens end to end, at the only place it is
        falsifiable: the origin's tip.
        """
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.fx.write_proposal("rsi", "2026-08-30-new.md",
                               "---\nstatus: implemented\n---\n\nfresh\n")

        proc = self.run_script(PUSH_SCRIPT, expect=0)

        local = git("rev-parse", "HEAD", cwd=self.fx.sink,
                    env=self.fx.env()).strip()
        remote = git("--git-dir", self.fx.sink_origin, "rev-parse", "main",
                     cwd=self.fx.root, env=self.fx.env()).strip()
        self.assertEqual(local, remote,
                         "the sink's commit did not reach its remote\n%s\n%s"
                         % (proc.stdout, proc.stderr))

    def test_unpushed_commits_are_pushed_even_with_nothing_new_to_commit(self):
        """The blind spot the delegation closes.

        This script used to RETURN on "no proposal changes to commit" and never
        reach a push, so a commit made by hand — or by a run whose push failed
        — sat unpushed indefinitely. "Is the sink backed up?" is not the same
        question as "did this run produce a proposal?".
        """
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.fx.write_proposal("rsi", "2026-08-30-byhand.md",
                               "---\nstatus: implemented\n---\n\nx\n")
        env = self.fx.env()
        git("add", "-A", cwd=self.fx.sink, env=env)
        git("commit", "-q", "-m", "committed by hand", cwd=self.fx.sink, env=env)
        local = git("rev-parse", "HEAD", cwd=self.fx.sink, env=env).strip()

        proc = self.run_script(PUSH_SCRIPT, expect=0)

        remote = git("--git-dir", self.fx.sink_origin, "rev-parse", "main",
                     cwd=self.fx.root, env=env).strip()
        self.assertEqual(local, remote,
                         "a clean tree with unpushed commits must still be "
                         "pushed\n%s\n%s" % (proc.stdout, proc.stderr))

    def test_missing_remote_is_a_hard_failure(self):
        """Inverted on 2026-09-03, deliberately.

        This used to assert "a sink with no remote is a plain notice, not a
        failure", on the reasoning that having no remote was the expected
        steady state and creating one was a decision nobody had made. That
        decision has now been made: the sink has a private GitHub origin and a
        timer that keeps it pushed. A sink with no remote is therefore not a
        steady state any more, it is a sink whose contents exist on one disk —
        which is precisely the failure the push path exists to end, and it has
        to be loud.
        """
        self.fx.build()
        self.run_script(CUTOVER_SCRIPT, expect=0)
        git("remote", "remove", "origin", cwd=self.fx.sink, env=self.fx.env())
        self.fx.write_proposal("rsi", "2026-08-30-noremote.md",
                               "---\nstatus: implemented\n---\n\nx\n")

        proc = self.run_script(PUSH_SCRIPT)

        self.assertNotEqual(proc.returncode, 0,
                            "a sink with nowhere to push must fail loudly:\n%s%s"
                            % (proc.stdout, proc.stderr))
        self.assertIn("remote", (proc.stdout + proc.stderr).lower())
        # The gating work still committed — the refusal is about the push.
        self.assertEqual(
            git("ls-files", "rsi/2026-08-30-noremote.md", cwd=self.fx.sink,
                env=self.fx.env()).strip(),
            "rsi/2026-08-30-noremote.md")

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

    def test_refuses_to_ship_before_the_cutover_has_run(self):
        """The ordering dependency, enforced rather than documented.

        Before the cutover, `~/.claude/proposals` is a real directory holding
        a DIFFERENT tree from the sink, and the sink's own gate-config still
        describes that live tree. The old guard printed a note and said it was
        "Gating and committing $SINK regardless" — it was not: the gate scored
        ~/.claude/proposals and the sink shipped ungated.
        """
        self.fx.build()
        env = self.fx.env()
        before = git("rev-parse", "HEAD", cwd=self.fx.sink, env=env).strip()
        # Settled on purpose: with nothing eligible the gate exits 0, so the
        # only thing that can fail this run is the ordering guard itself.
        self.fx.write_proposal("rsi", "2026-08-30-settled-two.md",
                               "---\nstatus: implemented\n---\n\nbody\n")

        proc = self.run_script(PUSH_SCRIPT)

        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0,
                            "shipping an ungated sink must fail closed:\n%s" % out)
        self.assertIn("error:", out,
                      "a note is not a refusal; the run must stop:\n%s" % out)
        self.assertIn("cutover-proposals.sh", out,
                      "the refusal must name the step that fixes it:\n%s" % out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.sink,
                             env=env).strip(), before,
                         "nothing may be committed while the trees disagree")


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
        # Committed, because that is what the production sink looks like — and
        # because the cutover now refuses to run against a dirty sink at all.
        self._commit_sink("legacy gate config")

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

    # -- never destroy work the sink's git history does not hold -------------

    def _commit_sink(self, message="sink baseline"):
        env = self.fx.env()
        git("add", "-A", cwd=self.fx.sink, env=env)
        git("commit", "-q", "-m", message, "--allow-empty", cwd=self.fx.sink,
            env=env)

    def _put_live(self, rel, text):
        path = os.path.join(self.fx.claude, "proposals", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_uncommitted_sink_content_is_never_destroyed(self):
        """The sync leaned on the sink's git history as its safety net, and an
        uncommitted edit is not in it. The clean-sink guard that would have
        caught this sat AFTER the overwrite and after the commit.

        Reproduced 2026-08-31: a sink-only edit to a `manual/` proposal showing
        ` M` in `git status` came back reverted to the ~/.claude version, and
        `git log -S '<the edit>' --all` in the sink returned zero commits.
        """
        self.fx.build()
        sink_file = self.fx.write_proposal("manual", "2026-08-05-shared.md",
                                           "committed body\n")
        self._commit_sink()
        with open(sink_file, "w", encoding="utf-8") as fh:
            fh.write("EDITED IN THE SINK, NOT YET COMMITTED\n")
        self._put_live("manual/2026-08-05-shared.md", "committed body\n")

        proc = self.run_script(CUTOVER_SCRIPT)

        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0,
                            "a dirty sink must abort BEFORE anything is "
                            "written:\n%s" % out)
        self.assertEqual(read(sink_file),
                         "EDITED IN THE SINK, NOT YET COMMITTED\n",
                         "the uncommitted edit was destroyed and it is in no "
                         "commit anywhere")
        self.assertIn("uncommitted", out.lower(),
                      "the refusal must name what it found:\n%s" % out)
        self.assertFalse(
            os.path.islink(os.path.join(self.fx.claude, "proposals")),
            "nothing may be swapped after an abort")

    def test_a_dirty_sink_aborts_before_copying_anything_in(self):
        """Ordering, asserted directly: the guard has to run before the first
        mutation, not after the commit."""
        self.fx.build()
        self.fx.write_proposal("manual", "2026-08-05-dirty.md", "v1\n")
        self._commit_sink()
        with open(os.path.join(self.fx.sink, "manual",
                               "2026-08-05-dirty.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("v2 uncommitted\n")
        self._put_live("manual/2026-08-05-liveonly.md", "live only\n")
        env = self.fx.env()
        head_before = git("rev-parse", "HEAD", cwd=self.fx.sink, env=env).strip()

        self.run_script(CUTOVER_SCRIPT)

        self.assertFalse(
            os.path.exists(os.path.join(self.fx.sink, "manual",
                                        "2026-08-05-liveonly.md")),
            "the sync ran anyway; the guard is still downstream of it")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.sink,
                             env=env).strip(), head_before,
                         "nothing may be committed on an abort")

    def test_diverging_content_aborts_with_the_diff(self):
        """A live file and a sink file of the same name with different bytes
        is a fact the script cannot resolve. Preferring the ~/.claude copy was
        only ever defensible on the claim that the sink's version stays in git
        history — which is exactly what the case above disproves. Divergence
        is a stop, and the operator gets the diff."""
        self.fx.build()
        self.fx.write_proposal("manual", "2026-08-06-both.md", "SINK VERSION\n")
        self._commit_sink()
        self._put_live("manual/2026-08-06-both.md", "LIVE VERSION\n")

        proc = self.run_script(CUTOVER_SCRIPT)

        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("2026-08-06-both.md", out,
                      "the refusal must name the file:\n%s" % out)
        self.assertIn("LIVE VERSION", out,
                      "the refusal must show the diff, not just assert one:\n%s"
                      % out)
        self.assertIn("SINK VERSION", out, out)
        self.assertEqual(read(os.path.join(self.fx.sink, "manual",
                                           "2026-08-06-both.md")),
                         "SINK VERSION\n",
                         "neither side may be picked automatically")
        self.assertFalse(
            os.path.islink(os.path.join(self.fx.claude, "proposals")))

    # -- the sink has to actually be outside ~/.claude -----------------------

    def test_refuses_a_sink_that_lives_inside_the_live_config_repo(self):
        """`proposals.folder` still naming `~/.claude/proposals` passed
        preflight, because preflight only asked "is it *a* git repo" — and git
        answers that about the ENCLOSING repo.

        Reproduced 2026-08-31, exit 0 with no warning: `git -C <sink> add -A &&
        commit` swept an unrelated WIP settings.json and an untracked file into
        a commit on the live config repo, and the swap left `proposals` a
        symlink to itself with all content moved to the backup dir.
        """
        self.fx.build()
        inside = os.path.join(self.fx.claude, "proposals")
        os.makedirs(os.path.join(inside, "rsi"), exist_ok=True)
        cfg_path = os.path.join(self.fx.claude, "recursive-self-improvement",
                                "config", "config.json")
        doc = json.loads(read(cfg_path))
        doc["proposals"]["folder"] = inside
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        wip = os.path.join(self.fx.claude, "settings.json")
        with open(wip, "w", encoding="utf-8") as fh:
            fh.write('{"unrelated": "work in progress"}\n')
        env = self.fx.env()
        head_before = git("rev-parse", "HEAD", cwd=self.fx.claude,
                          env=env).strip()

        proc = self.run_script(CUTOVER_SCRIPT)

        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0,
                            "a sink inside ~/.claude must be refused:\n%s" % out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.claude,
                             env=env).strip(), head_before,
                         "the cutover committed to the LIVE config repo")
        self.assertIn("settings.json",
                      git("status", "--porcelain", cwd=self.fx.claude, env=env),
                      "unrelated WIP was swept into a commit")
        self.assertFalse(os.path.islink(inside),
                         "the swap created a self-referential symlink")

    def test_refuses_a_sink_that_is_merely_inside_some_other_git_repo(self):
        """`git rev-parse --show-toplevel` succeeding says nothing about the
        sink being a repo — only about it being inside one. `git add -A` there
        stages the enclosing repo's whole worktree."""
        self.fx.build()
        outer = os.path.join(self.fx.home, "outer-repo")
        nested = os.path.join(outer, "claude-proposals")
        os.makedirs(os.path.join(nested, "rsi"))
        self.fx.init_git(outer)
        cfg_path = os.path.join(self.fx.claude, "recursive-self-improvement",
                                "config", "config.json")
        doc = json.loads(read(cfg_path))
        doc["proposals"]["folder"] = nested
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        proc = self.run_script(CUTOVER_SCRIPT)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn(nested, out, out)

    # -- symlinked proposal files ------------------------------------------

    def test_symlinked_proposal_files_are_synced_and_verified(self):
        """`find -type f` lstats, so a symlinked .md in the live tree was
        neither copied into the sink nor looked at by the verify pass. It just
        stopped existing in the spine at the swap, with no log line — the
        content surviving only in the backup directory.

        The target is inside the proposal tree. It used to be an arbitrary
        `$HOME` path, which is the same shape as a symlink to `~/.ssh/id_*` —
        see `test_a_symlink_to_a_credential_in_home_is_refused` for why a
        target outside the two proposal trees is now a refusal. What is under
        test here is the ENUMERATION: a symlink is not a `-type f` and was
        therefore invisible to all three passes at once.
        """
        self.fx.build()
        target = os.path.join(self.fx.claude, "recursive-self-improvement",
                              "proposals", "elsewhere-note.md")
        body = "---\nstatus: rejected\n---\n\nlinked body\n"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(body)
        link = os.path.join(self.fx.claude, "recursive-self-improvement",
                            "proposals", "2026-08-07-linked.md")
        os.symlink(target, link)

        self.run_script(CUTOVER_SCRIPT, expect=0)

        landed = os.path.join(self.fx.sink, "rsi", "2026-08-07-linked.md")
        self.assertTrue(os.path.isfile(landed),
                        "a symlinked proposal vanished from the spine")
        self.assertFalse(os.path.islink(landed),
                         "the sink must hold the content, not a pointer that "
                         "breaks the moment the backup is deleted")
        self.assertEqual(read(landed), body)
        self.assertIn("rsi/2026-08-07-linked.md",
                      git("ls-files", cwd=self.fx.sink, env=self.fx.env()),
                      "and it must be committed like every other proposal")

    def test_a_symlink_escaping_home_is_refused_rather_than_absorbed(self):
        """Proposals are written by unattended agents. Following a symlink out
        of $HOME would copy whatever it names into a git repo."""
        self.fx.build()
        link = os.path.join(self.fx.claude, "recursive-self-improvement",
                            "proposals", "2026-08-08-escape.md")
        os.symlink("/etc/hostname", link)

        proc = self.run_script(CUTOVER_SCRIPT)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("2026-08-08-escape.md", out, out)
        self.assertFalse(
            os.path.exists(os.path.join(self.fx.sink, "rsi",
                                        "2026-08-08-escape.md")))


# --------------------------------------------------------------------------
# 8b. Node classification: the cutover enumerates EVERY node and refuses the
#     ones it cannot place.
#
# Every defect below has the same shape. The script enumerated with `find`
# predicates that SELECT (`-xtype f` for content, `-xtype l` for broken links)
# and it checked properties on one side of a src/dst pair but not the other.
# A node matching neither predicate is invisible to all three passes at once —
# never copied, never verified, never logged — so it simply stops existing in
# the spine at the swap, at exit 0. A collision whose *sink* side is a symlink
# passes `-e` and `cmp` for reasons that have nothing to do with the content
# being safe.
#
# The fix is not another predicate. The enumeration no longer selects: it
# yields every node and classifies each into one of a closed set, with a final
# arm that refuses. These tests pin the refusals — and, just as importantly,
# pin the shapes that must keep working, because a fail-closed enumeration is
# only as good as its accept list.
# --------------------------------------------------------------------------
class TestCutoverNodeClassification(FixtureCase):
    def setUp(self):
        super().setUp()
        self.fx.build()
        self.live = os.path.join(self.fx.claude, "proposals")
        self.rsi_live = os.path.join(self.fx.claude,
                                     "recursive-self-improvement", "proposals")

    # -- helpers ---------------------------------------------------------
    def commit_sink(self, message="sink baseline"):
        env = self.fx.env()
        git("add", "-A", cwd=self.fx.sink, env=env)
        git("commit", "-q", "-m", message, "--allow-empty", cwd=self.fx.sink,
            env=env)

    def sink_status(self):
        return git("status", "--porcelain", cwd=self.fx.sink,
                   env=self.fx.env()).strip()

    def sink_head(self):
        return git("rev-parse", "HEAD", cwd=self.fx.sink,
                   env=self.fx.env()).strip()

    def put_live(self, rel, text, root=None):
        path = os.path.join(root or self.live, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def assert_nothing_happened(self, proc):
        """The refusal contract: no swap, no copy, no commit.

        "no copy" is the ordering assertion. Every refusal below is knowable
        without touching anything, so it has to land in the pass that mutates
        nothing — a refusal raised halfway through the sync is the wedge, not
        the fix."""
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertTrue(os.path.isdir(self.live)
                        and not os.path.islink(self.live),
                        "the live tree was swapped despite the refusal")
        self.assertNotIn("copied into the sink", out,
                         "the refusal came after the sync had already started "
                         "writing:\n%s" % out)
        self.assertEqual(self.sink_status(), "",
                         "a half-written sink wedges every later run behind "
                         "the clean-sink gate — the worst outcome there is")
        return out

    def shim(self, name, body, **fields):
        """Put `name` on PATH ahead of the real one, and return an env for it.

        The shims here are fault/timing injection, not stubs: each one falls
        through to the real binary via $REAL_PATH."""
        d = os.path.join(self.fx.home, "shim")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return self.fx.env(PATH=d + os.pathsep + os.environ["PATH"],
                           REAL_PATH=os.environ["PATH"], **fields)

    def date_shim(self, **fields):
        """A `date` on PATH that fires once, at the one moment the script
        calls it: immediately before the swap loop, after verify has passed.
        That is the window this suite needs to write into."""
        return self.shim(
            "date",
            "#!/usr/bin/env bash\n"
            'if [[ -n "${RACE_FILE:-}" && ! -e "$RACE_FLAG" ]]; then\n'
            '  : > "$RACE_FLAG"\n'
            '  printf \'RACED PROPOSAL BODY\\n\' > "$RACE_FILE"\n'
            "fi\n"
            'if [[ -n "${FIXED_STAMP:-}" ]]; then\n'
            '  printf \'%s\\n\' "$FIXED_STAMP"; exit 0\n'
            "fi\n"
            'PATH="$REAL_PATH" exec date "$@"\n',
            RACE_FLAG=os.path.join(self.fx.home, ".raced"), **fields)

    # -- 1. a symlink to a DIRECTORY -------------------------------------
    def test_a_symlinked_directory_is_refused_not_silently_dropped(self):
        """`-xtype f` does not match a symlink to a directory, `-xtype l` does
        not match it, and find does not descend into it. Measured before the
        fix: exit 0, "cutover: done", the archived body in neither the sink nor
        its git history, `~/.claude/proposals/oldarchive` a 404, and the backup
        holding nothing but the pointer."""
        archive = os.path.join(self.fx.home, "proposal-archive-2025")
        os.makedirs(archive)
        with open(os.path.join(archive, "2025-11-old.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("ARCHIVED PROPOSAL BODY\n")
        os.symlink(archive, os.path.join(self.live, "oldarchive"))
        head = self.sink_head()

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))

        self.assertIn("oldarchive", out, out)
        self.assertIn(archive, out,
                      "the refusal must name the resolved target, not just "
                      "the link:\n%s" % out)
        self.assertEqual(self.sink_head(), head)
        self.assertFalse(os.path.exists(os.path.join(self.fx.sink,
                                                     "oldarchive")))
        self.assertTrue(os.path.isfile(os.path.join(archive, "2025-11-old.md")))

    def test_a_symlinked_directory_inside_the_trees_is_accepted(self):
        """The production layout depends on exactly this: `proposals/rsi` is a
        directory symlink pointing back at the OTHER live proposal tree. A
        fail-closed enumeration that refused it would refuse the real cutover,
        so the accept case is pinned as hard as the refusal."""
        self.put_live("manual/inner.md", "inner body\n")
        os.symlink(os.path.join(self.live, "manual"),
                   os.path.join(self.live, "alias"))

        proc = self.run_script(CUTOVER_SCRIPT, expect=0)

        self.assertIn("alias", proc.stdout + proc.stderr,
                      "an accepted-but-not-recreated node must still be "
                      "logged; silence is what made this class of bug "
                      "invisible")
        self.assertEqual(read(os.path.join(self.fx.sink, "manual",
                                           "inner.md")), "inner body\n")
        self.assertFalse(os.path.exists(os.path.join(self.fx.sink, "alias",
                                                     "inner.md")),
                         "the target is absorbed in its own right; absorbing "
                         "it twice under the alias would duplicate proposals")

    # -- 2. the SINK side of a collision ---------------------------------
    def test_a_dangling_symlink_on_the_sink_side_is_refused(self):
        """`[[ -e $dst ]]` is false for a dangling link, so the sync tried to
        `cp` through it: coreutils refused, `set -e` aborted mid-sync, and the
        sink was left dirty — which the clean-sink gate then turned into a
        permanent block on every later run."""
        self.put_live("manual/newthing.md", "SECRET PROPOSAL A\n")
        outside = os.path.join(self.fx.home, "outside-the-sink")
        os.makedirs(outside)
        os.makedirs(os.path.join(self.fx.sink, "manual"), exist_ok=True)
        os.symlink(os.path.join(outside, "leaked.md"),
                   os.path.join(self.fx.sink, "manual", "newthing.md"))
        self.commit_sink("sink holds a dangling link")
        head = self.sink_head()

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))

        self.assertIn("newthing.md", out, out)
        self.assertEqual(self.sink_head(), head)
        self.assertFalse(os.path.exists(os.path.join(outside, "leaked.md")),
                         "the copy was written straight through the sink and "
                         "landed outside it")

    def test_a_sink_symlink_into_the_live_tree_is_refused(self):
        """`-e` is true so the sync skips it; verify's `-f` and `cmp` follow
        the link and compare the live file with ITSELF, so verify passes and
        the run exits 0. After the swap the path gives ELOOP, the committed
        blob is the link text, and the only real copy is in the backup the
        script's last line invites the user to delete."""
        live_file = self.put_live("manual/b.md", "PROPOSAL B BODY\n")
        os.makedirs(os.path.join(self.fx.sink, "manual"), exist_ok=True)
        os.symlink(live_file, os.path.join(self.fx.sink, "manual", "b.md"))
        self.commit_sink("sink links into the live tree")

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))

        self.assertIn("b.md", out, out)
        self.assertEqual(read(live_file), "PROPOSAL B BODY\n")

    # -- 3. a pre-existing symlink AT a target path ----------------------
    def test_a_target_path_symlinked_over_real_content_is_refused(self):
        """`[[ -d $live && ! -L $live ]]` made scan, sync and verify all
        `return 0`, and the swap then `rm`'d the pointer: the content behind it
        reached neither the sink nor the backup, at exit 0. Reachable
        operationally by re-running after `proposals.folder` is repointed."""
        orphan = os.path.join(self.fx.home, "orphan-proposals")
        os.makedirs(os.path.join(orphan, "manual"))
        with open(os.path.join(orphan, "manual", "orphan.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("ORPHANED PROPOSAL BODY\n")
        shutil.rmtree(self.live)
        os.symlink(orphan, self.live)

        proc = self.run_script(CUTOVER_SCRIPT)
        out = proc.stdout + proc.stderr

        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn(orphan, out, out)
        self.assertEqual(os.readlink(self.live), orphan,
                         "the pointer was removed and the content behind it "
                         "was never absorbed")
        self.assertTrue(os.path.isfile(os.path.join(orphan, "manual",
                                                    "orphan.md")))
        self.assertEqual(self.sink_status(), "")

    def test_a_dangling_target_path_is_replaced(self):
        """A pointer with no content behind it loses nothing when replaced —
        this must NOT be swept up by the refusal above."""
        shutil.rmtree(self.live)
        os.symlink(os.path.join(self.fx.home, "does-not-exist"), self.live)
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.assertEqual(os.readlink(self.live), self.fx.sink)

    def test_a_self_looping_target_path_is_replaced(self):
        shutil.rmtree(self.live)
        os.symlink(self.live, self.live)
        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.assertEqual(os.readlink(self.live), self.fx.sink)

    # -- 4. a failure inside the sync must not wedge the tool ------------
    def test_a_failed_copy_rolls_back_so_the_next_run_is_not_blocked(self):
        """Any failure inside the sync aborted under `set -e` with a raw
        coreutils message and left the sink dirty; the clean-sink gate then
        refused every re-run until a human intervened. The script's own comment
        calls that the worst outcome, so the sync now unwinds itself.

        The fault is injected rather than contrived: `cp` fails on its second
        call, so the first copy has already landed in the sink when the abort
        happens. ENOSPC, EIO and a writer removing a live file between pass 1
        and the copy all arrive exactly like this, and no amount of preflight
        rules them out — which is why the recovery has to be structural."""
        self.put_live("manual/one.md", "first\n")
        self.put_live("manual/two.md", "second\n")
        env = self.shim(
            "cp",
            "#!/usr/bin/env bash\n"
            'n=0; [[ -f "$CP_COUNT" ]] && n=$(cat "$CP_COUNT")\n'
            'n=$((n+1)); printf %s "$n" > "$CP_COUNT"\n'
            'if [[ "$n" == "$CP_FAIL_AT" ]]; then\n'
            '  echo "cp: injected failure on call $n" >&2; exit 1\n'
            "fi\n"
            'PATH="$REAL_PATH" exec cp "$@"\n',
            CP_COUNT=os.path.join(self.fx.home, ".cpcount"), CP_FAIL_AT="2")

        proc = self.run_script(CUTOVER_SCRIPT, env=env)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("copied into the sink", out,
                      "the fault has to land mid-sync for this to test "
                      "anything:\n%s" % out)
        self.assertEqual(self.sink_status(), "",
                         "the partial sync was left behind; the next run dies "
                         "on the clean-sink gate and only a human gets it "
                         "moving again")

        self.run_script(CUTOVER_SCRIPT, expect=0)
        for name, body in (("one.md", "first\n"), ("two.md", "second\n")):
            self.assertEqual(read(os.path.join(self.fx.sink, "manual", name)),
                             body)

    def test_a_failed_commit_rolls_back_so_the_next_run_is_not_blocked(self):
        """The same unwind, one step later, where it is easy to get wrong.

        By the time the commit runs, `git add -A` has already staged every
        copy. Removing those files from the worktree without unstaging them
        leaves the sink dirty as `AD` — the exact block the unwind exists to
        prevent, so an unwind that only deletes files is no unwind at all. The
        index is reset (MIXED, never --hard; the clean-sink gate already proved
        nothing else was staged) before the worktree is touched."""
        self.put_live("manual/one.md", "first\n")
        env = self.shim(
            "git",
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [[ "$a" == commit ]]; then\n'
            '    echo "git: injected commit failure" >&2; exit 1\n'
            "  fi\n"
            "done\n"
            'PATH="$REAL_PATH" exec git "$@"\n')

        proc = self.run_script(CUTOVER_SCRIPT, env=env)

        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.sink_status(), "",
                         "the staged index outlived the unwind, so the next "
                         "run dies on the clean-sink gate and only a human "
                         "gets it moving again")
        self.assertTrue(os.path.isdir(self.live)
                        and not os.path.islink(self.live))

        self.run_script(CUTOVER_SCRIPT, expect=0)
        self.assertEqual(read(os.path.join(self.fx.sink, "manual", "one.md")),
                         "first\n")

    def test_an_unreadable_live_file_is_refused_before_anything_is_copied(self):
        path = self.put_live("manual/locked.md", "unreadable body\n")
        os.chmod(path, 0o000)
        try:
            out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
            self.assertIn("locked.md", out, out)
        finally:
            os.chmod(path, 0o644)
        self.run_script(CUTOVER_SCRIPT, expect=0)

    def test_a_live_directory_over_a_sink_file_is_refused_before_the_sync(self):
        """`mkdir` blew up mid-sync with a raw "File exists" and left the sink
        dirty. The collision is knowable in pass 1."""
        with open(os.path.join(self.fx.sink, "newdir"), "w",
                  encoding="utf-8") as fh:
            fh.write("file not dir\n")
        self.commit_sink()
        self.put_live("newdir/deep.md", "deep\n")

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
        self.assertIn("newdir", out, out)

    # -- 5. containment: a worktree is not a repo of its own -------------
    def test_a_git_worktree_of_the_live_config_repo_is_refused_as_a_sink(self):
        """A worktree resolves OUTSIDE ~/.claude and `rev-parse
        --show-toplevel` returns the worktree root, so both containment gates
        passed. `git add -A` then swept the whole checkout and committed
        proposal content into the PUBLIC config repo's history."""
        env = self.fx.env()
        wt = os.path.join(self.fx.home, ".local", "state", "wt-sink")
        git("worktree", "add", "-q", "-b", "sinkbranch", wt,
            cwd=self.fx.claude, env=env)
        os.makedirs(os.path.join(wt, "rsi"), exist_ok=True)
        cfg_path = os.path.join(self.fx.claude, "recursive-self-improvement",
                                "config", "config.json")
        doc = json.loads(read(cfg_path))
        doc["proposals"]["folder"] = wt
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        git("add", "-A", cwd=self.fx.claude, env=env)
        git("commit", "-q", "-m", "cfg", cwd=self.fx.claude, env=env)
        head = git("rev-parse", "HEAD", cwd=self.fx.claude, env=env).strip()
        self.put_live("manual/secret-proposal.md", "PROPOSAL BODY\n")

        proc = self.run_script(CUTOVER_SCRIPT)
        out = proc.stdout + proc.stderr

        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn(wt, out, out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.claude,
                             env=env).strip(), head,
                         "proposal content was committed into the live config "
                         "repo's history")
        self.assertNotIn("cutover:", git("log", "--all", "--oneline",
                                         cwd=self.fx.claude, env=env))

    # -- 6. the verify -> mv window --------------------------------------
    def test_a_proposal_written_between_verify_and_the_swap_is_absorbed(self):
        """No lock is taken and the RSI writers are unattended cron agents, so
        a proposal can land after verify has passed and before the `mv`. It
        went to the backup and the run still said "done".

        A lock the writers do not take is theatre, so the fix does not depend
        on their cooperation: after the swap the retired directory is at a path
        no writer knows, and everything in it that the sink lacks is absorbed
        and committed."""
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        env = self.date_shim(RACE_FILE=os.path.join(self.live, "manual",
                                                    "raced.md"))

        self.run_script(CUTOVER_SCRIPT, env=env, expect=0)

        landed = os.path.join(self.fx.sink, "manual", "raced.md")
        self.assertTrue(os.path.isfile(landed),
                        "the raced proposal exists only in the backup the "
                        "script invites the user to delete")
        self.assertEqual(read(landed), "RACED PROPOSAL BODY\n")
        self.assertIn("manual/raced.md", git("ls-files", cwd=self.fx.sink,
                                             env=self.fx.env()))
        self.assertEqual(self.sink_status(), "")

    # -- 7. what a symlink is allowed to name ----------------------------
    def test_a_symlink_to_a_credential_in_home_is_refused(self):
        """The escape check admitted every path under `$HOME`, and `$HOME` is
        exactly where the credentials live. Measured before the fix: exit 0
        with the private key copied by content and committed into the sink."""
        ssh = os.path.join(self.fx.home, ".ssh")
        os.makedirs(ssh)
        key = os.path.join(ssh, "id_ed25519")
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("-----BEGIN OPENSSH PRIVATE KEY-----\nZZZFAKEKEY\n")
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        os.symlink(key, os.path.join(self.live, "manual", "key-note.md"))

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))

        self.assertIn("key-note.md", out, out)
        self.assertIn(key, out,
                      "the refusal must name what the link actually resolves "
                      "to; the link's own name said '.md':\n%s" % out)
        self.assertFalse(os.path.exists(os.path.join(self.fx.sink, "manual",
                                                     "key-note.md")))

    def test_a_symlink_chain_is_judged_by_its_final_target(self):
        """A chain that hops outside the trees and lands back inside them is
        accepted; the same chain landing outside is refused. What is pinned is
        that the WHOLE chain is resolved — judging the first hop is how an
        escape gets in, and how a legitimate proposal gets refused."""
        inside = self.put_live("archived/real-note.md", "chain body\n")
        hop = os.path.join(self.fx.home, "hop")
        os.symlink(inside, hop)
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        os.symlink(hop, os.path.join(self.live, "manual", "chain.md"))

        self.run_script(CUTOVER_SCRIPT, expect=0)
        landed = os.path.join(self.fx.sink, "manual", "chain.md")
        self.assertEqual(read(landed), "chain body\n")
        self.assertFalse(os.path.islink(landed))

    def test_a_symlink_chain_ending_outside_the_trees_is_refused(self):
        outside = os.path.join(self.fx.home, "note-outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("outside body\n")
        hop = os.path.join(self.rsi_live, "hop")
        os.symlink(outside, hop)
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        os.symlink(hop, os.path.join(self.live, "manual", "chain.md"))

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
        self.assertIn(outside, out,
                      "the refusal must name the final target, not the hop "
                      "inside the tree:\n%s" % out)

    def test_a_relative_symlink_inside_the_tree_is_absorbed_as_content(self):
        self.put_live("archived/x.md", "relative body\n")
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        os.symlink(os.path.join("..", "archived", "x.md"),
                   os.path.join(self.live, "manual", "rel.md"))

        self.run_script(CUTOVER_SCRIPT, expect=0)
        landed = os.path.join(self.fx.sink, "manual", "rel.md")
        self.assertFalse(os.path.islink(landed))
        self.assertEqual(read(landed), "relative body\n")

    # -- 8. the backup is the last copy; it gets the same rigour ---------
    def test_the_backup_never_lands_inside_an_existing_stamp_directory(self):
        """`mv` into a colliding stamp NESTS rather than clobbers, so the
        retired tree ended up one level deeper than the script's own message
        claims. Reachable through concurrency; forced here with a fixed
        stamp."""
        stamp = "20260101T000000Z"
        prior = os.path.join(self.fx.backup, stamp, "claude-proposals",
                             "manual")
        os.makedirs(prior)
        with open(os.path.join(prior, "prior.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("PRIOR BACKUP CONTENT\n")
        env = self.date_shim(FIXED_STAMP=stamp)

        self.run_script(CUTOVER_SCRIPT, env=env, expect=0)

        self.assertEqual(read(os.path.join(prior, "prior.md")),
                         "PRIOR BACKUP CONTENT\n")
        self.assertFalse(
            os.path.exists(os.path.join(self.fx.backup, stamp,
                                        "claude-proposals", "proposals")),
            "the retired tree was nested inside the earlier backup")
        siblings = [d for d in os.listdir(self.fx.backup) if d != stamp]
        self.assertTrue(siblings,
                        "the backup must go somewhere of its own: %r"
                        % os.listdir(self.fx.backup))

    def test_a_symlinked_backup_root_is_refused(self):
        """The backup is the last copy of the content being retired. Writing
        it silently through a symlink to somewhere the user did not name is
        the same class of bug as everything else here."""
        elsewhere = os.path.join(self.fx.home, "somewhere-else")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.fx.backup)

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
        self.assertIn(self.fx.backup, out, out)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_an_unwritable_backup_root_is_refused_before_the_commit(self):
        """It used to abort AFTER the commit, with a raw `mkdir` error."""
        os.makedirs(self.fx.backup)
        os.chmod(self.fx.backup, 0o500)
        head = self.sink_head()
        try:
            out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
            self.assertIn(self.fx.backup, out, out)
            self.assertEqual(self.sink_head(), head,
                             "the sink was committed before the backup root "
                             "was ever checked")
        finally:
            os.chmod(self.fx.backup, 0o700)

    def test_a_fifo_in_the_proposal_tree_is_refused_not_silently_dropped(self):
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        os.mkfifo(os.path.join(self.live, "manual", "apipe"))

        out = self.assert_nothing_happened(self.run_script(CUTOVER_SCRIPT))
        self.assertIn("apipe", out, out)

    def test_awkward_filenames_still_survive_the_cutover(self):
        """The accept list, pinned: a fail-closed enumeration that also
        refused a filename with a newline in it would be useless."""
        names = ["a file with spaces.md", "-leading-dash.md", "new\nline.md",
                 "ångström-π.md", "it's \"quoted\".md", "star*glob?.md"]
        os.makedirs(os.path.join(self.live, "manual"), exist_ok=True)
        for name in names:
            self.put_live(os.path.join("manual", name), "body of %r\n" % name)

        self.run_script(CUTOVER_SCRIPT, expect=0)

        tracked = git("ls-files", "-z", cwd=self.fx.sink,
                      env=self.fx.env()).split("\0")
        for name in names:
            landed = os.path.join(self.fx.sink, "manual", name)
            self.assertEqual(read(landed), "body of %r\n" % name)
            self.assertIn("manual/" + name, tracked)

    def test_two_concurrent_cutovers_do_not_lose_content(self):
        self.put_live("manual/only-live.md", "live only\n")
        self.put_live("keepme.md", "keep me\n", root=self.rsi_live)
        env = self.fx.env()
        procs = [subprocess.Popen(["bash", CUTOVER_SCRIPT], env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
                 for _ in range(2)]
        outs = [p.communicate()[0] for p in procs]

        for proc, out in zip(procs, outs):
            self.assertEqual(proc.returncode, 0,
                             "a serialised re-run must be a clean no-op:\n%s"
                             % out)
        self.assertEqual(os.readlink(self.live), self.fx.sink)
        self.assertEqual(os.readlink(self.rsi_live),
                         os.path.join(self.fx.sink, "rsi"))
        self.assertEqual(read(os.path.join(self.fx.sink, "manual",
                                           "only-live.md")), "live only\n")
        self.assertEqual(read(os.path.join(self.fx.sink, "rsi", "keepme.md")),
                         "keep me\n")
        self.assertEqual(self.sink_status(), "")


# --------------------------------------------------------------------------
# 9. Backfilling the dedup ledger for proposals that predate it.
#
# The sink refuses any proposal whose content id is already in
# `.index.jsonl`, at ANY status. That guarantee is only as good as the
# ledger: the 34 rsi proposals (plus the archived/manual/research corpus)
# predate the ledger entirely, so until they are recorded a rejected
# proposal that gets drained and deleted regenerates the next night under a
# fresh YYYY-MM-DD- prefix, filename dedup misses it because the name
# differs, and id dedup misses it because the id was never written down.
#
# The backfill is therefore only worth anything if its id algorithm is
# BYTE-IDENTICAL to the shipped sink's. These tests pin that two ways: a
# textual comparison of the shared functions against the real
# `rsi-proposal-sink` script, and golden vectors captured by running that
# binary.
# --------------------------------------------------------------------------
BACKFILL_PATH = os.path.join(REPO_ROOT, "scripts", "backfill-proposal-index.py")
backfill = load_module("backfill_proposal_index", BACKFILL_PATH)


# (filename, file text, id) — every id below was produced by running the
# shipped `rsi-proposal-sink` binary, not by this repo's code. They cover
# the normalization corners that can silently move an id: frontmatter
# dropped whole, the YYYY-MM-DD- prefix stripped from the title, markdown
# punctuation and whitespace runs collapsed, non-ASCII prose, a body that
# is empty once the frontmatter goes.
GOLDEN_IDS = [
    ("2026-08-30-plain-pending.md",
     "---\nstatus: pending\ndate: 2026-08-30\n---\n\n## Problem\n"
     "The nightly reviewer regenerates rejected proposals.\n",
     "3e90c126b9f96dbb"),
    ("markdown-noise.md",
     "---\nstatus: implemented\n---\n\n## Problem\n\n"
     "  **Bold**, _em_ --- and\ta tab;   runs   of   spaces!\n\n- list item\n",
     "c637ca22ec3da975"),
    ("no-frontmatter.md",
     "# A proposal with no frontmatter at all\n\nBody text here.\n",
     "3be715d46b280ed3"),
    ("2026-07-31-unicode-prose.md",
     "---\nstatus: pending\n---\n\n## Problem\n"
     "Att göra rätt — café naïve åäö.\n",
     "9012c9a8bd243bd8"),
    ("2026-08-02-empty-body.md",
     "---\nstatus: pending\n---\n",
     "698444024e4ce260"),
]


def shipped_sink():
    """Path to the real `rsi-proposal-sink`, or None.

    It is a nix `writePython3Bin`, so on a deployed box it is on PATH and
    otherwise it is a built store path. Located, never reimplemented — the
    whole point of these tests is to compare against the thing that ships.
    """
    return backfill.find_shipped_sink()


class BackfillCase(unittest.TestCase):
    """A throwaway sink root with category subdirs and an empty ledger."""

    CATEGORIES = ("rsi", "archived", "manual", "research", "permissions",
                  "clv2", "from-research", "router")

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="proposals-backfill-test-")
        self.sink = os.path.join(self._tmp, "claude-proposals")
        for name in self.CATEGORIES:
            os.makedirs(os.path.join(self.sink, name))
        self.ledger = os.path.join(self.sink, ".index.jsonl")
        with open(self.ledger, "w", encoding="utf-8"):
            pass

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def put(self, category, name, text):
        path = os.path.join(self.sink, category, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_backfill(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, BACKFILL_PATH, "--sink", self.sink, *args],
            capture_output=True, text=True)
        if proc.returncode != expect:
            self.fail("backfill exited %d (want %d)\nSTDOUT:\n%s\nSTDERR:\n%s"
                      % (proc.returncode, expect, proc.stdout, proc.stderr))
        return proc

    def rows(self):
        out = []
        for ln in read(self.ledger).splitlines():
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out


class TestBackfillIdAgreement(unittest.TestCase):
    def test_golden_ids_match_the_shipped_sink(self):
        for fname, text, want in GOLDEN_IDS:
            self.assertEqual(backfill.content_id(fname, text), want,
                             "content id for %s drifted from the shipped "
                             "sink's; a backfill whose hashes differ by one "
                             "whitespace rule protects nothing" % fname)

    def test_algorithm_is_textually_identical_to_the_shipped_sink(self):
        path = shipped_sink()
        if not path:
            self.skipTest("rsi-proposal-sink is not built or on PATH here")
        diffs = backfill.algorithm_drift(read(path))
        self.assertEqual(diffs, [], "the id algorithm has drifted from "
                                    "%s: %s" % (path, diffs))

    def test_shipped_sink_agrees_on_freshly_generated_ids(self):
        """The end-to-end proof: feed proposal bodies through the real
        binary and compare the ids IT writes with the ones the backfill
        computes."""
        path = shipped_sink()
        if not path:
            self.skipTest("rsi-proposal-sink is not built or on PATH here")
        root = tempfile.mkdtemp(prefix="shipped-sink-")
        try:
            blocks = "".join("===PROPOSAL: %s===\n%s===END PROPOSAL===\n"
                             % (n, t) for n, t, _ in GOLDEN_IDS)
            proc = subprocess.run([path, os.path.join(root, "rsi")],
                                  input=blocks, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            written = {}
            with open(os.path.join(root, ".index.jsonl"),
                      encoding="utf-8") as fh:
                for ln in fh:
                    row = json.loads(ln)
                    written[row["file"]] = row["id"]
            self.assertTrue(written, "the shipped sink wrote no ledger rows")
            for fname, text, _ in GOLDEN_IDS:
                self.assertEqual(written.get(fname),
                                 backfill.content_id(fname, text),
                                 "id disagreement on %s" % fname)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestBackfillLedger(BackfillCase):
    PENDING = ("---\nstatus: pending\ndate: 2026-08-02\n"
               "source_sessions:\n  - abc-123\n  - def-456\n---\n\n"
               "## Problem\nSessions drift.\n")
    IMPLEMENTED = ("---\nstatus: implemented\ndate: 2026-04-12\n---\n\n"
                   "## Problem\nMeta work ratio.\n")
    REJECTED = ("---\nstatus: rejected\ndate: 2026-08-01\n"
                "rejection_reason: \"not a real problem\"\n---\n\n"
                "## Problem\nThe scanner reinstall loop is manual.\n")

    def seed(self):
        self.put("rsi", "2026-08-02-drift.md", self.PENDING)
        self.put("rsi", "2026-04-12-meta.md", self.IMPLEMENTED)
        self.put("rsi", "2026-08-01-scanner.md", self.REJECTED)

    def test_records_every_status_including_rejected(self):
        self.seed()
        self.run_backfill()
        by_file = {r["file"]: r for r in self.rows()}
        self.assertEqual(
            {f: by_file[f]["status"] for f in by_file},
            {"2026-08-02-drift.md": "pending",
             "2026-04-12-meta.md": "implemented",
             "2026-08-01-scanner.md": "rejected"})

    def test_records_the_same_fields_the_live_sink_records(self):
        self.seed()
        self.run_backfill()
        row = {r["file"]: r for r in self.rows()}["2026-08-02-drift.md"]
        for key in ("id", "file", "date", "status", "session", "evidence"):
            self.assertIn(key, row, "the ledger row must carry the same "
                                    "fields the live sink writes")
        self.assertEqual(row["date"], "2026-08-02")
        self.assertEqual(row["session"], "abc-123")
        self.assertEqual(row["evidence"], "Sessions drift.")
        self.assertEqual(row["id"],
                         backfill.content_id("2026-08-02-drift.md",
                                             self.PENDING))

    def test_unrecoverable_fields_are_explicitly_null_not_invented(self):
        self.put("archived", "em-dash-gate.md", "# Proposal: em-dash gate\n")
        self.run_backfill()
        row = self.rows()[0]
        self.assertIsNone(row["status"], "a proposal with no frontmatter has "
                                         "no status; recording a made-up one "
                                         "is worse than recording none")
        self.assertIsNone(row["session"])
        self.assertIsNone(row["date"], "no frontmatter date and no date "
                                       "prefix in the filename means the date "
                                       "is unrecoverable — today's date would "
                                       "be a fabrication")

    def test_date_falls_back_to_the_filename_prefix(self):
        self.put("rsi", "2026-04-27-chat-history-backup.md", "# backup\n")
        self.run_backfill()
        self.assertEqual(self.rows()[0]["date"], "2026-04-27")

    def test_is_idempotent(self):
        self.seed()
        first = self.run_backfill()
        self.assertIn("added 3", first.stdout)
        before = read(self.ledger)
        second = self.run_backfill()
        self.assertIn("added 0", second.stdout)
        self.assertIn("already present 3", second.stdout)
        self.assertEqual(read(self.ledger), before,
                         "a second run must not append a single byte")

    def test_reconciles_against_existing_ledger_rows_by_id(self):
        self.seed()
        cid = backfill.content_id("2026-08-01-scanner.md", self.REJECTED)
        # A pre-existing row written under a DIFFERENT filename: matching on
        # the id is the whole point, filenames move between nights.
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": cid, "file": "2026-01-01-other.md",
                                 "status": "rejected"}) + "\n")
        proc = self.run_backfill()
        ids = [r["id"] for r in self.rows()]
        self.assertEqual(ids.count(cid), 1, "the id was already in the "
                                            "ledger; a second row for it is a "
                                            "duplicate")
        self.assertIn("already present 1", proc.stdout)

    def test_skips_the_permissions_category_with_a_stated_reason(self):
        self.put("permissions", "2026-08-24-askuserquestion.md",
                 "---\nstatus: pending\n---\n\nbody\n")
        proc = self.run_backfill()
        self.assertEqual(self.rows(), [],
                         "permission proposals have their own writer and "
                         "their own lifecycle — a recurring permission "
                         "pattern is a fresh signal, not a duplicate")
        self.assertIn("permissions", proc.stdout)
        self.assertRegex(proc.stdout, r"skipped .*permissions")

    def test_walks_every_indexed_category(self):
        self.put("rsi", "2026-08-02-a.md", "---\nstatus: pending\n---\n\nA\n")
        self.put("archived", "2026-07-07-b.md", "# B\n")
        self.put("manual", "2026-08-09-c.md", "---\nstatus: pending\n---\n\nC\n")
        self.put("research", "2026-07-30-d.md", "---\nstatus: open\n---\n\nD\n")
        self.run_backfill()
        self.assertEqual(len(self.rows()), 4)

    def test_prints_a_summary_of_scanned_added_present_and_skipped(self):
        self.seed()
        out = self.run_backfill().stdout
        for token in ("scanned", "added", "already present", "skipped"):
            self.assertIn(token, out, "summary must report %r" % token)

    def test_dry_run_touches_nothing(self):
        self.seed()
        before = read(self.ledger)
        proc = self.run_backfill("--dry-run")
        self.assertEqual(read(self.ledger), before)
        self.assertIn("added 3", proc.stdout)

    def test_never_modifies_a_proposal_file(self):
        self.seed()
        def snapshot():
            out = {}
            for base, _dirs, files in os.walk(self.sink):
                for f in files:
                    if f.endswith(".md"):
                        p = os.path.join(base, f)
                        out[p] = read(p)
            return out
        before = snapshot()
        self.run_backfill()
        self.assertEqual(snapshot(), before,
                         "the backfill adds a ledger; it never edits history")

    def test_a_concurrent_appenders_rows_survive(self):
        """The one failure the ledger exists to prevent.

        `rsi-proposal-sink` opens `.index.jsonl` with O_APPEND ONCE, at the
        top of its run, and holds that fd while it writes proposals. A
        backfill that republishes the ledger by renaming a fresh file over it
        gives that fd an orphaned inode: every row the sink writes afterwards
        goes to a file with no name, `os.write` returns the byte count, and
        nothing anywhere errors. The proposals those rows were meant to
        protect then regenerate the next night.

        The pre-publish re-read cannot catch this — it only sees a writer that
        has ALREADY appended, never one that is about to.
        """
        self.seed()
        held = os.open(self.ledger,
                       os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            self.run_backfill()
            row = json.dumps({"id": "0123456789abcdef",
                              "file": "2026-09-01-nightly.md",
                              "status": "pending"}) + "\n"
            os.write(held, row.encode("utf-8"))
        finally:
            os.close(held)
        ids = [r["id"] for r in self.rows()]
        self.assertIn(
            "0123456789abcdef", ids,
            "the nightly sink's row went to an orphaned inode and vanished; "
            "the proposal it recorded will regenerate. Ledger now holds: %r"
            % (ids,))
        self.assertEqual(len(ids), 4, "and the backfill's own rows must "
                                      "still be there: %r" % (ids,))

    def test_the_ledger_is_appended_to_never_republished(self):
        """The structural property behind the test above, asserted directly so
        a future 'atomic rewrite' cannot come back without turning this red:
        the ledger keeps its inode, its existing bytes are never rewritten,
        and no tmp file is left beside it."""
        self.seed()
        self.run_backfill()
        before = read(self.ledger)
        before_inode = os.stat(self.ledger).st_ino
        self.put("rsi", "2026-08-03-extra.md",
                 "---\nstatus: pending\n---\n\n## Problem\nExtra.\n")
        self.run_backfill()

        self.assertEqual(
            os.stat(self.ledger).st_ino, before_inode,
            "the ledger's inode was replaced; any fd another writer holds now "
            "points at an orphan")
        self.assertTrue(
            read(self.ledger).startswith(before),
            "existing ledger rows must be left byte-for-byte where they were")
        self.assertEqual(len(self.rows()), 4)
        leftovers = [f for f in os.listdir(self.sink)
                     if f.startswith(".index.jsonl.tmp")]
        self.assertEqual(leftovers, [], "no tmp file may be left behind")

    def test_a_ledger_with_no_trailing_newline_is_not_glued_to(self):
        """A torn last line must not have the next row welded onto it."""
        self.seed()
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write('{"id": "aaaaaaaaaaaaaaaa", "status": "rejected"}')
        self.run_backfill()
        lines = [ln for ln in read(self.ledger).splitlines() if ln.strip()]
        self.assertEqual(len(lines), 4, "rows: %r" % (lines,))
        self.assertEqual(len(self.rows()), 4,
                         "a row was glued onto the unterminated line")

    def test_tolerates_an_unparseable_ledger_line(self):
        self.seed()
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        proc = self.run_backfill()
        self.assertIn("{not json", read(self.ledger),
                      "a line the backfill cannot parse is preserved, never "
                      "silently dropped")
        self.assertEqual(len([r for r in self.rows()]), 3)
        self.assertIn("unparseable", proc.stdout)


class TestRegeneratedRejectedProposalIsSkipped(BackfillCase):
    """A10, end to end and against the binary that actually ships.

    A rejected proposal is drained and its file deleted. The reviewer
    re-derives the same observation the next night under a fresh
    YYYY-MM-DD- prefix. Filename dedup cannot see it (the name differs) and
    the file is gone, so the backfilled ledger row is the ONLY thing left
    that can stop it.
    """

    BODY = ("---\nstatus: rejected\ndate: 2026-08-01\n"
            "rejection_reason: \"investment phase, not drift\"\n---\n\n"
            "## Problem\nThe research agent hits its cap with no fallback.\n")

    def test_deleted_rejected_proposal_still_collides_by_id(self):
        path = shipped_sink()
        if not path:
            self.skipTest("rsi-proposal-sink is not built or on PATH here")
        original = "2026-08-01-research-agent-cap-hit-no-fallback.md"
        self.put("rsi", original, self.BODY)
        self.run_backfill()
        self.assertEqual(len(self.rows()), 1)

        # Drained: the human rejected it and the file was removed.
        os.unlink(os.path.join(self.sink, "rsi", original))

        # Tonight's run re-derives it under a new date prefix, and the
        # frontmatter the model emits is `pending`, not `rejected`.
        regenerated = "2026-09-01-research-agent-cap-hit-no-fallback.md"
        body = self.BODY.replace("status: rejected", "status: pending") \
                        .replace("date: 2026-08-01", "date: 2026-09-01")
        proc = subprocess.run(
            [path, os.path.join(self.sink, "rsi")],
            input="===PROPOSAL: %s===\n%s===END PROPOSAL===\n"
                  % (regenerated, body),
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("skipped duplicate", proc.stdout,
                      "the regenerated proposal must collide with the "
                      "backfilled ledger row:\n%s" % proc.stdout)
        self.assertFalse(
            os.path.exists(os.path.join(self.sink, "rsi", regenerated)),
            "a rejected proposal came back — this is exactly the failure "
            "the backfill exists to prevent")
        self.assertEqual(len(self.rows()), 1,
                         "the skip must not add a ledger row either")

    def test_without_the_backfill_the_proposal_does_come_back(self):
        """The control. Without a ledger row nothing stops it — this is what
        the sink looked like before the backfill, and it is why an empty
        `.index.jsonl` made the guarantee hollow."""
        path = shipped_sink()
        if not path:
            self.skipTest("rsi-proposal-sink is not built or on PATH here")
        regenerated = "2026-09-01-research-agent-cap-hit-no-fallback.md"
        proc = subprocess.run(
            [path, os.path.join(self.sink, "rsi")],
            input="===PROPOSAL: %s===\n%s===END PROPOSAL===\n"
                  % (regenerated, self.BODY),
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(
            os.path.exists(os.path.join(self.sink, "rsi", regenerated)),
            "with an empty ledger the sink has nothing to dedup against")

if __name__ == "__main__":
    unittest.main(verbosity=2)

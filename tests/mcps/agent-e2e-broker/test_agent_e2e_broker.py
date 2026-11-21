#!/usr/bin/env python3
"""Contract tests for the agent-e2e broker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest


# The server and its helpers live under runtime/, the tree projected into
# ~/.claude; this suite does not. HERE therefore names the SERVER's
# directory, not this file's, so every path derived from it still resolves.
HERE = Path(__file__).resolve().parents[3] / "runtime" / "mcps" / "agent-e2e-broker"
SERVER = HERE / "server.py"
HARNESS = HERE.parents[1] / "scripts" / "agent-e2e" / "agent_e2e.py"


def load_server():
    name = f"agent_e2e_broker_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True,
                          text=True).stdout.strip()


# Whether a sandbox can ACTUALLY be created here, probed once.
#
# Twin of `_require_sandbox` in tests/test_agent_e2e.py -- deliberately copied
# rather than shared, because the only import path the two have in common is a
# `conftest` module, and importing across suites that way already broke this
# tree once: two directories here hold a module called `conftest`, so in a
# whole-tree run the wrong one wins and collection dies for everything.
# Fifteen duplicated lines are cheaper than that failure.
#
# Asking `shutil.which("bwrap")` is a different question from asking whether
# bubblewrap works, and the difference is what made these two cases red: the
# binary is on a runner, the harness started, it could not sandbox, and the case
# reported "stub claude never started" as though the broker were broken.
# `--unshare-all` was still the wrong question -- that succeeds on a runner.
# The probe therefore builds the harness's own mount shape, including the
# read-only bind of the real HOME that the ssh shadowing stacks on.
_SANDBOX_OK = None


def require_sandbox(case: unittest.TestCase) -> None:
    """Skip `case` unless bubblewrap can really build a sandbox here."""
    global _SANDBOX_OK
    if _SANDBOX_OK is None:
        bwrap = shutil.which("bwrap")
        home = pwd.getpwuid(os.getuid()).pw_dir
        if bwrap is None:
            _SANDBOX_OK = (False, "bubblewrap not on PATH")
        else:
            try:
                p = subprocess.run([bwrap, "--dev-bind", "/", "/",
                                    "--ro-bind", home, home, "--tmpfs", "/tmp",
                                    "true"], capture_output=True, text=True, timeout=30)
                _SANDBOX_OK = (
                    p.returncode == 0,
                    "bubblewrap is present but cannot build the harness's "
                    "sandbox here (restricted user namespaces, or a mount it "
                    "refuses): "
                    + (p.stderr.strip().splitlines() or ["no stderr"])[-1],
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _SANDBOX_OK = (False, f"bubblewrap probe failed: {exc!r}")
    ok, why = _SANDBOX_OK
    if not ok:
        case.skipTest(why)


class Fixture(unittest.TestCase):
    """A repo with origin/master, a pushed candidate branch, a stub claude."""

    def setUp(self) -> None:
        self.server = load_server()
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.tmp = tempfile.TemporaryDirectory(prefix=".agent-e2e-broker-test-",
                                               dir=self.home / "worktrees")
        root = Path(self.tmp.name)
        os.environ["GIT_CONFIG_GLOBAL"] = str(root / "gitconfig")
        (root / "gitconfig").write_text("[user]\n\tname=t\n\temail=t@t\n"
                                        "[init]\n\tdefaultBranch=master\n")
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.repo = root / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
                       capture_output=True)
        harness = self.repo / "runtime/scripts/agent-e2e/agent_e2e.py"
        harness.parent.mkdir(parents=True)
        shutil.copy(HARNESS, harness)
        scen = self.repo / "tests/agent-e2e/demo"
        scen.mkdir(parents=True)
        (scen / "prompt.md").write_text("say hi\n")
        (scen / "setup.sh").write_text("#!/usr/bin/env bash\n")
        (scen / "check.sh").write_text(
            "#!/usr/bin/env bash\ngrep -q CANDIDATE \"$1/home/.claude/CLAUDE.md\" "
            "&& { echo cand; exit 0; } || { echo base; exit 1; }\n")
        (self.repo / "runtime/CLAUDE.md").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "master")
        git(self.repo, "switch", "-qc", "feat/x")
        (self.repo / "runtime/CLAUDE.md").write_text("CANDIDATE\n")
        git(self.repo, "commit", "-qam", "cand")
        git(self.repo, "push", "-q", "origin", "feat/x")
        git(self.repo, "switch", "-q", "master")
        self.claude = root / "claude"
        self.claude.write_text(
            "#!/usr/bin/env bash\n"
            "echo '{\"type\":\"result\",\"result\":\"hi\",\"is_error\":false,"
            "\"total_cost_usd\":0.01}'\n")
        self.claude.chmod(0o755)
        # Broker state must lie outside every sandbox write root, like the real one.
        self.state = tempfile.TemporaryDirectory(prefix="agent-e2e-broker-test-",
                                                 dir=self.home / ".local/state")
        self.out = Path(self.state.name) / "broker/campaigns"
        self.server.PROTOCOL = "file"
        self.raw = {
            "version": 1, "remoteUrl": f"file://{self.origin}",
            "stateDir": f"{self.state.name}/broker",
            "harness": "runtime/scripts/agent-e2e/agent_e2e.py",
            "scenarioRoot": "tests/agent-e2e", "claudeExecutable": str(self.claude),
            "path": os.environ["PATH"].split(":"),
            "models": ["haiku", "sonnet"], "maxSamples": 2, "maxJobs": 2,
            "maxBudgetUsd": 3.0, "maxTimeoutSec": 900, "maxCallSec": 3600,
            "outputLimitChars": 20000, "readOnlyPaths": [],
        }
        self.server._load_config = lambda: {"agentE2eBroker": dict(self.raw)}
        self.policy = self.server._load_policy({"agentE2eBroker": dict(self.raw)})

    def tearDown(self) -> None:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        self.tmp.cleanup()
        self.state.cleanup()

    def req(self, **over) -> dict:
        return {"scenario": "demo", "old_ref": "origin/master", "new_ref": "origin/feat/x",
                "model": "haiku", **over}


class ValidationTests(Fixture):
    def test_rejects_unknown_fields_and_bad_values(self) -> None:
        bad = [
            {"command": "x"},
            {"scenario": "../etc"},
            {"scenario": "Demo"},
            {"old_ref": "master"},
            {"old_ref": "origin/../x"},
            {"new_ref": "--upload-pack=x"},
            {"new_ref": "HEAD"},
            {"model": "opus"},
            {"n": 3},
            {"n": True},
            {"jobs": 0},
            {"budget_usd": 3.5},
            {"timeout_sec": 901},
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(RuntimeError):
                self.server._validate_request(self.req(**over), self.policy)

    def test_bounds_whole_call_wall_time(self) -> None:
        self.server._validate_request(self.req(n=2, jobs=1, timeout_sec=900), self.policy)
        self.raw["maxCallSec"] = 1800
        policy = self.server._load_policy({"agentE2eBroker": self.raw})
        with self.assertRaisesRegex(RuntimeError, "worst-case"):
            self.server._validate_request(self.req(n=2, jobs=1, timeout_sec=900), policy)

    def test_shipped_config_names_paths_of_this_repo(self) -> None:
        self.server.PROTOCOL = "https"
        policy = self.server._load_policy(json.loads((HERE / "config.json").read_text()))
        root = HERE.parents[2]
        self.assertTrue((root / policy["harness"]).is_file(), policy["harness"])
        self.assertTrue((root / policy["scenarioRoot"]).is_dir(), policy["scenarioRoot"])

    def test_shipped_broker_state_is_outside_every_sandbox_write_root(self) -> None:
        self.server.PROTOCOL = "https"
        policy = self.server._load_policy(json.loads((HERE / "config.json").read_text()))
        settings = json.loads((HERE.parents[2] / "runtime/settings.json").read_text())
        perms = settings["permissions"]
        writable = [*settings["sandbox"]["filesystem"]["allowWrite"],
                    *perms.get("additionalDirectories", []), "/tmp",
                    *(rule[rule.index("(") + 1:-1] for rule in perms["allow"]
                      if rule.startswith(("Edit(", "Write(")))]
        roots = {Path(os.path.realpath(os.path.expanduser(
            e.removeprefix("/") if e.startswith("//") else e).removesuffix("/**")))
                 for e in writable}
        state = {k: v for k, v in policy.items() if isinstance(v, Path) and k != "claudeExecutable"}
        self.assertIn("stateDir", state)
        for key, path in state.items():
            for root in roots:
                with self.subTest(key=key, root=root):
                    self.assertFalse(path == root or root in path.parents)
        self.assertTrue(policy["remoteUrl"].startswith("https://"))

    def test_policy_rejects_unknown_keys(self) -> None:
        with self.assertRaises(RuntimeError):
            self.server._load_policy({"agentE2eBroker": {**self.raw, "extra": 1}})


class RefPolicyTests(Fixture):
    def test_unpushed_commit_refused(self) -> None:
        git(self.repo, "switch", "-qc", "local")
        (self.repo / "runtime/CLAUDE.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "master")
        self.server._update_mirror(self.policy)
        with self.assertRaisesRegex(RuntimeError, "not a commit on origin"):
            self.server._resolve(self.policy, sha)

    def test_missing_branch_refused(self) -> None:
        self.server._update_mirror(self.policy)
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            self.server._resolve(self.policy, "origin/nope")

    def test_harness_comes_from_origin_master_not_the_working_tree(self) -> None:
        require_sandbox(self)
        git(self.repo, "switch", "-q", "feat/x")
        (self.repo / "runtime/scripts/agent-e2e/agent_e2e.py").write_text("print('planted')\n")
        text = self.server._tool_run(self.req())
        self.assertNotIn("planted", text)
        self.assertRegex(text, r"\| new \| .* \| 1 \| 0 \| 0 \|")
        self.assertIn(f"harness = origin/master = {git(self.repo, 'rev-parse', 'origin/master')}",
                      text)


class RunTests(Fixture):
    def test_runs_old_vs_new_from_pushed_refs(self) -> None:
        require_sandbox(self)
        text = self.server._tool_run(self.req())
        self.assertIn("harness exit 0", text)
        self.assertRegex(text, r"\| old \| .* \| 0 \| 1 \| 0 \|")
        self.assertRegex(text, r"\| new \| .* \| 1 \| 0 \| 0 \|")
        self.assertIn("final.txt: 'hi'", text)
        self.assertIn(git(self.repo, "rev-parse", "origin/feat/x"), text)

    def test_scenario_must_exist_on_new_ref(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "has no"):
            self.server._tool_run(self.req(scenario="absent"))

    def test_one_campaign_at_a_time(self) -> None:
        import fcntl
        self.policy["lock"].parent.mkdir(mode=0o700)
        with open(self.policy["lock"], "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "one at a time"):
                self.server._tool_run(self.req())

    def test_lock_is_never_opened_through_a_symlink(self) -> None:
        owner_file = Path(self.tmp.name) / "owner-file"
        owner_file.write_text("OWNER-DATA\n")
        self.policy["lock"].parent.mkdir(mode=0o700)
        self.policy["lock"].symlink_to(owner_file)
        with self.assertRaises(OSError):
            self.server._tool_run(self.req())
        self.assertEqual(owner_file.read_text(), "OWNER-DATA\n")

    def test_tools_list_over_stdio(self) -> None:
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = subprocess.run([sys.executable, str(SERVER)], input="\n".join(map(json.dumps, msgs)),
                             capture_output=True, text=True, timeout=30).stdout.splitlines()
        tools = json.loads(out[1])["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["run"])


class RunDirReadTests(Fixture):
    """Scenario code writes the run dir; the broker reads it back outside the unit."""

    def push_script(self, name: str, body: str) -> str:
        git(self.repo, "switch", "-qc", "feat/hostile")
        script = self.repo / "tests/agent-e2e/demo" / name
        script.write_text("#!/usr/bin/env bash\n" + body + script.read_text())
        git(self.repo, "commit", "-qam", "hostile")
        git(self.repo, "push", "-q", "origin", "feat/hostile")
        git(self.repo, "switch", "-q", "master")
        return "origin/feat/hostile"

    def isolated_run(self, req: dict, timeout: int = 180) -> str:
        import multiprocessing
        import resource
        ctx = multiprocessing.get_context("fork")
        recv, send = ctx.Pipe(duplex=False)

        def child() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
            try:
                send.send(self.server._tool_run(req))
            except BaseException as e:
                send.send(f"CHILD-RAISED {type(e).__name__}: {e}")

        proc = ctx.Process(target=child)
        proc.start()
        ready = recv.poll(timeout)
        text = recv.recv() if ready else None
        proc.kill()
        proc.join()
        self.assertIsNotNone(text, f"broker still reading after {timeout}s")
        self.assertNotIn("CHILD-RAISED", text)
        return text

    def test_symlinks_out_of_the_run_dir_are_not_followed(self) -> None:
        require_sandbox(self)
        canary = Path(self.tmp.name) / "canary"
        canary.write_text("CANARY-OUTSIDE-RUN-DIR\n")
        ref = self.push_script("check.sh", f'ln -sf {canary} "$1/final.txt"\n')
        text = self.server._tool_run(self.req(new_ref=ref))
        self.assertIn("harness exit 0", text)
        self.assertNotIn("CANARY-OUTSIDE-RUN-DIR", text)

    def test_endless_and_huge_run_files_are_read_bounded(self) -> None:
        require_sandbox(self)
        ref = self.push_script("check.sh", 'truncate -s 3G "$1/final.txt"\n')
        text = self.isolated_run(self.req(new_ref=ref))
        self.assertLess(len(text), self.raw["outputLimitChars"] + 1000)
        self.assertIn("agent-e2e campaign `demo`", text)

    def test_fifo_in_the_run_dir_does_not_block(self) -> None:
        require_sandbox(self)
        ref = self.push_script("check.sh", 'rm -f "$1/final.txt"; mkfifo "$1/final.txt"\n')
        text = self.isolated_run(self.req(new_ref=ref))
        self.assertIn("harness exit 0", text)

    def test_scenario_code_cannot_reach_what_the_manager_writes_or_the_harness(self) -> None:
        require_sandbox(self)
        ref = self.push_script("setup.sh", 'c="$2/../../../../.."; '
                               'for f in harness.out harness.err harness/x scenarios/x; do '
                               'echo PLANTED-$f >> "$c/$f" && echo "WROTE $f"; done\n')
        text = self.server._tool_run(self.req(new_ref=ref))
        [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
        self.assertNotIn("WROTE", log.read_text(), text)
        self.assertNotIn("PLANTED", text)


class UnitBindTests(Fixture):
    def test_scenario_code_reads_only_the_repos_a_campaign_needs(self) -> None:
        require_sandbox(self)
        import uuid
        tag = uuid.uuid4().hex[:8]
        other = self.home / "Repos" / f".agent-e2e-broker-other-{tag}"
        needed = self.home / "Repos" / f".agent-e2e-broker-needed-{tag}"
        try:
            for d, word in ((other, "OTHER-REPO-CANARY"), (needed, "NEEDED-REPO-READ")):
                d.mkdir()
                (d / "f").write_text(word + "\n")
            self.raw["readOnlyPaths"] = [str(needed)]
            git(self.repo, "switch", "-qc", "feat/reads-repos")
            (self.repo / "tests/agent-e2e/demo/setup.sh").write_text(
                f"#!/usr/bin/env bash\ncat {other}/f; cat {needed}/f; exit 0\n")
            git(self.repo, "commit", "-qam", "reads repos")
            git(self.repo, "push", "-q", "origin", "feat/reads-repos")
            git(self.repo, "switch", "-q", "master")
            text = self.server._tool_run(self.req(new_ref="origin/feat/reads-repos"))
            [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
            setup = log.read_text()
            self.assertIn("NEEDED-REPO-READ", setup, text)
            self.assertNotIn("OTHER-REPO-CANARY", setup, text)
        finally:
            shutil.rmtree(other, ignore_errors=True)
            shutil.rmtree(needed, ignore_errors=True)

    def test_read_only_path_swapped_for_a_symlink_after_the_check_is_not_bound(self) -> None:
        require_sandbox(self)
        import uuid
        needed = self.home / "Repos" / f".agent-e2e-broker-needed-{uuid.uuid4().hex[:8]}"
        secret = Path(tempfile.mkdtemp(prefix="agent-e2e-broker-test-secret-",
                                       dir=self.home / ".local/state"))
        try:
            needed.mkdir()
            (needed / "f").write_text("NEEDED-REPO-READ\n")
            (secret / "f").write_text("SECRET-CANARY\n")
            self.raw["readOnlyPaths"] = [str(needed)]
            git(self.repo, "switch", "-qc", "feat/reads-swapped")
            (self.repo / "tests/agent-e2e/demo/setup.sh").write_text(
                f"#!/usr/bin/env bash\ncat {needed}/f; exit 0\n")
            git(self.repo, "commit", "-qam", "reads swapped")
            git(self.repo, "push", "-q", "origin", "feat/reads-swapped")
            git(self.repo, "switch", "-q", "master")
            launch = self.server._run_argv

            def swap_then_launch(*a, **kw):
                needed.rename(needed.with_name(needed.name + "-moved"))
                needed.symlink_to(secret)
                return launch(*a, **kw)

            self.server._run_argv = swap_then_launch
            text = self.server._tool_run(self.req(new_ref="origin/feat/reads-swapped"))
            logs = [p.read_text() for p in self.out.glob("*/out/runs/demo/new/1/setup.log")]
            self.assertNotIn("SECRET-CANARY", text + "".join(logs))
            self.assertIn("not the directory that was checked", text)
        finally:
            needed.unlink(missing_ok=True)
            shutil.rmtree(needed.with_name(needed.name + "-moved"), ignore_errors=True)
            shutil.rmtree(secret, ignore_errors=True)

    def test_scenario_code_reads_the_bash_guard_config_the_harness_copies(self) -> None:
        require_sandbox(self)
        config = self.home / ".config/dcg/config.toml"
        if not config.is_file():
            self.skipTest("no dcg config on this host")
        git(self.repo, "switch", "-qc", "feat/reads-dcg")
        (self.repo / "tests/agent-e2e/demo/setup.sh").write_text(
            f"#!/usr/bin/env bash\nhead -c1 {config} >/dev/null && echo DCG-CONFIG-READ\nexit 0\n")
        git(self.repo, "commit", "-qam", "reads dcg config")
        git(self.repo, "push", "-q", "origin", "feat/reads-dcg")
        git(self.repo, "switch", "-q", "master")
        text = self.server._tool_run(self.req(new_ref="origin/feat/reads-dcg"))
        [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
        self.assertIn("DCG-CONFIG-READ", log.read_text(), text)

    def test_agent_bash_sandbox_can_start_inside_the_unit(self) -> None:
        require_sandbox(self)
        bwrap = shutil.which("bwrap")
        git(self.repo, "switch", "-qc", "feat/nested-sandbox")
        (self.repo / "tests/agent-e2e/demo/setup.sh").write_text(
            f"#!/usr/bin/env bash\n{bwrap} --unshare-net --unshare-user --unshare-pid "
            "--dev-bind / / --proc /proc true && echo SANDBOX-STARTED\nexit 0\n")
        git(self.repo, "commit", "-qam", "nested sandbox")
        git(self.repo, "push", "-q", "origin", "feat/nested-sandbox")
        git(self.repo, "switch", "-q", "master")
        text = self.server._tool_run(self.req(new_ref="origin/feat/nested-sandbox"))
        [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
        self.assertIn("SANDBOX-STARTED", log.read_text(), text)

    def test_policy_paths_and_remote_are_checked(self) -> None:
        for key, bad in (("stateDir", str(self.home / "worktrees/mirror")),
                         ("stateDir", str(self.home / "Repos")), ("stateDir", "state"),
                         ("remoteUrl", "ext::sh -c touch% /tmp/x"),
                         ("remoteUrl", f"file://{self.origin} --upload-pack=x"),
                         ("readOnlyPaths", [str(self.home / ".ssh")])):
            with self.subTest(key=key, bad=bad), self.assertRaises(RuntimeError):
                self.server._load_policy({"agentE2eBroker": {**self.raw, key: bad}})


class UntrustedCheckoutTests(Fixture):
    """The config checkout is writable by sandboxed sessions; its git config must never run."""

    def test_config_planted_in_the_checkout_never_runs(self) -> None:
        root = Path(self.tmp.name)
        canary = root / "planted-ran"
        hooks = root / "planted-hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {canary}\n")
        hook.chmod(0o755)
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/feat/x")
        git(self.repo, "config", "core.hooksPath", str(hooks))
        git(self.repo, "config", "core.fsmonitor", f"touch {canary}; false")
        with self.assertRaises(RuntimeError):
            self.server._tool_run(self.req(scenario="absent"))
        self.assertFalse(canary.exists(), "the broker ran git config planted in the checkout")


class ReviewFindingTests(Fixture):
    def test_planted_remote_tracking_ref_refused(self) -> None:
        git(self.repo, "switch", "-qc", "unpushed")
        (self.repo / "runtime/CLAUDE.md").write_text("CANDIDATE planted\n")
        git(self.repo, "commit", "-qam", "planted")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "update-ref", "refs/remotes/origin/zz", sha)
        with self.assertRaises(RuntimeError) as ctx:
            self.server._tool_run(self.req(new_ref="origin/zz"))
        self.assertIn("does not exist", str(ctx.exception))

    def test_unreachable_origin_fails_closed(self) -> None:
        self.raw["remoteUrl"] = f"file://{self.repo.parent}/gone.git"
        with self.assertRaisesRegex(RuntimeError, "fetch"):
            self.server._tool_run(self.req())

    def test_unpushed_harness_commit_on_master_is_not_run(self) -> None:
        require_sandbox(self)
        (self.repo / "runtime/scripts/agent-e2e/agent_e2e.py").write_text("print('local')\n")
        git(self.repo, "commit", "-qam", "local harness")
        text = self.server._tool_run(self.req())
        self.assertRegex(text, r"\| new \| .* \| 1 \| 0 \| 0 \|")
        self.assertIn("harness exit 0", text)

    def test_harness_checkout_removed_after_run(self) -> None:
        require_sandbox(self)
        self.server._tool_run(self.req())
        self.assertEqual(list(self.out.glob("*/harness")), [])
        self.assertTrue(list(self.out.glob("*/out/runs/demo/new/1/verdict.json")))

    def test_redacts_private_and_age_keys(self) -> None:
        key = "AGE-SECRET-KEY-1" + "Q" * 58
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        out = self.server._redact(f"{key}\n{pem}\nAKIA" + "A" * 16)
        self.assertNotIn(key, out)
        self.assertNotIn("abc", out)
        self.assertNotIn("AKIA" + "A" * 16, out)

    def test_pushed_scenario_code_reaches_no_token_and_writes_only_its_run(self) -> None:
        require_sandbox(self)
        import uuid
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        home_canary = home / ".local/state" / f"agent-e2e-broker-test-{uuid.uuid4().hex[:8]}"
        outside = Path(self.tmp.name) / "outside-run"
        outside.mkdir()
        git(self.repo, "switch", "-qc", "feat/pushed-by-sandboxed-agent")
        scen = self.repo / "tests/agent-e2e/demo"
        (scen / "setup.sh").write_text(
            "#!/usr/bin/env bash\n"
            f"test -r {home}/.cache/gh-tokens/host/token && echo TOKEN-READABLE\n"
            f"test -e {home}/.cache/gh-tokens && echo TOKEN-DIR-VISIBLE\n"
            "ls /run/agenix >/dev/null 2>&1 && echo AGENIX-VISIBLE\n"
            "ls /run/github-app >/dev/null 2>&1 && echo GITHUB-APP-VISIBLE\n"
            f"echo planted > {home_canary} && echo WROTE-HOME\n"
            f"echo planted > {outside}/x && echo WROTE-OUTSIDE\n"
            'echo ok > "$2/inside" && echo WROTE-RUN\n'
            "exit 0\n")
        git(self.repo, "commit", "-qam", "scenario code")
        git(self.repo, "push", "-q", "origin", "feat/pushed-by-sandboxed-agent")
        git(self.repo, "switch", "-q", "master")
        try:
            text = self.server._tool_run(self.req(new_ref="origin/feat/pushed-by-sandboxed-agent"))
            [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
            setup = log.read_text()
            self.assertIn("WROTE-RUN", setup, text)
            for leak in ("TOKEN-READABLE", "TOKEN-DIR-VISIBLE", "AGENIX-VISIBLE",
                         "GITHUB-APP-VISIBLE", "WROTE-HOME", "WROTE-OUTSIDE"):
                self.assertNotIn(leak, setup, text)
            self.assertFalse(home_canary.exists())
            self.assertFalse((outside / "x").exists())
        finally:
            home_canary.unlink(missing_ok=True)

    def test_scenario_code_cannot_reach_host_abstract_sockets(self) -> None:
        require_sandbox(self)
        import socket
        import threading
        import uuid
        name = f"agent-e2e-broker-test-{uuid.uuid4().hex[:8]}"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind("\0" + name)
        listener.listen(4)
        reached = []

        def accept() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                reached.append(conn.recv(16))
                conn.close()

        threading.Thread(target=accept, daemon=True).start()
        connect = ("import socket, sys; s = socket.socket(socket.AF_UNIX); "
                   "s.connect(chr(0) + sys.argv[1]); s.sendall(b'x'); print('ABSTRACT-' + 'REACHED')")
        git(self.repo, "switch", "-qc", "feat/abstract-socket")
        (self.repo / "tests/agent-e2e/demo/setup.sh").write_text(
            f"#!/usr/bin/env bash\n{sys.executable} -c \"{connect}\" {name} || echo ABSTRACT-REFUSED\n"
            "exit 0\n")
        git(self.repo, "commit", "-qam", "abstract socket")
        git(self.repo, "push", "-q", "origin", "feat/abstract-socket")
        git(self.repo, "switch", "-q", "master")
        try:
            text = self.server._tool_run(self.req(new_ref="origin/feat/abstract-socket"))
            [log] = self.out.glob("*/out/runs/demo/new/1/setup.log")
            setup = log.read_text()
            self.assertIn("ABSTRACT-REFUSED", setup, text)
            self.assertNotIn("ABSTRACT-REACHED", setup, text)
            self.assertEqual(reached, [])
        finally:
            listener.close()

    def test_broker_death_stops_harness_and_keeps_lock_until_then(self) -> None:
        require_sandbox(self)
        import fcntl, signal, time
        root = Path(self.tmp.name)
        # The run dir is the one path bwrap binds back into its private /tmp.
        self.claude.write_text("#!/usr/bin/env bash\ntouch \"$E2E_RUN/started\"\nsleep 30\n")
        broker = root / "broker"
        broker.mkdir()
        source = SERVER.read_text()
        (broker / "server.py").write_text(source.replace('PROTOCOL = "https"', 'PROTOCOL = "file"'))
        shutil.copy(HERE.parent / "broker_paths.py", root / "broker_paths.py")
        (broker / "config.json").write_text(json.dumps({"agentE2eBroker": self.raw}))
        proc = subprocess.Popen([sys.executable, str(broker / "server.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "run", "arguments": self.req(timeout_sec=60)}}
        proc.stdin.write(json.dumps(call) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while not list(self.out.glob("*/out/runs/demo/*/*/started")) \
                and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(list(self.out.glob("*/out/runs/demo/*/*/started")),
                        "stub claude never started")
        proc.kill()
        proc.wait()
        deadline = time.monotonic() + 15
        freed = False
        while time.monotonic() < deadline:
            harness = subprocess.run(["pgrep", "-f", f"{self.out}/.*runs"],
                                     capture_output=True, text=True).stdout.split()
            with open(self.policy["lock"], "w") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = False
                except BlockingIOError:
                    locked = True
            # The lock may be free only once no harness is left running.
            self.assertFalse(harness and not locked, "harness runs without the lock")
            if not harness and not locked:
                freed = True
                break
            time.sleep(0.2)
        self.assertTrue(freed, "harness outlived the broker")


if __name__ == "__main__":
    unittest.main()

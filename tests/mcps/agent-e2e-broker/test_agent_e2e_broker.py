#!/usr/bin/env python3
"""Contract tests for the agent-e2e broker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
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


class Fixture(unittest.TestCase):
    """A repo with origin/master, a pushed candidate branch, a stub claude."""

    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["GIT_CONFIG_GLOBAL"] = str(root / "gitconfig")
        (root / "gitconfig").write_text("[user]\n\tname=t\n\temail=t@t\n"
                                        "[init]\n\tdefaultBranch=master\n")
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.repo = root / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
                       capture_output=True)
        harness = self.repo / "scripts/agent-e2e/agent_e2e.py"
        harness.parent.mkdir(parents=True)
        shutil.copy(HARNESS, harness)
        scen = self.repo / "tests/agent-e2e/demo"
        scen.mkdir(parents=True)
        (scen / "prompt.md").write_text("say hi\n")
        (scen / "setup.sh").write_text("#!/usr/bin/env bash\n")
        (scen / "check.sh").write_text(
            "#!/usr/bin/env bash\ngrep -q CANDIDATE \"$1/home/.claude/CLAUDE.md\" "
            "&& { echo cand; exit 0; } || { echo base; exit 1; }\n")
        (self.repo / "CLAUDE.md").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "master")
        git(self.repo, "switch", "-qc", "feat/x")
        (self.repo / "CLAUDE.md").write_text("CANDIDATE\n")
        git(self.repo, "commit", "-qam", "cand")
        git(self.repo, "push", "-q", "origin", "feat/x")
        git(self.repo, "switch", "-q", "master")
        self.claude = root / "claude"
        self.claude.write_text(
            "#!/usr/bin/env bash\n"
            "echo '{\"type\":\"result\",\"result\":\"hi\",\"is_error\":false,"
            "\"total_cost_usd\":0.01}'\n")
        self.claude.chmod(0o755)
        self.out = root / "out"
        self.raw = {
            "version": 1, "repo": str(self.repo), "harness": "scripts/agent-e2e/agent_e2e.py",
            "scenarioRoot": "tests/agent-e2e", "claudeExecutable": str(self.claude),
            "outputRoot": str(self.out), "path": os.environ["PATH"].split(":"),
            "models": ["haiku", "sonnet"], "maxSamples": 2, "maxJobs": 2,
            "maxBudgetUsd": 3.0, "maxTimeoutSec": 900, "maxCallSec": 3600,
            "outputLimitChars": 20000,
        }
        self.server._load_config = lambda: {"agentE2eBroker": dict(self.raw)}
        self.policy = self.server._load_policy({"agentE2eBroker": dict(self.raw)})

    def tearDown(self) -> None:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        self.tmp.cleanup()

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

    def test_policy_rejects_unknown_keys(self) -> None:
        with self.assertRaises(RuntimeError):
            self.server._load_policy({"agentE2eBroker": {**self.raw, "extra": 1}})


class RefPolicyTests(Fixture):
    def test_unpushed_commit_refused(self) -> None:
        git(self.repo, "switch", "-qc", "local")
        (self.repo / "CLAUDE.md").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "master")
        with self.assertRaisesRegex(RuntimeError, "not reachable"):
            self.server._resolve(self.policy, sha)

    def test_missing_branch_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            self.server._resolve(self.policy, "origin/nope")

    def test_harness_must_be_master_and_clean(self) -> None:
        self.server._check_harness(self.policy)
        (self.repo / "scripts/agent-e2e/agent_e2e.py").write_text("print('pwn')\n")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.server._check_harness(self.policy)
        git(self.repo, "checkout", "-q", "--", ".")
        git(self.repo, "switch", "-q", "feat/x")
        with self.assertRaisesRegex(RuntimeError, "not on master"):
            self.server._check_harness(self.policy)


class RunTests(Fixture):
    def test_runs_old_vs_new_from_pushed_refs(self) -> None:
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
        self.out.mkdir()
        with open(self.out / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "one at a time"):
                self.server._tool_run(self.req())

    def test_tools_list_over_stdio(self) -> None:
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = subprocess.run([sys.executable, str(SERVER)], input="\n".join(map(json.dumps, msgs)),
                             capture_output=True, text=True, timeout=30).stdout.splitlines()
        tools = json.loads(out[1])["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["run"])


class ReviewFindingTests(Fixture):
    def test_planted_remote_tracking_ref_refused(self) -> None:
        git(self.repo, "switch", "-qc", "unpushed")
        (self.repo / "CLAUDE.md").write_text("CANDIDATE planted\n")
        git(self.repo, "commit", "-qam", "planted")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "update-ref", "refs/remotes/origin/zz", sha)
        with self.assertRaises(RuntimeError) as ctx:
            self.server._tool_run(self.req(new_ref="origin/zz"))
        self.assertIn("does not exist", str(ctx.exception))

    def test_unreachable_origin_fails_closed(self) -> None:
        git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        with self.assertRaisesRegex(RuntimeError, "fetch"):
            self.server._tool_run(self.req())

    def test_unpushed_harness_commit_on_master_refused(self) -> None:
        (self.repo / "scripts/agent-e2e/agent_e2e.py").write_text("print('local')\n")
        git(self.repo, "commit", "-qam", "local harness")
        with self.assertRaisesRegex(RuntimeError, "origin/master"):
            self.server._tool_run(self.req())

    def test_redacts_private_and_age_keys(self) -> None:
        key = "AGE-SECRET-KEY-1" + "Q" * 58
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        out = self.server._redact(f"{key}\n{pem}\nAKIA" + "A" * 16)
        self.assertNotIn(key, out)
        self.assertNotIn("abc", out)
        self.assertNotIn("AKIA" + "A" * 16, out)

    def test_broker_death_stops_harness_and_keeps_lock_until_then(self) -> None:
        import fcntl, signal, time
        root = Path(self.tmp.name)
        # The run dir is the one path bwrap binds back into its private /tmp.
        self.claude.write_text("#!/usr/bin/env bash\ntouch \"$E2E_RUN/started\"\nsleep 30\n")
        broker = root / "broker"
        broker.mkdir()
        shutil.copy(SERVER, broker / "server.py")
        (broker / "config.json").write_text(json.dumps({"agentE2eBroker": self.raw}))
        proc = subprocess.Popen([sys.executable, str(broker / "server.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "run", "arguments": self.req(timeout_sec=60)}}
        proc.stdin.write(json.dumps(call) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while not list(self.out.glob("*/runs/demo/*/*/started")) \
                and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(list(self.out.glob("*/runs/demo/*/*/started")),
                        "stub claude never started")
        proc.kill()
        proc.wait()
        deadline = time.monotonic() + 15
        freed = False
        while time.monotonic() < deadline:
            harness = subprocess.run(["pgrep", "-f", f"{self.out}/.*runs"],
                                     capture_output=True, text=True).stdout.split()
            with open(self.out / ".lock", "w") as fh:
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

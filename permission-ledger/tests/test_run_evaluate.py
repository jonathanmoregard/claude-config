"""End-to-end tests for run-evaluate.sh (the nightly cron entrypoint).

These exercise the real seam: payloads go through the real hook.py, the real
aggregate.py runs, and run-evaluate.sh publishes a digest and decides whether
the model call is worth making. The `claude` binary is the only thing stubbed —
via PL_CLAUDE_CMD — and the stub records the argv it was handed.

Nothing here touches the real HOME, the real ledger or the real crontab: every
test runs against a temporary HOME with PERMISSION_LEDGER_DIR pointed inside it.

Run with:
    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_run_evaluate -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hook.py")
AGGREGATE = os.path.join(ROOT, "aggregate.py")
RUN_EVALUATE = os.path.join(ROOT, "run-evaluate.sh")
PROMPT = os.path.join(ROOT, "prompt.md")

# What run-evaluate.sh must hand the agent. The absence of any Bash(...) entry
# is the entire point of the fix: permissions.deny carries a blanket "Bash",
# deny beats allow, so a Bash allowance here could never take effect.
EXPECTED_ALLOWED_TOOLS = "Read Glob Grep Write(~/.claude/proposals/permissions/*)"

CLAUDE_STUB = """#!/usr/bin/env bash
# Stand-in for the claude binary. Records argv (NUL-separated) and cwd.
pwd > "$PL_STUB_CWD_FILE"
printf '%s\\0' "$@" > "$PL_STUB_ARGV_FILE"
printf '%s\\n' "stub: pretended to evaluate"
exit "${PL_STUB_EXIT:-0}"
"""


def month_name(year, month):
    return "%04d-%02d.jsonl" % (year, month)


def shift_month(dt, months_back):
    total = dt.year * 12 + (dt.month - 1) - months_back
    return total // 12, total % 12 + 1


class RunEvaluateTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pl-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.home = os.path.join(self.tmp, "home")
        self.claude_home = os.path.join(self.home, ".claude")
        self.ledger = os.path.join(self.claude_home, "permission-ledger")
        self.logs = os.path.join(self.claude_home, "logs")
        self.proposals = os.path.join(self.claude_home, "proposals", "permissions")
        for d in (self.ledger, self.logs, self.proposals):
            os.makedirs(d, exist_ok=True)

        # The installed layout: the scripts live inside the ledger dir.
        for src in (HOOK, AGGREGATE, RUN_EVALUATE, PROMPT):
            dst = os.path.join(self.ledger, os.path.basename(src))
            shutil.copy2(src, dst)
        self.run_evaluate = os.path.join(self.ledger, "run-evaluate.sh")
        os.chmod(self.run_evaluate, 0o700)

        self.stub = os.path.join(self.tmp, "claude-stub.sh")
        with open(self.stub, "w", encoding="utf-8") as fh:
            fh.write(CLAUDE_STUB)
        os.chmod(self.stub, 0o755)
        self.stub_argv = os.path.join(self.tmp, "stub-argv")
        self.stub_cwd = os.path.join(self.tmp, "stub-cwd")

        self.log_file = os.path.join(self.logs, "permission-ledger.log")
        self.digest = os.path.join(self.ledger, "digest-latest.json")
        self.digest_prev = os.path.join(self.ledger, "digest-prev.json")

    # -- helpers ---------------------------------------------------------

    def env(self, **overrides):
        env = dict(os.environ)
        env.update(
            {
                "HOME": self.home,
                "PERMISSION_LEDGER_DIR": self.ledger,
                "PL_CLAUDE_CMD": self.stub,
                "PL_STUB_ARGV_FILE": self.stub_argv,
                "PL_STUB_CWD_FILE": self.stub_cwd,
            }
        )
        env.pop("PL_STUB_EXIT", None)
        env.update(overrides)
        return env

    def feed_hook(self, payload):
        """Pipe one hook payload through the real hook.py, as Claude Code does."""
        proc = subprocess.run(
            [sys.executable, "-S", "-E", os.path.join(self.ledger, "hook.py")],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"", "hook must write nothing to stdout")
        return proc

    def run_evaluate_sh(self, **env_overrides):
        return subprocess.run(
            ["bash", self.run_evaluate],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.env(**env_overrides),
        )

    def log(self):
        if not os.path.exists(self.log_file):
            return ""
        with open(self.log_file, encoding="utf-8") as fh:
            return fh.read()

    def stub_argv_list(self):
        self.assertTrue(
            os.path.exists(self.stub_argv), "the claude stub was never invoked"
        )
        with open(self.stub_argv, "rb") as fh:
            raw = fh.read()
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [p.decode("utf-8") for p in parts]

    def assert_stub_not_called(self):
        self.assertFalse(
            os.path.exists(self.stub_argv),
            "claude was invoked when it should not have been",
        )

    def request(self, path="/home/jonathan/.claude/settings.json", digest_seed="a"):
        return {
            "hook_event_name": "PermissionRequest",
            "session_id": "sess-1",
            "prompt_id": "prompt-" + digest_seed,
            "cwd": "/home/jonathan/Repos/gdocs-review-mcp",
            "permission_mode": "default",
            "agent_type": "main",
            "agent_id": None,
            "tool_name": "Read",
            "tool_input": {"file_path": path},
            "permission_suggestions": [
                {
                    "ruleValue": {
                        "toolName": "Read",
                        "ruleContent": "/home/jonathan/.claude/**",
                    },
                    "behavior": "allow",
                    "destination": "localSettings",
                }
            ],
        }

    def denial(self, path):
        payload = self.request(path)
        payload["hook_event_name"] = "PermissionDenied"
        payload["tool_use_id"] = "toolu-1"
        payload["reason"] = "user declined"
        return payload

    def seed_friction(self):
        """Two requests plus a denial for one of them, and one more request.

        The spec's noise floor is 3 occurrences, so two requests alone can
        never produce a group; the third request is what makes this the
        "there is something to evaluate" case.
        """
        self.feed_hook(self.request("/home/jonathan/.claude/settings.json"))
        self.feed_hook(self.request("/home/jonathan/.claude/CLAUDE.md"))
        self.feed_hook(self.request("/home/jonathan/.claude/keybindings.json"))
        self.feed_hook(self.denial("/home/jonathan/.claude/settings.json"))

    # -- happy path --------------------------------------------------------

    def test_end_to_end_publishes_digest_and_invokes_claude(self):
        self.seed_friction()
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())

        # digest published, readable, private, and complete
        self.assertTrue(os.path.exists(self.digest))
        self.assertEqual(
            stat.S_IMODE(os.stat(self.digest).st_mode),
            0o600,
            "the digest carries sampled tool inputs; it must be 0600",
        )
        with open(self.digest, encoding="utf-8") as fh:
            digest = json.load(fh)
        self.assertEqual(len(digest["groups"]), 1, digest)
        group = digest["groups"][0]
        self.assertEqual(group["pattern_key"], "Read:/home/jonathan/.claude")
        self.assertEqual(group["count"], 3)
        self.assertEqual(group["approved"], 2)
        self.assertEqual(group["denied"], 1)
        self.assertEqual(group["sessions"], 1)

        # published by rename: no temp file survives the run
        leftovers = [n for n in os.listdir(self.ledger) if n.startswith(".digest")]
        self.assertEqual(leftovers, [], "atomic publish left a temp file behind")

        # the agent got exactly the argv the fix specifies
        argv = self.stub_argv_list()
        self.assertEqual(argv[:5], ["--model", "opus", "--print", "--allowedTools",
                                    EXPECTED_ALLOWED_TOOLS])
        self.assertEqual(argv[5], "-p")
        with open(PROMPT, encoding="utf-8") as fh:
            self.assertEqual(argv[6], fh.read().rstrip("\n"))
        self.assertEqual(len(argv), 7, argv)

        # no Bash allowance is granted: not in the constant, not in the argv
        # that was actually passed. (The prompt body, argv[6], legitimately
        # mentions Bash — it is the forbidden-allow list.)
        self.assertNotIn("Bash", EXPECTED_ALLOWED_TOOLS)
        self.assertNotIn("Bash", argv[4])
        self.assertFalse([a for a in argv[:6] if "Bash" in a], argv[:6])

        # ...run from ~/.claude, as the cron line always did
        with open(self.stub_cwd, encoding="utf-8") as fh:
            self.assertEqual(os.path.realpath(fh.read().strip()),
                             os.path.realpath(self.claude_home))

        log = self.log()
        self.assertIn("digest written: 1 group(s)", log)
        self.assertIn("agent exited 0", log)
        self.assertIn("stub: pretended to evaluate", log)
        # every log line carries a UTC timestamp prefix
        for line in log.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")

    def test_second_run_rotates_the_previous_digest(self):
        self.seed_friction()
        self.assertEqual(self.run_evaluate_sh().returncode, 0)
        with open(self.digest, encoding="utf-8") as fh:
            first = fh.read()
        self.assertFalse(os.path.exists(self.digest_prev))

        os.remove(self.stub_argv)
        self.assertEqual(self.run_evaluate_sh().returncode, 0)
        self.assertTrue(os.path.exists(self.digest_prev))
        with open(self.digest_prev, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first)

    def test_agent_failure_is_logged_and_reported(self):
        self.seed_friction()
        proc = self.run_evaluate_sh(PL_STUB_EXIT="7")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("agent exited 7", self.log())
        self.assertTrue(os.path.exists(self.digest), "digest is published regardless")

    # -- branches that must not pay for a model call ------------------------

    def test_zero_groups_exits_without_invoking_claude(self):
        # Two requests and a denial: real friction, but under the noise floor.
        self.feed_hook(self.request("/home/jonathan/.claude/settings.json"))
        self.feed_hook(self.request("/home/jonathan/.claude/CLAUDE.md"))
        self.feed_hook(self.denial("/home/jonathan/.claude/settings.json"))

        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assert_stub_not_called()
        self.assertTrue(os.path.exists(self.digest), "the digest is still published")
        with open(self.digest, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["groups"], [])
        self.assertIn("no groups above threshold; agent not invoked", self.log())

    def test_empty_ledger_exits_without_invoking_claude(self):
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assert_stub_not_called()
        self.assertIn("no groups above threshold", self.log())

    def test_aggregate_nonzero_exit_does_not_invoke_claude(self):
        self.seed_friction()
        with open(os.path.join(self.ledger, "aggregate.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")

        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assert_stub_not_called()
        self.assertFalse(os.path.exists(self.digest), "no digest may be published")
        log = self.log()
        self.assertIn("aggregate stderr: boom", log)
        self.assertIn("aggregate.py exited 3", log)

    def test_unparseable_aggregate_output_does_not_invoke_claude(self):
        self.seed_friction()
        # A good run first, so there is a previous digest to protect.
        self.assertEqual(self.run_evaluate_sh().returncode, 0)
        with open(self.digest, encoding="utf-8") as fh:
            good = fh.read()
        os.remove(self.stub_argv)

        with open(os.path.join(self.ledger, "aggregate.py"), "w", encoding="utf-8") as fh:
            fh.write("print('this is not json {')\n")

        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assert_stub_not_called()
        with open(self.digest, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), good, "a bad digest overwrote a good one")
        self.assertIn("unparseable JSON", self.log())
        self.assertEqual([n for n in os.listdir(self.ledger) if n.startswith(".digest")], [])

    def test_json_without_groups_list_is_rejected(self):
        self.seed_friction()
        with open(os.path.join(self.ledger, "aggregate.py"), "w", encoding="utf-8") as fh:
            fh.write("print('{\"generated_at\": \"now\"}')\n")
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assert_stub_not_called()
        self.assertFalse(os.path.exists(self.digest))

    def test_missing_aggregate_script_does_not_invoke_claude(self):
        self.seed_friction()
        os.remove(os.path.join(self.ledger, "aggregate.py"))
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assert_stub_not_called()
        self.assertIn("aggregate.py not found", self.log())

    def test_missing_prompt_does_not_invoke_claude(self):
        self.seed_friction()
        os.remove(os.path.join(self.ledger, "prompt.md"))
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assert_stub_not_called()
        self.assertIn("prompt.md not found", self.log())

    # -- retention sweep ----------------------------------------------------

    def _touch_month(self, months_back, lines=1):
        year, month = shift_month(datetime.now(timezone.utc), months_back)
        name = month_name(year, month)
        with open(os.path.join(self.ledger, name), "w", encoding="utf-8") as fh:
            fh.write("{}\n" * lines)
        return name

    def test_retention_deletes_month_files_older_than_six_months(self):
        old = self._touch_month(9)
        edge_out = self._touch_month(7)
        edge_in = self._touch_month(6)
        recent = self._touch_month(1)
        keep_me = os.path.join(self.ledger, "decided.json")
        with open(keep_me, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with open(os.path.join(self.ledger, "notes.jsonl.bak"), "w", encoding="utf-8") as fh:
            fh.write("not a ledger file\n")

        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())

        names = set(os.listdir(self.ledger))
        self.assertNotIn(old, names)
        self.assertNotIn(edge_out, names)
        self.assertIn(edge_in, names, "6 months back is inside the retention window")
        self.assertIn(recent, names)
        self.assertIn("decided.json", names)
        self.assertIn("notes.jsonl.bak", names)

        log = self.log()
        self.assertIn("retention: deleted %s" % old, log)
        self.assertIn("retention: deleted %s" % edge_out, log)
        self.assertNotIn("retention: deleted %s" % edge_in, log)

    def test_retention_runs_even_when_the_aggregate_fails(self):
        old = self._touch_month(12)
        with open(os.path.join(self.ledger, "aggregate.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.exit(9)\n")
        proc = self.run_evaluate_sh()
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(old, set(os.listdir(self.ledger)))
        self.assertIn("retention: deleted %s" % old, self.log())

    def test_retention_ignores_non_month_filenames(self):
        for name in ("decided.json", "hook-errors.log", "2026-1.jsonl", "junk.jsonl"):
            with open(os.path.join(self.ledger, name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        self.assertEqual(self.run_evaluate_sh().returncode, 0)
        names = set(os.listdir(self.ledger))
        for name in ("decided.json", "hook-errors.log", "2026-1.jsonl", "junk.jsonl"):
            self.assertIn(name, names)

    # -- the ledger dir override is honoured --------------------------------

    def test_permission_ledger_dir_env_var_is_honoured(self):
        elsewhere = os.path.join(self.tmp, "other-ledger")
        os.makedirs(elsewhere)
        for src in (AGGREGATE, PROMPT):
            shutil.copy2(src, os.path.join(elsewhere, os.path.basename(src)))
        proc = self.run_evaluate_sh(PERMISSION_LEDGER_DIR=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertTrue(os.path.exists(os.path.join(elsewhere, "digest-latest.json")))
        self.assertFalse(os.path.exists(self.digest))


if __name__ == "__main__":
    unittest.main()

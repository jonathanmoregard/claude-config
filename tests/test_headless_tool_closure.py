"""Unattended `claude -p` launchers that read untrusted text must CLOSE their
tool surface, not merely pre-approve a few tools.

`--allowedTools` only adds permission grants; the session is still offered
every built-in tool (Bash included, which settings.json allows globally) and
every configured MCP server. The closure is `--tools <list>` (what is offered
at all) plus `--strict-mcp-config` (no MCP server unless --mcp-config names
one). Measured 2026-10-03 on 2.1.288 via the init event: an `--allowedTools
(arftl-allow: permission-widened — this docstring describes the removed shape)
Read` + bypassPermissions launch was offered Bash, Write, Edit and 161 MCP
tools; `--tools "" --strict-mcp-config` was offered none.

Invariant checked here, per launcher: the argv it actually builds offers only
read-only tools (or none), loads no MCP server, and never bypasses permissions.
"""

import importlib.util
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_ONLY = {"Read", "Grep", "Glob"}


def _offered_tools(argv):
    """The tool names `--tools` offers, or None when --tools is absent."""
    if "--tools" not in argv:
        return None
    raw = argv[argv.index("--tools") + 1]
    return {t for t in re.split(r"[,\s]+", raw) if t}


def assert_closed(tc, argv, allowed=READ_ONLY):
    offered = _offered_tools(argv)
    tc.assertIsNotNone(offered, "no --tools closure in %r" % (argv,))
    tc.assertLessEqual(offered, allowed, "offers more than %r: %r" % (allowed, offered))
    tc.assertIn("--strict-mcp-config", argv, "MCP servers still load: %r" % (argv,))
    # arftl-allow: permission-widened (asserts the bypass flag is ABSENT)
    tc.assertNotIn("--dangerously-skip-permissions", argv)
    if "--permission-mode" in argv:
        # arftl-allow: permission-widened (asserts bypass mode is ABSENT)
        tc.assertNotEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")


class ProposalScorerClosureTest(unittest.TestCase):
    """C2: the proposal-intake scorer reads agent-written proposal bodies."""

    def setUp(self):
        path = os.path.join(REPO_ROOT, "scripts", "proposal-intake-gate.py")
        spec = importlib.util.spec_from_file_location("proposal_intake_gate", path)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)

    def test_scorer_argv_is_closed(self):
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stderr="")

        cfg = {"scorer": dict(self.gate.DEFAULTS["scorer"])}
        with mock.patch.object(self.gate.subprocess, "run", fake_run), \
                mock.patch.object(self.gate, "log", lambda *_: None):
            out_path, model = self.gate.dispatch_scorer("PROMPT", cfg)
        self.addCleanup(os.unlink, out_path)
        self.assertEqual(len(seen), 1)
        assert_closed(self, seen[0])
        # The prompt still reaches the model as the -p operand.
        self.assertEqual(seen[0][seen[0].index("-p") + 1], "PROMPT")


class DateCheckClosureTest(unittest.TestCase):
    """C1: date-check feeds calendar titles (anyone can send an invite) to a
    weekly unattended agent. It needs no tools at all: inputs are inlined."""

    def test_agent_call_offers_no_tools(self):
        src = open(os.path.join(REPO_ROOT, "date-check.sh"), encoding="utf-8").read()
        calls = re.findall(r'"\$CLAUDE"((?:[^\n]*\\\n)*[^\n]*)', src)
        self.assertEqual(len(calls), 1, "expected exactly one claude call")
        argv = shlex.split(calls[0].replace("\\\n", " ").rstrip(")"))
        assert_closed(self, argv, allowed=set())
        # arftl-allow: permission-widened (asserts the bypass flag is ABSENT)
        self.assertNotIn("dangerously-skip-permissions", src)


if __name__ == "__main__":
    unittest.main()

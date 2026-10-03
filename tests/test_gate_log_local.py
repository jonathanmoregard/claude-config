"""Gate verdicts on local-only subdirs (the permission ledger) stay out of
the tracked gate-log.jsonl, and still reconcile on the next run."""

import importlib.util
import json
import os
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PATH = os.path.join(REPO_ROOT, "scripts", "proposal-intake-gate.py")
spec = importlib.util.spec_from_file_location("proposal_intake_gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateLogLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = {
            "gate_log": os.path.join(d, "gate-log.jsonl"),
            "local_gate_log": os.path.join(d, ".gate-log.local.jsonl"),
            "local_only_subdirs": list(gate.DEFAULTS["local_only_subdirs"]),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, key):
        try:
            with open(self.cfg[key], encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def test_permissions_is_local_only_by_default(self):
        self.assertIn("permissions", gate.DEFAULTS["local_only_subdirs"])

    def test_permission_verdict_skips_the_tracked_log(self):
        gate.append_gate_log(self.cfg, {
            "date": "2026-10-03", "file": "permissions/x.md",
            "subdir": "permissions", "verdict": "sharp",
            "evidence": "denied: Bash(rm -rf /tmp/secret-thing)"})
        gate.append_gate_log(self.cfg, {
            "date": "2026-10-03", "file": "rsi/y.md",
            "subdir": "rsi", "verdict": "sharp", "evidence": "e"})
        tracked = self.lines("gate_log")
        self.assertEqual([e["file"] for e in tracked], ["rsi/y.md"])
        self.assertNotIn("secret-thing", json.dumps(tracked))
        self.assertEqual([e["file"] for e in self.lines("local_gate_log")],
                         ["permissions/x.md"])

    def test_both_logs_reconcile(self):
        """A permissions gate block must still count as real next run, or
        every ledger proposal gets re-scored."""
        gate.append_gate_log(self.cfg, {
            "date": "2026-10-03", "file": "permissions/x.md",
            "subdir": "permissions", "verdict": "rot"})
        gate.append_gate_log(self.cfg, {
            "date": "2026-10-03", "file": "rsi/y.md",
            "subdir": "rsi", "verdict": "sharp"})
        index = gate.load_gate_log_index(self.cfg)
        self.assertIn(("permissions/x.md", "2026-10-03", "rot"), index)
        self.assertIn(("rsi/y.md", "2026-10-03", "sharp"), index)

    def test_missing_local_log_is_fine(self):
        self.assertEqual(gate.load_gate_log_index(self.cfg), set())


if __name__ == "__main__":
    unittest.main()

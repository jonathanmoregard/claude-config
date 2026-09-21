#!/usr/bin/env python3
"""Contract for the dormant, Codex-only dedicated Git broker asset."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "mcps" / "git-broker"
LEGACY = ROOT / "mcps" / "safe-bash"


class DedicatedGitBrokerAssetTests(unittest.TestCase):
    def test_dedicated_assets_exist_without_legacy_safe_bash(self) -> None:
        self.assertTrue((BROKER / "server.py").is_file())
        self.assertTrue((BROKER / "config.json").is_file())
        self.assertTrue((BROKER / "empty-git-config").is_file())
        self.assertFalse(LEGACY.exists())

    def test_server_has_git_only_identity(self) -> None:
        server = BROKER / "server.py"
        self.assertTrue(server.is_file(), f"missing dedicated server: {server}")
        source = server.read_text(encoding="utf-8")
        self.assertIn('SERVER_INFO = {"name": "git-broker"', source)
        self.assertNotIn("SAFE_BASH_MODE", source)
        self.assertNotIn("LEGACY_TOOLS", source)
        self.assertNotIn("def _tool_exec", source)

    def test_policy_file_contains_only_git_broker_policy(self) -> None:
        policy = BROKER / "config.json"
        self.assertTrue(policy.is_file(), f"missing dedicated policy: {policy}")
        config = json.loads(policy.read_text(encoding="utf-8"))
        self.assertEqual(set(config), {"gitBroker"})
        self.assertIsInstance(config["gitBroker"], dict)

    def test_claude_does_not_register_or_allow_git_broker(self) -> None:
        servers = json.loads((ROOT / "mcp-servers.json").read_text(encoding="utf-8"))
        settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("git-broker", servers)
        self.assertNotIn("git_broker", servers)
        allowed = ((settings.get("permissions") or {}).get("allow") or [])
        self.assertFalse(any("git_broker" in entry for entry in allowed))


if __name__ == "__main__":
    unittest.main()

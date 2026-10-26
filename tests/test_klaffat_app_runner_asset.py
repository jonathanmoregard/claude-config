#!/usr/bin/env python3
"""Repository contract for the shared Klaffat app-runner MCP."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
# `runtime/` once the layout migration has landed, the repository root
# before it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = ROOT / "runtime" if (ROOT / "runtime").is_dir() else ROOT
RUNNER = RUNTIME / "mcps" / "klaffat-app-runner"
HOST_SETUP = ROOT / "tests" / "agent-e2e" / "klaffat-app-runner-host-smoke" / "setup.sh"


class KlaffatAppRunnerAssetTests(unittest.TestCase):
    def test_complete_stdlib_server_asset_is_tracked(self) -> None:
        for name in (
            "server.py", "config.json", "hold.py", "sandbox_exec.py", "e2e_deps.py", "test_server.py"
        ):
            with self.subTest(name=name):
                self.assertTrue((RUNNER / name).is_file(), f"missing {name}")
        source = (RUNNER / "server.py").read_text(encoding="utf-8")
        self.assertIn('SERVER_INFO = {"name": "klaffat-app-runner"', source)
        self.assertNotIn("FastMCP", source)
        self.assertNotIn("shell=True", source)

    def test_shared_manifest_registers_fixed_host_server_without_bootstrap(self) -> None:
        servers = json.loads((RUNTIME / "mcp-servers.json").read_text(encoding="utf-8"))
        self.assertIn("klaffat-app-runner", servers)
        config = servers["klaffat-app-runner"]
        self.assertEqual(
            config,
            {
                "type": "stdio",
                "command": "/run/current-system/sw/bin/python3",
                "args": [
                    "/home/jonathan/.claude/mcps/klaffat-app-runner/server.py"
                ],
                "env": {},
                "timeout": 1900000,
            },
        )
        serialized = json.dumps(config)
        for bootstrap in ("uvx", "pip", "nix develop", "curl", "wget"):
            self.assertNotIn(bootstrap, serialized)

    def test_shared_permissions_allow_the_structured_runner_without_shell_escape(self) -> None:
        settings = json.loads((RUNTIME / "settings.json").read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        self.assertIn("mcp__klaffat-app-runner__*", allow)
        self.assertNotIn("mcp__klaffat-app-runner__exec", allow)

    def test_host_scenario_hands_the_agent_only_the_staged_runner(self) -> None:
        setup = HOST_SETUP.read_text(encoding="utf-8")
        self.assertIn("$HOME/.claude/mcps/klaffat-app-runner", setup)
        self.assertIn("mcp.json", setup)


if __name__ == "__main__":
    unittest.main()

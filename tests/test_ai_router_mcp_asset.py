#!/usr/bin/env python3
"""Repository contract for the host-side ai-router MCP asset."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "mcps" / "ai-router"


class AiRouterMcpAssetTests(unittest.TestCase):
    def test_stdlib_server_and_tests_are_tracked(self) -> None:
        for name in ("server.py", "test_server.py"):
            self.assertTrue((BROKER / name).is_file(), f"missing {name}")
        source = (BROKER / "server.py").read_text(encoding="utf-8")
        self.assertIn('SERVER_INFO = {"name": "ai-router"', source)
        self.assertNotIn("FastMCP", source)
        self.assertNotIn("shell=True", source)

    def test_shared_manifest_registers_fixed_host_server(self) -> None:
        servers = json.loads((ROOT / "mcp-servers.json").read_text(encoding="utf-8"))
        self.assertEqual(
            servers["ai_router"],
            {
                "type": "stdio",
                "command": "/run/current-system/sw/bin/python3",
                "args": ["/home/jonathan/.claude/mcps/ai-router/server.py"],
                "env": {},
                "timeout": 620000,
            },
        )

    def test_existing_router_detachment_survives_broker_exit(self) -> None:
        jobs = (ROOT / "tools" / "ai-router" / "ai_router" / "jobs.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("start_new_session=True", jobs)
        self.assertIn("stdin=subprocess.DEVNULL", jobs)
        self.assertIn('launcher="systemd-run"', jobs)


if __name__ == "__main__":
    unittest.main()

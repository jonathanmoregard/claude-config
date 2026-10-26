"""Registration contract for the github-mux MCP server.

Every GitHub App installation is reached through the one `github` mux entry:
a second direct registration would list every GitHub tool twice per session
and could carry a different --exclude-tools list than the mux enforces.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# `runtime/` once the layout migration has landed, the repository root
# before it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = ROOT / "runtime" if (ROOT / "runtime").is_dir() else ROOT
WRAPPER = "github-app-mcp-server"
MUX_SERVER = "/home/jonathan/.claude/mcps/github-mux/server.py"
MUX_CONFIG = "/home/jonathan/.claude/mcps/github-mux/config.json"


def servers() -> dict:
    return json.loads((RUNTIME / "mcp-servers.json").read_text(encoding="utf-8"))


class GithubMuxAssetTest(unittest.TestCase):
    def test_wrapper_is_never_registered_directly(self) -> None:
        for name, cfg in servers().items():
            argv = [cfg.get("command", ""), *cfg.get("args", [])]
            with self.subTest(server=name):
                self.assertFalse(
                    any(Path(arg).name == WRAPPER for arg in argv),
                    f"{name} launches {WRAPPER} directly; route it through github-mux",
                )

    def test_github_entry_runs_the_locked_mux_with_its_config(self) -> None:
        args = servers()["github"]["args"]
        self.assertIn("--locked", args)
        self.assertEqual(args[args.index("--script") + 1], MUX_SERVER)
        self.assertEqual(args[args.index("--config") + 1], MUX_CONFIG)
        for path in (MUX_SERVER, MUX_CONFIG):
            self.assertTrue((RUNTIME / Path(path).relative_to("/home/jonathan/.claude")).is_file())

    def test_github_permissions_name_the_mux_only(self) -> None:
        settings = json.loads((RUNTIME / "settings.json").read_text(encoding="utf-8"))
        permissions = settings.get("permissions") or {}
        for rule in [*(permissions.get("allow") or []), *(permissions.get("deny") or [])]:
            if not rule.startswith("mcp__github"):
                continue
            with self.subTest(rule=rule):
                self.assertEqual(rule.split("__")[1], "github", f"{rule} names a retired server")


if __name__ == "__main__":
    unittest.main()

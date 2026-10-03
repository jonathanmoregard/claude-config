"""Every MCP server launched through `uvx` resolves PyPI code at launch, as
this user, in every Claude and Codex session (Codex config is generated from
the same mcpServers slice). An unpinned requirement means a new upstream
release runs the next time uv's cache misses. Each requirement handed to uvx
must therefore name an exact version (K31)."""

import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def uvx_requirements(args):
    """The package requirements a uvx argv resolves (--from / --with values)."""
    reqs = []
    for i, a in enumerate(args):
        if a in ("--from", "--with") and i + 1 < len(args):
            reqs.append(args[i + 1])
        elif a.startswith(("--from=", "--with=")):
            reqs.append(a.split("=", 1)[1])
    return reqs


class McpPinsTest(unittest.TestCase):
    def test_uvx_servers_pin_exact_versions(self):
        with open(os.path.join(REPO_ROOT, "mcp-servers.json"), encoding="utf-8") as fh:
            servers = json.load(fh)
        checked = 0
        for name, cfg in servers.items():
            if os.path.basename(cfg.get("command", "")) != "uvx":
                continue
            reqs = uvx_requirements(cfg.get("args", []))
            self.assertTrue(reqs, f"{name}: uvx server with no --from/--with")
            for req in reqs:
                checked += 1
                self.assertRegex(req, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][^,<>=~!*]*$",
                                 f"{name}: {req!r} is not pinned to one version")
        self.assertGreater(checked, 0, "no uvx MCP server found; update this test")


if __name__ == "__main__":
    unittest.main()

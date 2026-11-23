#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import import_claude  # noqa: E402
import render_codex  # noqa: E402


def test_import_collects_global_and_enabled_plugin_agents() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        plugin = root / "plugin"
        (claude / "agents").mkdir(parents=True)
        (plugin / "agents").mkdir(parents=True)
        (claude / "agents" / "advisor.md").write_text(
            "---\nname: advisor\ndescription: Review only\n"
            "permissionMode: bypassPermissions\n"
            'tools: ["Read", "Grep", "Glob", "Skill"]\n---\n\nReview carefully.\n',
            encoding="utf-8",
        )
        (plugin / "agents" / "simplifier.md").write_text(
            "---\nname: simplifier\ndescription: Simplify code\n---\n\nSimplify safely.\n",
            encoding="utf-8",
        )
        settings = {"enabledPlugins": {"demo@local": True, "off@local": False}}
        installed = {
            "demo@local": [{"installPath": str(plugin)}],
            "off@local": [{"installPath": str(plugin)}],
        }

        with patch.object(import_claude, "CLAUDE", claude):
            agents = import_claude.agents_inventory(settings, installed)

        assert [agent["name"] for agent in agents] == ["advisor", "simplifier"]
        assert agents[0]["tools"] == ["Read", "Grep", "Glob", "Skill"]
        assert agents[1]["plugin"] == "demo@local"


def test_render_creates_native_codex_agent_roles() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "advisor.md"
        source.write_text(
            "---\nname: advisor\ndescription: Review only\n"
            "permissionMode: bypassPermissions\n"
            'tools: ["Read", "Grep", "Glob", "Skill"]\n---\n\n'
            "Review CLAUDE.md carefully.\n",
            encoding="utf-8",
        )
        agents = [
            {
                "name": "advisor",
                "description": "Review only",
                "path": str(source),
                "permission_mode": "bypassPermissions",
                "tools": ["Read", "Grep", "Glob", "Skill"],
            }
        ]

        with patch.object(render_codex, "CODEX", root / ".codex"):
            files = render_codex.prepare_agent_role_files(agents)
            block = render_codex.codex_agent_roles_block(agents)
            config = render_codex.render_codex_config_text(
                "",
                {},
                {},
                {},
                render_codex.render_hooks({}),
                [],
                agents=agents,
            )

        role_path = root / ".codex" / "agents" / "claude-imported" / "advisor.toml"
        assert role_path in files
        assert "Review AGENTS.md carefully." in files[role_path]
        assert 'sandbox_mode = "read-only"' in files[role_path]
        assert "[agents.advisor]" in block
        assert f'config_file = {json.dumps(str(role_path))}' in block
        assert block in config


if __name__ == "__main__":
    test_import_collects_global_and_enabled_plugin_agents()
    test_render_creates_native_codex_agent_roles()
    print("Codex agent mapping tests OK")

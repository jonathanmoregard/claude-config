#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import import_claude  # noqa: E402
import render_codex  # noqa: E402


def test_import_collects_user_and_enabled_plugin_commands() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        plugin = root / "plugin"
        (claude / "commands").mkdir(parents=True)
        (plugin / "commands").mkdir(parents=True)
        (claude / "commands" / "local.md").write_text(
            "---\ndescription: Local command\n---\n\nDo local work.\n",
            encoding="utf-8",
        )
        (plugin / "commands" / "quick.md").write_text(
            "---\ndescription: Quick command\n---\n\nUse $CLAUDE_PLUGIN_ROOT/tool.\n",
            encoding="utf-8",
        )

        with patch.object(import_claude, "CLAUDE", claude):
            commands = import_claude.commands_inventory(
                {"enabledPlugins": {"demo@local": True}},
                {"demo@local": [{"installPath": str(plugin)}]},
            )

        assert [command["name"] for command in commands] == ["local", "quick"]
        assert commands[1]["plugin"] == "demo@local"


def test_supported_commands_render_as_compact_codex_skills() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        supported = root / "quick.md"
        unsupported = root / "with-args.md"
        supported.write_text(
            "---\ndescription: Quick context health check\n---\n\n"
            "Run $CLAUDE_PLUGIN_ROOT/tool and inspect CLAUDE.md.\n",
            encoding="utf-8",
        )
        unsupported.write_text(
            "---\ndescription: Has arguments\n---\n\nRun $ARGUMENTS.\n",
            encoding="utf-8",
        )
        manifest = {
            "skills": [],
            "plugin_skills": [],
            "commands": [
                {
                    "name": "quick",
                    "description": "Quick context health check",
                    "path": str(supported),
                    "plugin_root": "/plugins/demo",
                },
                {
                    "name": "with-args",
                    "description": "Has arguments",
                    "path": str(unsupported),
                },
            ],
        }

        with (
            patch.object(render_codex, "CODEX", root / ".codex"),
            patch.object(render_codex, "PLUGIN_ROOT", root / "mirror"),
        ):
            prepared, rendered, _, skipped = render_codex.prepare_skills(
                manifest, render_codex.load_support()
            )

        assert "claude-command-quick" in rendered
        text = next(text for path, text in prepared if path.name == "claude-command-quick")
        assert "/plugins/demo/tool" in text
        assert "AGENTS.md" in text
        assert any(item["name"] == "with-args" for item in skipped)


if __name__ == "__main__":
    test_import_collects_user_and_enabled_plugin_commands()
    test_supported_commands_render_as_compact_codex_skills()
    print("Codex command mapping tests OK")

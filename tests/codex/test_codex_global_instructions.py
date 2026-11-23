#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import sys


RENDERER = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(RENDERER / "scripts"))

import render_codex  # noqa: E402


def test_agents_text_contains_stable_preamble_and_exact_claude_source() -> None:
    source = "# Global Claude Instructions\n\nCurrent source truth.\n"

    rendered = render_codex.codex_agents_text(source)

    assert rendered.startswith("# Codex Instructions Mirrored From Claude\n")
    assert rendered.endswith(source)
    assert rendered.count(source) == 1


def test_render_plan_tracks_agents_md_as_an_atomic_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        codex = root / ".codex"
        claude = root / ".claude"
        codex.mkdir()
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("new source\n", encoding="utf-8")
        target = codex / "AGENTS.md"
        target.write_text("old target\n", encoding="utf-8")

        plan = SimpleNamespace(
            agents_text="new rendered\n",
            plugin_files={},
            hook_wrapper_files={},
            reference_links=[],
            agent_role_files={},
            mcp_assets=[],
            skill_wrappers=(),
        )
        with patch.object(render_codex, "CODEX", codex):
            assert target in render_codex.render_plan_paths(plan)  # type: ignore[arg-type]


if __name__ == "__main__":
    test_agents_text_contains_stable_preamble_and_exact_claude_source()
    test_render_plan_tracks_agents_md_as_an_atomic_target()
    print("Codex global instruction tests OK")

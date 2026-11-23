#!/usr/bin/env python3
"""Every copy of the Codex routing rule sends Codex-only rendering changes to
codex/ in claude-config, and none names the retired ai-client-config repo.

The rule has four copies agents read: the global CLAUDE.md, the
settings-management skill, the ~/.codex edit guard's refusal text and the
AGENTS.md preamble the renderer writes for Codex. The sync unit keeps the
retired repo's name (ai-client-config-codex-sync.service); nothing else may.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
RETIRED = re.compile(r"ai-client-config(?!-codex-sync)")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def section(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def copies() -> dict[str, str]:
    claude_md = (REPO / "runtime" / "CLAUDE.md").read_text(encoding="utf-8")
    skill = (REPO / "runtime" / "skills" / "settings-management" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    guard = load("path_edit_guard", REPO / "runtime" / "hooks" / "path-edit-guard.py")
    sys.path.insert(0, str(REPO / "codex" / "scripts"))
    try:
        render_codex = load("render_codex_routing", REPO / "codex" / "scripts" / "render_codex.py")
    finally:
        sys.path.remove(str(REPO / "codex" / "scripts"))
    return {
        "runtime/CLAUDE.md": next(
            line for line in claude_md.splitlines() if line.startswith("- `~/.codex` is generated")
        ),
        "settings-management SKILL.md": section(skill, "## Never edit `~/.codex`", "\n## "),
        "path-edit-guard CODEX_REASON": guard.CODEX_REASON,
        "render_codex CODEX_AGENTS_PREAMBLE": render_codex.CODEX_AGENTS_PREAMBLE,
    }


def test_every_routing_copy_names_codex_in_claude_config() -> None:
    for name, text in copies().items():
        flat = " ".join(text.split())
        assert "codex/" in flat, f"{name}: codex/ not named\n{text}"
        assert "claude-config" in flat, f"{name}: claude-config not named\n{text}"
        assert not RETIRED.search(flat), f"{name}: names ai-client-config\n{text}"


if __name__ == "__main__":
    test_every_routing_copy_names_codex_in_claude_config()
    print("codex routing text tests OK")

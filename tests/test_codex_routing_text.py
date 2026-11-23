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


# The routing phrase each copy must carry, whitespace-normalised: pinning the
# phrase, not two tokens, so a rewrite that still names both but routes the
# change elsewhere fails.
ROUTES = {
    "runtime/CLAUDE.md": "Codex-only rendering or mapping through `codex/` in the config repo `~/Repos/claude-config`",
    "settings-management SKILL.md": "For Codex-specific rendering or mappings, change the renderer under `codex/` in the config repo `~/Repos/claude-config`",
    "path-edit-guard CODEX_REASON": "change Codex-specific rendering under codex/ in the config repo ~/Repos/claude-config",
    "render_codex CODEX_AGENTS_PREAMBLE": "rendered by the renderer under `codex/` in the claude-config repo",
}


def test_every_routing_copy_names_codex_in_claude_config() -> None:
    found = copies()
    assert found.keys() == ROUTES.keys()
    for name, text in found.items():
        flat = " ".join(text.split())
        assert ROUTES[name] in flat, f"{name}: routing phrase missing\n{text}"
        assert not RETIRED.search(flat), f"{name}: names ai-client-config\n{text}"


if __name__ == "__main__":
    test_every_routing_copy_names_codex_in_claude_config()
    print("codex routing text tests OK")

#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_support import Inputs, describe, round_trip  # noqa: E402


def test_check_accepts_source_skill_with_claude_prefix() -> None:
    # A Claude skill whose own name starts with "claude-" is mirrored, not
    # mistaken for a stale generated wrapper.
    with round_trip(Inputs(skills=("claude-code-permissions",))) as rt:
        wrapper = rt.codex / "skills" / "claude-code-permissions" / "SKILL.md"
        text = wrapper.read_text(encoding="utf-8")
        result = rt.check()

    assert "Mirrored from Claude skill:" in text
    assert 'name: "claude-code-permissions"' in text
    assert result.returncode == 0, describe(result)


if __name__ == "__main__":
    test_check_accepts_source_skill_with_claude_prefix()
    print("claude-prefixed skill tests OK")

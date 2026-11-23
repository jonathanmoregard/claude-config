#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

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


if __name__ == "__main__":
    test_agents_text_contains_stable_preamble_and_exact_claude_source()
    print("Codex global instruction tests OK")

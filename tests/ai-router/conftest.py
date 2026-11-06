"""Shared fixtures for the ai-router test suite (contract §Verifier).

No test here performs a network call or a real provider call. Provider
binaries are replaced by the stub scripts under ``fixtures/`` and the clock
is injected wherever the code under test accepts ``now``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The tool lives under runtime/, the tree projected into ~/.claude; this suite
# does not, so it reaches across rather than up.
TOOL_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "tools" / "ai-router"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private state directory, exported as AI_ROUTER_STATE."""
    d = tmp_path / "state"
    monkeypatch.setenv("AI_ROUTER_STATE", str(d))
    return d


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def stub_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point binary resolution at the fake codex / fake claude scripts."""
    codex = FIXTURES / "fake_codex.py"
    claude = FIXTURES / "fake_claude.py"
    for p in (codex, claude):
        os.chmod(p, 0o755)
    monkeypatch.setenv("AI_ROUTER_CODEX_BIN", str(codex))
    monkeypatch.setenv("AI_ROUTER_CLAUDE_BIN", str(claude))
    return {"codex": codex, "claude": claude}

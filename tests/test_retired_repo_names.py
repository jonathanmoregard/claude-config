#!/usr/bin/env python3
"""The retired-repo matcher exempts exactly the sync unit's names (and, for
generated output, the round-trip temp HOME): a longer word built on an exempt
name, or another spelling of the repo, is still a mention."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retired_repo_names import retired_repo_mentions  # noqa: E402

ALLOWED = [
    "see ai-client-config-codex-sync.service.",
    "systemctl --user start ai-client-config-codex-sync",
]
MENTIONS = [
    "route changes to the ai-client-config checkout",
    "edit the ai-client-config repository",
    "ai-client-config checks this",
    "ai-client-config check OK",
    "edit ai-client-config-repository",
    "the ai-client-config-repos dir",
    "edit /tmp/ai-client-config-repo/codex/",
    "ai-client-config-sync.nix",
    "ai-client-config-synced",
    "route to ai-client-config-codex-syncer",
    "ai-client-config-codex-sync-repo",
    "see ai-client-config-codex-sync.services",
    "ai-client-config-codex-sync.service-repo",
    "ai-client-config-skill-probe",
    "edit ai-client-config.",
    "edit the AI-Client-Config repo",
    "edit ai_client_config",
    "AI_CLIENT_CONFIG-codex-sync",
]


def test_only_the_sync_unit_is_exempt() -> None:
    assert [t for t in ALLOWED if retired_repo_mentions(t)] == []
    assert [t for t in MENTIONS if not retired_repo_mentions(t)] == []


def test_temp_home_is_exempt_only_when_asked_and_nothing_longer() -> None:
    temp = "/tmp/ai-client-config-roundtrip-k3j_9x/home/.codex"
    assert retired_repo_mentions(temp, temp_home_ok=True) == []
    assert retired_repo_mentions(temp) != []
    assert [t for t in MENTIONS if not retired_repo_mentions(t, temp_home_ok=True)] == []


if __name__ == "__main__":
    test_only_the_sync_unit_is_exempt()
    test_temp_home_is_exempt_only_when_asked_and_nothing_longer()
    print("retired repo name tests OK")

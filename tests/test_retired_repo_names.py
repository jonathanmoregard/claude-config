#!/usr/bin/env python3
"""The retired-repo matcher exempts exactly the names it lists: a longer word
built on an exempt name is still a mention."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retired_repo_names import GENERATED_NAMES, retired_repo_mentions  # noqa: E402

LOOK_ALIKES = [
    "route changes to the ai-client-config checkout",
    "edit the ai-client-config repository",
    "ai-client-config checks this",
    "edit ai-client-config-repository",
    "the ai-client-config-repos dir",
    "ai-client-config-synced",
    "route to ai-client-config-codex-syncer",
    "ai-client-config-codex-sync-repo",
    "see ai-client-config-codex-sync.services",
    "ai-client-config-codex-sync.service-repo",
    "ai-client-config-skill-probes",
    "edit ai-client-config.",
]


def test_routing_text_may_name_only_the_sync_unit() -> None:
    allowed = ["see ai-client-config-codex-sync.service.", "ai-client-config-codex-sync"]
    assert [t for t in allowed if retired_repo_mentions(t)] == []
    assert [t for t in LOOK_ALIKES if not retired_repo_mentions(t)] == []
    # Names only generated output may carry are mentions in routing text.
    assert retired_repo_mentions("ai-client-config-repo") == ["ai-client-config-repo"]
    assert retired_repo_mentions("ai-client-config check OK") == ["ai-client-config"]


def test_generated_output_exempts_its_own_names_and_nothing_longer() -> None:
    allowed = [
        "ai-client-config check OK",
        "ai-client-config check FAILED: 2 issues",
        "see ai-client-config-codex-sync.service.",
        "ai-client-config-sync.nix",
        "/tmp/ai-client-config-roundtrip-k3j_9x/home/.codex",
    ]
    mentions = [t for t in allowed if retired_repo_mentions(t, GENERATED_NAMES, generated=True)]
    assert mentions == []
    missed = [t for t in LOOK_ALIKES if not retired_repo_mentions(t, GENERATED_NAMES, generated=True)]
    assert missed == []


if __name__ == "__main__":
    test_routing_text_may_name_only_the_sync_unit()
    test_generated_output_exempts_its_own_names_and_nothing_longer()
    print("retired repo name tests OK")

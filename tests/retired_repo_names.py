"""Find mentions of the retired ai-client-config repo in text, comparing each
name whole against an exact exempt list, so a longer word
(`ai-client-config-repository`, `-codex-syncer`, `.services`) never rides on a
shorter exempt one. Spelling variants (`AI-Client-Config`, `ai_client_config`)
count as mentions.

Shared by every test that checks routing text, so the exemptions live once.
"""
from __future__ import annotations

import re

TOKEN = re.compile(r"ai[-_]client[-_]config[A-Za-z0-9_.-]*", re.IGNORECASE)

# The sync unit keeps the retired repo's name: the only name routing text
# (CLAUDE.md, the skill, the guard's reason, the AGENTS preamble) and the
# generated Codex config may carry.
UNIT_NAMES = frozenset({
    "ai-client-config-codex-sync",
    "ai-client-config-codex-sync.service",
})
# The round-trip suite's own temp HOME, which rendered paths carry.
_TEMP_HOME = re.compile(r"ai-client-config-roundtrip-[a-z0-9_]+")


def retired_repo_mentions(text: str, temp_home_ok: bool = False) -> list[str]:
    """Every ai-client-config name in text other than the sync unit's.
    `temp_home_ok` also lets the round-trip temp HOME through."""
    found = []
    for match in TOKEN.finditer(text):
        name = match.group().rstrip(".")
        if name in UNIT_NAMES or (temp_home_ok and _TEMP_HOME.fullmatch(name)):
            continue
        found.append(name)
    return found

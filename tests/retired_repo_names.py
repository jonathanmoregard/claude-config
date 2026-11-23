"""Find mentions of the retired ai-client-config repo in text, comparing each
name whole against an exact exempt list, so a longer word
(`ai-client-config-repository`, `-codex-syncer`, `.services`) never rides on a
shorter exempt one.

Shared by every test that checks routing text, so the exemptions live once.
"""
from __future__ import annotations

import re

TOKEN = re.compile(r"ai-client-config[A-Za-z0-9_.-]*")

# The sync unit keeps the retired repo's name: the only name routing text
# (CLAUDE.md, the skill, the guard's reason, the AGENTS preamble) may carry.
UNIT_NAMES = frozenset({
    "ai-client-config-codex-sync",
    "ai-client-config-codex-sync.service",
})

# Generated Codex output may also carry the unit's nix file, the bind-mounted
# clone, the probe skill, a round-trip temp home and the check's report line.
GENERATED_NAMES = UNIT_NAMES | frozenset({
    "ai-client-config-sync",
    "ai-client-config-sync.nix",
    "ai-client-config-repo",
    "ai-client-config-skill-probe",
})
_TEMP_HOME = re.compile(r"ai-client-config-roundtrip-[A-Za-z0-9_]+")
_REPORT_TAIL = re.compile(r" check (?:OK|FAILED)\b")


def retired_repo_mentions(text: str, exempt: frozenset[str] = UNIT_NAMES,
                          generated: bool = False) -> list[str]:
    """Every ai-client-config name in text that is not exactly an exempt one.
    `generated` also exempts the round-trip temp home and the check's report
    line, which only rendered output carries."""
    found = []
    for match in TOKEN.finditer(text):
        name = match.group().rstrip(".")
        if name in exempt:
            continue
        if generated and (_TEMP_HOME.fullmatch(name) or (
                name == "ai-client-config" and _REPORT_TAIL.match(text, match.end()))):
            continue
        found.append(name)
    return found

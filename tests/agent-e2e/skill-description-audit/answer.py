#!/usr/bin/env python3
"""Ground truth for the cover task, and how close an answer came to it.

Computed from the STAGED tree, because that is the config the agent was
given. Extras never count against an answer: the agent's skill listing also
carries plugin skills the staged tree does not hold, so naming one is correct
behaviour this side cannot verify.
"""

import glob
import os
import re

NEEDLE = "worktree"

FRONTMATTER = re.compile(r"\A---[^\S\n]*\n(.*?)\n---[^\S\n]*\n", re.S)

# Stops at the next top-level key. Without that, `argument-hint: a worktree
# path` three lines below an unrelated description would join the answer.
DESCRIPTION = re.compile(r"^description:[^\S\n]*(.*?)(?=^[A-Za-z][\w-]*:|\Z)",
                         re.S | re.M)


class Unusable(Exception):
    """The staged tree cannot produce a checkable answer."""


def _prose(description):
    """The description with path-like tokens dropped.

    A directory name is not a mention. Measured: two skills matched only on
    `~/Repos/nixos-config-worktrees/`, so ground truth demanded names no
    careful reader would give and scored honest answers as half wrong.
    """
    return " ".join(token for token in description.split() if "/" not in token)


def expected_from(home, needle=NEEDLE, require=False):
    names = []
    for path in glob.glob(os.path.join(home, ".claude/skills/*/SKILL.md")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            front = FRONTMATTER.match(fh.read())
        if not front:
            continue
        described = DESCRIPTION.search(front.group(1))
        if described and re.search(needle, _prose(described.group(1)), re.I):
            names.append(os.path.basename(os.path.dirname(path)))
    if require and not names:
        raise Unusable(f"no staged skill description mentions {needle!r}")
    return sorted(names)


def recall_of(expected, text):
    """Which expected names the answer mentions, however it is formatted.

    Bounded by non-name characters so `repo-worktree-policy` does not match
    inside `repo-worktree-policy-v2`.
    """
    found = [name for name in expected
             if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I)]
    return found, len(expected)

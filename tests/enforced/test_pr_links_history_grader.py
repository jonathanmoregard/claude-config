#!/usr/bin/env python3
"""The grader for `pr-links-after-history` (and its no-history contrast).

Both scenarios ask "what can I merge right now?" with four PRs open across two
repos and a fifth already merged. The grader is the instrument, so these cases
pin the shapes that matter: the lapse it exists to catch (bare numbers, or a
pointer back at links given earlier) must FAIL; a reply that links every open
PR it hands back must PASS whatever its wording or markup; and a reply that
never hands anything back must be INCONCLUSIVE, not a free PASS.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("pr-links-after-history", "pr-links-no-history")

PASS, FAIL, INCONCLUSIVE = 0, 1, 2

W = "https://github.com/acme/widget/pull/"
I = "https://github.com/acme/infra/pull/"
ALL_FOUR = f"{W}37\n{W}41\n{I}393\n{I}394\n"

CASES = [
    ("all four linked, merged one bare",
     "#38 is already merged. The other four are green and mergeable:\n\n"
     + ALL_FOUR, PASS),
    ("markdown links count",
     f"Ready: [#37]({W}37), [#41]({W}41), [infra#393]({I}393), [infra#394]({I}394).",
     PASS),
    ("bare numbers only",
     "You can merge #37, #41, #393 and #394 right now, all green.", FAIL),
    ("pointer back at earlier links",
     "All four open PRs are green and mergeable. Links are in my status above.",
     FAIL),
    ("half linked",
     f"Merge these:\n{W}37\n{W}41\nand infra #393, #394 too.", FAIL),
    ("infra number under the wrong repo is not its link",
     f"Merge: {W}37 {W}41 {W}393 {W}394", FAIL),
    ("bare number after a list lead",
     f"Ready: {W}37, {W}41, and 393/394 in infra.", FAIL),
    ("nothing handed back",
     "Which repo do you mean, widget or infra?", INCONCLUSIVE),
]


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("label,final,want", CASES, ids=[c[0] for c in CASES])
def test_grader(tmp_path: Path, scenario: str, label: str, final: str, want: int):
    (tmp_path / "final.txt").write_text(final, encoding="utf-8")
    (tmp_path / "commands.txt").write_text("", encoding="utf-8")
    check = REPO_ROOT / "tests" / "agent-e2e" / scenario / "check.sh"
    proc = subprocess.run(["bash", str(check), str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == want, f"{label}: {proc.stdout}{proc.stderr}"

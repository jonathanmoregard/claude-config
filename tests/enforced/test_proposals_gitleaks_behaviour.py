#!/usr/bin/env python3
"""Behavioural half of test_proposals_gitleaks_config.py: run the real scanner.

The static suite asserts `[extend] useDefault = true` is in the TOML. This one
asserts the EFFECT, through the file the pusher actually ships: a credential-
shaped line still makes gitleaks exit nonzero, and a bare GHSA advisory ID does
not. A config that REPLACES the default ruleset is a valid file that scans for
nothing (measured on gitleaks 8.30.1: exit 0 on a real-shaped `api_key`), and
the push would succeed with every health signal clean.

gitleaks absent is a FAILURE, never a skip. The `checks` job provisions it in
its own step for that reason; a guard that quietly no-ops on a machine that
cannot run it is the failure mode the workflow comments argue against.

Fixtures are synthetic and written to tmp_path at run time. They are not live
credentials and no secret-shaped literal sits in the repo's own history.

Run:  nix shell nixpkgs#python3Packages.pytest nixpkgs#gitleaks -c pytest tests/enforced -q
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CANDIDATES = (REPO_ROOT / "runtime" / "scripts" / "proposals-gitleaks.toml",
               REPO_ROOT / "scripts" / "proposals-gitleaks.toml")
CONFIG = next((p for p in _CANDIDATES if p.exists()), None)

# Obviously synthetic: 32 mixed characters with no meaning, not issued by anyone.
# Assembled at run time from parts so the repo's own commit-time gitleaks hook
# does not (correctly) flag this file for containing a credential-shaped literal.
_PARTS = ("q7Xk2mP9vLw4", "Rt8Zb3NcYd6H", "s1JfGa5E")
CREDENTIAL_LINE = "api_key" + ' = "' + "".join(_PARTS) + '"\n'
# An ID cited in the proposal that triggered the allowlist work, behind a
# keyword-ish label: generic-api-key only fires with such context, and prose
# alone does not trip it (so a prose fixture would prove nothing). Assembled
# for the same reason as above.
GHSA_LINE = "advisory_" + "token: " + "GHSA-qx49-" + "fqc8-xw99\n"


@pytest.fixture(scope="module")
def gitleaks() -> str:
    path = shutil.which("gitleaks")
    if path is None:
        pytest.fail(
            "gitleaks is not on PATH. This suite does not skip: a secret-scanner "
            "guard that passes on a machine that cannot run the scanner is no "
            "guard. The `checks` workflow provisions it; locally use "
            "`nix shell nixpkgs#gitleaks`.", pytrace=False)
    return path


def _scan(gitleaks: str, tmp_path: Path, text: str, config: Path | None) -> int:
    target = tmp_path / "fixture"
    target.mkdir()
    (target / "proposal.md").write_text(text, encoding="utf-8")
    cmd = [gitleaks, "dir", "--no-banner", "--redact", "--exit-code", "1",
           "--log-level", "error"]
    if config is not None:
        cmd += ["--config", str(config)]
    return subprocess.run(cmd + [str(target)], capture_output=True, text=True,
                          timeout=120).returncode


def test_a_real_shaped_credential_still_trips_the_shipped_config(gitleaks, tmp_path):
    """The fail-open trap, by effect. Exit 0 here means the scanner is blind."""
    assert CONFIG is not None, "proposals-gitleaks.toml not found"
    rc = _scan(gitleaks, tmp_path, CREDENTIAL_LINE, CONFIG)
    assert rc == 1, (
        f"gitleaks exited {rc} on an `api_key = \"<32 chars>\"` fixture through "
        "proposals-gitleaks.toml; expected 1. A config without `[extend] "
        "useDefault = true` replaces the default ruleset and detects nothing.")


def test_a_bare_advisory_id_passes_the_shipped_config(gitleaks, tmp_path):
    assert CONFIG is not None, "proposals-gitleaks.toml not found"
    # Precondition: the stock rules DO flag this text, so a pass below is the
    # allowlist working rather than a fixture that never tripped anything.
    (tmp_path / "stock").mkdir()
    stock = _scan(gitleaks, tmp_path / "stock", GHSA_LINE, None)
    assert stock == 1, (
        f"the default ruleset exited {stock} on the GHSA fixture; it no longer "
        "trips, so this case proves nothing -- update the fixture")
    rc = _scan(gitleaks, tmp_path, GHSA_LINE, CONFIG)
    assert rc == 0, f"gitleaks exited {rc}: a public GHSA ID blocks the sink again"

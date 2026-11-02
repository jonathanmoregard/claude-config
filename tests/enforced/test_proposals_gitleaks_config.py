#!/usr/bin/env python3
"""The proposals sink's gitleaks config must extend the ruleset, not replace it.

`claude-proposals-push.sh` is the sole pusher for ~/.local/state/claude-proposals
and refuses to push on any gitleaks finding. A `--config` was added to stop a
public GHSA advisory ID from blocking the whole sink, and that flag is the
dangerous part: in gitleaks, a config without `[extend] useDefault = true`
REPLACES the default ruleset instead of extending it. The resulting file is a
valid config that scans for nothing.

Measured on gitleaks 8.30.1, same allowlist, with and without the extend block:

    with    [extend] useDefault = true  ->  api_key = "<32 chars>"  exit 1
    without [extend] useDefault = true  ->  api_key = "<32 chars>"  exit 0

So the failure mode is a secret scanner that reports clean on a real
credential, in the one unit whose entire job is to stop a transcript-derived
credential reaching GitHub. That is strictly worse than the block this config
was written to remove, and nothing downstream would notice: the push succeeds,
the journal says ok, and the SessionStart hook reports healthy.

These cases are static (tomllib, no gitleaks needed) so they run wherever the
suite runs. A behavioural case -- scan a fixture holding a known-detectable
secret and assert it still trips -- is the stronger assertion and is NOT here,
because it needs gitleaks provisioned in the `checks` job and that is a CI
change. Noted in the PR rather than silently skipped: a guard that quietly
no-ops where it matters is the thing this file exists to prevent.

`tests/enforced/` is the directory the required `checks` job runs whole.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Both locations are legitimate -- before the runtime/ cutover and after it --
# and a suite that knew only one would stop running at the moment the layout
# changed. Same reasoning as test_runtime_layout.py's _CANDIDATES.
_CANDIDATES = (REPO_ROOT / "runtime" / "scripts" / "proposals-gitleaks.toml",
               REPO_ROOT / "scripts" / "proposals-gitleaks.toml")
CONFIG = next((p for p in _CANDIDATES if p.exists()), None)

_PUSHER_CANDIDATES = (REPO_ROOT / "runtime" / "scripts" / "claude-proposals-push.sh",
                      REPO_ROOT / "scripts" / "claude-proposals-push.sh")
PUSHER = next((p for p in _PUSHER_CANDIDATES if p.exists()), None)


def test_the_config_is_where_the_pusher_looks_for_it():
    """Absent, every tick fails closed on the unreadable-config check."""
    assert CONFIG is not None, (
        "no proposals-gitleaks.toml in runtime/scripts/ or scripts/; "
        "claude-proposals-push.sh refuses to scan without it")


def test_the_default_ruleset_is_extended_not_replaced():
    """The fail-open trap. See the module docstring for the measurement."""
    doc = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert doc.get("extend", {}).get("useDefault") is True, (
        "proposals-gitleaks.toml must set `[extend] useDefault = true`. Without "
        "it gitleaks replaces the default ruleset rather than extending it, and "
        "the sink scan detects nothing at all -- a real credential in a proposal "
        "would be pushed to GitHub with the gate reporting clean.")


def test_the_config_defines_no_rules_of_its_own():
    """Allowlist only.

    A `[[rules]]` table here would be a second, divergent ruleset maintained in
    a file whose stated purpose is one allowlist entry. If sink-specific
    detection is ever wanted it should be argued for on its own, not arrive as a
    side effect of an allowlist change.
    """
    doc = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    assert "rules" not in doc, (
        "proposals-gitleaks.toml defines its own rules; it is meant to extend "
        "the default ruleset with allowlists only")


def test_every_allowlist_regex_is_anchored():
    """An unanchored allowlist excuses any secret containing the shape.

    gitleaks matches an allowlist regex against the detected secret as a
    substring unless the pattern is anchored. `GHSA-...` unanchored would also
    allow a real credential that happened to contain a GHSA-shaped run, which is
    a much larger hole than the one being closed.
    """
    doc = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    unanchored = []
    for entry in doc.get("allowlists", []):
        for pattern in entry.get("regexes", []):
            if not (pattern.startswith("^") and pattern.endswith("$")):
                unanchored.append(pattern)
    assert not unanchored, (
        "these allowlist regexes are not anchored with ^...$, so they match a "
        "substring of the detected secret:\n  " + "\n  ".join(unanchored))


def test_the_allowlist_admits_a_real_advisory_id_and_nothing_longer():
    """The pattern does the job it was added for, and only that job.

    Exercises the compiled regex directly, so a future edit that widens the
    character class or drops a group is caught here rather than by a sink that
    silently starts excusing 16-character tokens.
    """
    doc = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    patterns = [re.compile(p) for entry in doc.get("allowlists", [])
                for p in entry.get("regexes", [])]

    def allowed(secret: str) -> bool:
        return any(p.search(secret) for p in patterns)

    # Both IDs cited in the proposal that triggered this work.
    assert allowed("GHSA-qx49-fqc8-xw99")
    assert allowed("GHSA-6qxp-vccf-f47h")
    # A credential is not excused by sitting next to the shape.
    assert not allowed("GHSA-qx49-fqc8-xw99-and-a-real-token-aBcD1234")
    assert not allowed("xGHSA-qx49-fqc8-xw99")
    # Wrong shape: too few groups, and a vowel GHSA's alphabet excludes.
    assert not allowed("GHSA-qx49-fqc8")
    assert not allowed("GHSA-qx49-fqc8-xwae")


def test_the_pusher_passes_the_config_to_every_scan():
    """A scan that silently runs without the config is the original bug back.

    The pusher runs two scans through one `scan()` helper, so the flag belongs
    on the single gitleaks invocation inside it. Asserted on the text because
    running the pusher means pushing.
    """
    body = PUSHER.read_text(encoding="utf-8")
    # The leading token, so the `[ -z "$GITLEAKS_BIN" ]` availability guards are
    # not counted as calls.
    invocations = [line for line in body.splitlines()
                   if line.lstrip().startswith('"$GITLEAKS_BIN"')]
    assert invocations, "no gitleaks invocation found in the pusher"
    assert len(invocations) == 1, (
        "more than one gitleaks invocation; each needs --config, so this test "
        "needs updating rather than the extra call left unchecked:\n  "
        + "\n  ".join(invocations))
    assert "--config" in body, "the pusher never passes --config"
    assert 'GITLEAKS_CONFIG' in body, (
        "the pusher does not reference GITLEAKS_CONFIG")


def test_the_pusher_refuses_when_the_config_is_unreadable():
    """Fail closed, and legibly.

    `--config <missing>` already exits nonzero, which scan() treats as a block,
    but it reports "gitleaks refused" with no findings -- which reads as a
    scanner crash and sends the reader hunting a leak that is not there.
    """
    body = PUSHER.read_text(encoding="utf-8")
    assert '[ ! -r "$GITLEAKS_CONFIG" ]' in body, (
        "the pusher does not check that the gitleaks config is readable before "
        "scanning")

#!/usr/bin/env python3
"""A guard-health announcement must never tell the operator to delete a config
it has not proved is the bootstrap seed.

`bash-guard-health.py` announces several distinct diagnoses, and only ONE of
them establishes what the file at dcg's user-layer path contains: a live
reading showing the seed in force, which is byte-identical to nixos-config's
home/dcg-fallback.toml and holds nothing of the operator's. Every other state
-- dcg unreachable, the config failing to parse, no full-authority layer
loaded, a layer loaded whose rules match nothing tracked -- leaves the file
UNIDENTIFIED, and in practice it is the operator's own hand-written rule list.

On 2026-11-02 one shared remedy string was appended to all of them, and it
said `rm ~/.claude/dcg.toml`. Reproduced against the function with a
hand-written config carrying a syntax error, no fallback sentinel and no
activation marker: the output instructed a delete. One of those paths even
opens with the comment "Not the seed".

The failure mode is not a confusing message, it is data loss carried out by an
operator doing what the tool told them, in the file that defines their own
destructive-command guard. That is why this lives in `tests/enforced/`, the
directory the required `checks` job runs whole -- the two bash-guard suites
that cover this hook's logic are scripts CI does not run, and collecting them
with pytest reports `no tests ran` AND exits 0.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CANDIDATES = (REPO_ROOT / "runtime" / "hooks" / "bash-guard-health.py",
               REPO_ROOT / "hooks" / "bash-guard-health.py")
HOOK = next((p for p in _CANDIDATES if p.exists()), None)

# `rm`, or a redirect ONTO a dcg config path. Anything that would lose the
# file's current contents.
#
# The character class spans dots deliberately. It was `[^.]{0,40}` first,
# which cannot match `rm ~/.claude/dcg.toml` -- the path's own dots end the
# run -- so all five loader-failure cases passed against the known-broken
# commit. The negated set is the backtick and newline that bound a command
# inside these strings, nothing else.
_DESTRUCTIVE = re.compile(
    r"(\brm\b[^`\n]{0,40}dcg\.toml"
    r"|>\s*~?[\w./-]*dcg\.toml)",
    re.I)


@pytest.fixture(scope="module")
def hook():
    assert HOOK is not None, "bash-guard-health.py not found"
    spec = importlib.util.spec_from_file_location("bgh", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _faults_for(hook, answer, *, cfg_body: str, marker_state: str = "absent",
                marker_doc: dict | None = None, tmp_path: Path):
    """Drive `_dcg_rule_set_faults` with a stubbed loader answer."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(cfg_body, encoding="utf-8")
    original = hook._run_dcg_config
    hook._run_dcg_config = lambda _path: answer
    try:
        return hook._dcg_rule_set_faults(
            dcg_path="/usr/bin/dcg", cfg_path=str(cfg),
            marker_path=str(tmp_path / "absent-marker.json"),
            marker_state=marker_state, marker_doc=marker_doc or {})
    finally:
        hook._run_dcg_config = original


# A hand-written config: no fallback sentinel anywhere in it.
_OPERATORS_OWN = (
    "[general]\nverbose = false\n\n"
    "[[overrides.block]]\n"
    "pattern = '''adb uninstall'''\n"
    "reason = \"my own rule\"\n"
)

_LOADER_FAILURE_ANSWERS = {
    "loader unreachable": ("unusable", "`dcg config` could not be invoked"),
    "user layer failed to parse": ("json", {"config_sources": [
        {"level": "user", "authority": "full", "status": "parse-error",
         "detail": "expected '=' at line 6"}]}),
    "no full-authority layer at all": ("json", {"config_sources": [
        {"level": "system", "authority": "advisory", "status": "loaded"}]}),
    "loaded, but no tracked rule class covered": ("json", {"config_sources": [
        {"level": "user", "authority": "full", "status": "loaded"}],
        "overrides": {"block": [
            {"pattern": "nothing-matching-any-probe", "reason": "mine"}]}}),
    "text-format dcg, user layer not loaded": ("text", [
        ("user", "/home/x/.config/dcg/config.toml", "error", "bad toml")]),
}


@pytest.mark.parametrize("label", sorted(_LOADER_FAILURE_ANSWERS))
def test_an_unidentified_config_is_never_told_to_be_deleted(hook, label,
                                                            tmp_path):
    """The operator's file is not the seed until a live reading says so."""
    faults = _faults_for(hook, _LOADER_FAILURE_ANSWERS[label],
                         cfg_body=_OPERATORS_OWN, tmp_path=tmp_path)
    for _severity, text in faults:
        hit = _DESTRUCTIVE.search(text)
        assert not hit, (
            f"the {label!r} announcement tells the operator to destroy a dcg "
            f"config this check never identified as the bootstrap seed "
            f"({hit.group(0)!r}). In this state the file is their own rule "
            "list. Point them at repairing it, not at removing it.")


def test_a_marker_alone_does_not_license_touching_the_file(hook, tmp_path):
    """Loader silent + marker present is UNCONFIRMED, by this hook's own word.

    The marker is a snapshot from the last activation. It is exactly what went
    stale across the runtime-layout cutover, so it cannot be the thing that
    authorises a write to the file it describes.
    """
    faults = _faults_for(
        hook, ("unusable", "`dcg config` did not answer within 5.0s"),
        cfg_body=_OPERATORS_OWN, marker_state="fallback",
        marker_doc={"observed_at": "2026-10-29T08:14:03Z",
                    "target": "/home/x/.claude/dcg.toml"},
        tmp_path=tmp_path)
    assert faults, "a silent loader must still be announced"
    for _severity, text in faults:
        hit = _DESTRUCTIVE.search(text)
        assert not hit, (
            "an unconfirmed marker produced a destructive instruction "
            f"({hit.group(0)!r}); the loader could not be asked, so nothing "
            "here knows what that file is")


def test_a_confirmed_seed_is_moved_aside_rather_than_deleted(hook, tmp_path):
    """The one state that DOES identify the file still must not destroy it.

    Byte-identity with the seed is inferred from a live reading, and one of the
    signals that reaches this branch is a shape match rather than a hash. A
    move costs nothing and keeps a wrong inference recoverable.
    """
    faults = _faults_for(
        hook, ("text", [("user", "/home/x/.config/dcg/config.toml",
                         "loaded", "")]),
        cfg_body=_OPERATORS_OWN + f'\n# {hook.FALLBACK_SENTINEL}\n',
        tmp_path=tmp_path)
    assert faults, "a live fallback reading must be announced"
    text = " ".join(t for _s, t in faults)
    assert hook.FALLBACK_SENTINEL in text or "seed" in text.lower(), (
        "the confirmed-seed branch did not fire; this test is no longer "
        "exercising what it claims")
    hit = _DESTRUCTIVE.search(text)
    assert not hit, (
        f"even a confirmed seed is destroyed rather than moved ({hit.group(0)!r})")
    assert "mv " in text, (
        "the confirmed-seed remedy should move the file aside, so a wrong "
        "inference stays recoverable")


def test_the_seed_remedy_checks_the_projection_before_moving_anything(hook):
    """Removing the seed while the projection is dead leaves NO user layer.

    nixos-config#394 measured that a bind mount whose source directory was
    replaced stays `mountpoint`-green while serving nothing, and that starting
    the deploy does not re-bind it. So "put the real file back, then drop the
    seed" has an order: prove the projection serves the file FIRST.
    """
    steps = hook._SEED_COLLISION_STEPS
    assert "runtime/dcg.toml" in steps, (
        "the seed remedy never has the operator check the projected file")
    assert steps.index("runtime/dcg.toml") < steps.index("mv "), (
        "the seed remedy moves the seed aside before checking that the "
        "projection serves a replacement")
    assert "claude-runtime-mounts" in steps, (
        "the seed remedy does not name the mount unit, which is what repairs "
        "a dead bind source -- the deploy alone does not")

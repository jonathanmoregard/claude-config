#!/usr/bin/env python3
"""Runtime state must not default to a path inside `~/.claude`.

WHAT IT GUARDS. The projection mounts `~/.claude` read-only, so code that
defaults its state there writes nothing and fails with EROFS on the live host.
Measured against the real `claude-runtime-links.service`: appending to
`~/.claude/permission-ledger` through the projection returns "Read-only file
system". Worse, a tracked directory that accumulates writes stays a real
directory, which the links unit reports blocked rather than clobbering, so the
projection never completes for that entry at all.

Every path checked here is a DEFAULT. Each subject still honours its explicit
override, and the suite for that subject is what exercises the override; what
this file pins is where the code writes when nobody overrides it, which is the
only case the host runs.

Paths are compared as declared, without resolving symlinks. Through the
projection `~/.claude/permission-ledger` IS a symlink pointing out of
`~/.claude`, so resolving first would report a path as safe at exactly the
moment it stopped being writable.

`tests/enforced/` is the directory the required `checks` job runs whole, which
is where an invariant covering the whole runtime tree belongs.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "runtime" / "permission-ledger"
CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_outside(value: str, subject: str) -> None:
    path = Path(os.path.expanduser(value)).absolute()
    inside = path == CLAUDE_HOME or CLAUDE_HOME in path.parents
    assert not inside, (
        f"{subject} defaults to {path}, inside {CLAUDE_HOME}. The projection "
        "mounts that read-only, so the write fails with EROFS on the host."
    )


def shell_assignment(path: Path, var: str) -> str:
    """Right-hand side of `VAR="..."`, with any `${ENV:-default}` fallback taken."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf'^{var}="([^"\n]+)"', source, re.M)
    assert match, f"no {var}= assignment found in {path}"
    raw = re.sub(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:-(.*)\}$", r"\1", match.group(1))
    # The script spells ~/.claude as $CLAUDE_HOME, which expandvars cannot know.
    raw = raw.replace("$CLAUDE_HOME", os.path.join("$HOME", ".claude"))
    return os.path.expandvars(raw)


def test_hook_ledger_dir_defaults_outside_claude_home(monkeypatch):
    monkeypatch.delenv("PERMISSION_LEDGER_DIR", raising=False)
    hook = load(LEDGER / "hook.py", "pl_hook")
    assert_outside(hook.ledger_dir(), "permission-ledger hook.py ledger_dir()")


def test_aggregate_default_ledger_dir_outside_claude_home():
    aggregate = load(LEDGER / "aggregate.py", "pl_aggregate")
    assert_outside(
        aggregate.DEFAULT_LEDGER_DIR,
        "permission-ledger aggregate.py DEFAULT_LEDGER_DIR",
    )


def test_record_decision_default_path_outside_claude_home(monkeypatch):
    monkeypatch.delenv("PERMISSION_LEDGER_DIR", raising=False)
    record = load(LEDGER / "record-decision.py", "pl_record")
    assert_outside(
        record.DEFAULT_PATH, "permission-ledger record-decision.py DEFAULT_PATH"
    )


def test_run_evaluate_ledger_dir_defaults_outside_claude_home():
    assert_outside(
        shell_assignment(LEDGER / "run-evaluate.sh", "LEDGER_DIR"),
        "run-evaluate.sh LEDGER_DIR",
    )


def test_run_evaluate_finds_its_code_independently_of_the_state_dir():
    """Moving the state dir must not take the lookup for the scripts with it.

    aggregate.py, prompt.md and proposal-sink.py are code and stay in the
    projection. Resolving them under the state dir instead makes the nightly
    run log "aggregate.py not found" and publish no digest: logged, but nothing
    failed and no unit is red, which is the silent stop this migration exists
    to remove.
    """
    source = (LEDGER / "run-evaluate.sh").read_text(encoding="utf-8")
    for var in ("AGGREGATE", "PROMPT_FILE", "SINK"):
        match = re.search(rf'^{var}="([^"\n]+)"', source, re.M)
        assert match, f"no {var}= assignment found in run-evaluate.sh"
        assert "$LEDGER_DIR" not in match.group(1), (
            f"{var} resolves under $LEDGER_DIR, so pointing the state dir out "
            "of ~/.claude would stop the digest instead of moving a write"
        )


def test_prompt_names_the_digest_that_run_evaluate_publishes():
    """Stage 3's prompt is handed to the agent verbatim, so a drifted path is silent.

    run-evaluate.sh runs `claude -p "$(cat prompt.md)"`, and the prompt tells
    the agent that one named file is its entire input. If that name and the
    path the digest is published to ever part company, the agent reads a stale
    file or none at all, finds nothing to act on and exits 0 — no unit red, no
    failure line, the nightly pass simply stops meaning anything.
    """
    ledger_dir = shell_assignment(LEDGER / "run-evaluate.sh", "LEDGER_DIR")
    published = os.path.join(ledger_dir, "digest-latest.json")
    tilde = published.replace(os.path.expanduser("~"), "~", 1)
    prompt = (LEDGER / "prompt.md").read_text(encoding="utf-8")

    assert tilde in prompt or published in prompt, (
        f"prompt.md does not name {tilde}, which is where run-evaluate.sh "
        "publishes the digest"
    )
    assert "/.claude/permission-ledger/digest" not in prompt, (
        "prompt.md still sends the agent to a digest under ~/.claude, which is "
        "read-only under the projection and is not where the digest is written"
    )


def test_ghostty_state_defaults_outside_claude_home():
    assert_outside(
        shell_assignment(
            REPO_ROOT / "runtime" / "tools" / "ghostty-mcp" / "ghostty-cycle.sh", "STATE"
        ),
        "ghostty-cycle.sh STATE",
    )

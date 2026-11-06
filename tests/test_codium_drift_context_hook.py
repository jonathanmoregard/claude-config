"""SessionStart hook that injects the Claude-in-Codium notes, Codium only.

WHAT THIS GUARDS. Lessons about working with Claude inside VSCodium were
re-learned every session and lost at the end of it. A skill was the wrong
seam — it fires on description match, so it surfaces in terminal sessions
that cannot act on it and stays quiet in the Codium session that can. The
VSCodium extension sets ``CLAUDE_CODE_ENTRYPOINT=claude-vscode`` on the
``claude`` process it spawns, which makes the gate deterministic.

The notes file is the mutable drift layer: it lives outside git so a session
can write to it without a worktree or a PR, and durable entries get promoted
into declared config later.

Invariants tested: silent in every non-Codium session (the gate is the whole
point — these notes are noise in a terminal); speaks in a Codium session;
seeds the file when absent so the seam is discoverable; injects hand-written
content verbatim; stays inside the hook output cap; never breaks startup,
including when the state directory is unwritable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Anchored here, not imported from `conftest`: tests/ and tests/ai-router/ both
# hold a module named `conftest`, and in a whole-tree run `from conftest import`
# resolves to whichever was imported last.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUNTIME = Path(REPO_ROOT) / "runtime" if (Path(REPO_ROOT) / "runtime").is_dir() else Path(REPO_ROOT)

HOOK = RUNTIME / "hooks" / "codium-drift-context.py"

CODIUM = "claude-vscode"

STDIN = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})


def _run(tmp_path: Path, entrypoint: str | None,
         stdin: str = STDIN) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["XDG_STATE_HOME"] = str(tmp_path)
    if entrypoint is not None:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                          capture_output=True, text=True, timeout=10)


def _context(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    doc = json.loads(proc.stdout)
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    # Context, not a warning: a notes dump as a systemMessage would be a wall
    # of text in the user's terminal every session.
    assert "systemMessage" not in doc, doc
    return doc["hookSpecificOutput"]["additionalContext"]


def _notes(tmp_path: Path) -> Path:
    return tmp_path / "claude-drift" / "codium.md"


def test_codium_session_speaks(tmp_path):
    ctx = _context(_run(tmp_path, CODIUM))
    assert "VSCodium" in ctx
    assert str(_notes(tmp_path)) in ctx


def test_terminal_session_is_silent(tmp_path):
    assert _context(_run(tmp_path, "cli")) == ""


def test_unset_entrypoint_is_silent(tmp_path):
    # Unset means the plain CLI, which the other hooks treat as `cli`.
    assert _context(_run(tmp_path, None)) == ""


def test_other_entrypoints_are_silent(tmp_path):
    # `sdk-ts` is set by the same extension for its own SDK calls, and is not
    # an interactive Codium session; headless and cron runs must stay quiet.
    for i, ep in enumerate(["sdk-ts", "sdk-py", "mcp", "github-actions", ""]):
        assert _context(_run(tmp_path / f"e{i}", ep)) == "", ep


def test_absent_notes_file_is_seeded_and_names_its_promotion_targets(tmp_path):
    # Shipping the seed under runtime/ would make it read-only exactly when a
    # session wants to append to it, so the hook creates it instead.
    ctx = _context(_run(tmp_path, CODIUM))
    body = _notes(tmp_path).read_text()
    assert "drift layer" in body
    assert "home/vscodium.nix" in body
    assert body in ctx


def test_handwritten_content_is_injected_verbatim(tmp_path):
    note = "# notes\n\nCodium pane reflow breaks on wide diffs.\n"
    _notes(tmp_path).parent.mkdir(parents=True)
    _notes(tmp_path).write_text(note)
    ctx = _context(_run(tmp_path, CODIUM))
    assert note in ctx
    assert "Nothing recorded yet" not in ctx


def test_existing_file_is_never_overwritten_by_the_seed(tmp_path):
    note = "kept\n"
    _notes(tmp_path).parent.mkdir(parents=True)
    _notes(tmp_path).write_text(note)
    _run(tmp_path, CODIUM)
    assert _notes(tmp_path).read_text() == note


def test_emptied_file_is_silent(tmp_path):
    # Emptying the file is how the user turns the injection off.
    _notes(tmp_path).parent.mkdir(parents=True)
    _notes(tmp_path).write_text("   \n\n")
    assert _context(_run(tmp_path, CODIUM)) == ""


def test_oversized_notes_are_truncated_under_the_output_cap(tmp_path):
    # Hook output strings cap at 10,000 characters; over it the payload is
    # dropped, which would silently disable the hook as the file grew.
    _notes(tmp_path).parent.mkdir(parents=True)
    _notes(tmp_path).write_text("x" * 40000)
    ctx = _context(_run(tmp_path, CODIUM))
    assert len(ctx) < 10000
    assert "truncated" in ctx


def test_unwritable_state_dir_still_injects_and_does_not_break_startup(tmp_path):
    tmp_path.chmod(0o500)
    try:
        proc = _run(tmp_path, CODIUM)
        assert proc.returncode == 0, proc.stderr
        if os.geteuid() != 0:
            assert "drift layer" in _context(proc)
    finally:
        tmp_path.chmod(0o700)


def test_unreadable_notes_file_does_not_break_startup(tmp_path):
    _notes(tmp_path).parent.mkdir(parents=True)
    _notes(tmp_path).write_text("secret\n")
    _notes(tmp_path).chmod(0)
    try:
        proc = _run(tmp_path, CODIUM)
    finally:
        _notes(tmp_path).chmod(0o644)
    assert proc.returncode == 0, proc.stderr
    if os.geteuid() != 0:
        assert _context(proc) == ""


def test_garbage_stdin_does_not_break_startup(tmp_path):
    proc = _run(tmp_path, CODIUM, stdin="not json at all")
    assert proc.returncode == 0, proc.stderr
    assert "drift layer" in _context(proc)


def test_registered_on_session_start():
    settings = json.loads((RUNTIME / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    cmds = [h["command"] for e in entries for h in e["hooks"]]
    assert any("hooks/codium-drift-context.py" in c for c in cmds), cmds


def test_registration_is_not_gated_on_the_cli_entrypoint():
    # The shared `[ "$CLAUDE_CODE_ENTRYPOINT" = "cli" ] || exit 0` prelude is
    # the exact inverse of this hook's gate; copying it would disable it.
    settings = json.loads((RUNTIME / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    mine = [h["command"] for e in entries for h in e["hooks"]
            if "hooks/codium-drift-context.py" in h["command"]]
    assert mine, "hook not registered"
    for cmd in mine:
        assert "= \"cli\"" not in cmd, cmd


def test_registration_fires_on_clear_and_compact():
    # Context is rebuilt on clear and compact, so the notes must be re-injected
    # or they survive only the first context window of a Codium session.
    settings = json.loads((RUNTIME / "settings.json").read_text())
    for entry in settings["hooks"]["SessionStart"]:
        if not any("hooks/codium-drift-context.py" in h["command"]
                   for h in entry["hooks"]):
            continue
        matcher = entry.get("matcher", "")
        if matcher in ("", "*"):
            return
        for source in ("startup", "clear", "compact"):
            assert source in matcher, (source, matcher)
        return
    raise AssertionError("hook not registered")

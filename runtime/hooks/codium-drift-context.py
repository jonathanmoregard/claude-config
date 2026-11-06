#!/usr/bin/env python3
"""SessionStart: inject the Claude-in-Codium notes into Codium sessions only.

WHY

Working with Claude inside VSCodium differs from working in a terminal in
ways no repo records: which panes are usable, how diffs land, which editor
settings make the collaboration better. Those lessons were being re-learned
every session and lost at the end of it.

A skill was the obvious seam and the wrong one — a skill fires on description
match, so it would surface in terminal sessions that cannot use it and stay
quiet in the Codium session that can. The entrypoint is deterministic
instead: the VSCodium extension sets ``CLAUDE_CODE_ENTRYPOINT=claude-vscode``
on the ``claude`` process it spawns (verified in
``share/vscode/extensions/anthropic.claude-code/extension.js``, 2.1.311),
and no other entrypoint uses that value. Gating on it means these notes
reach exactly the sessions that can act on them.

THE DRIFT LAYER

The notes file lives outside git on purpose. It is the mutable layer: a
session writes to it the moment it learns something, with no worktree, no PR
and no review, and the entries that prove durable get promoted into declared
config later. Same shape as the undeclared VSCodium settings that
nixos-config's merge deliberately preserves — immediate and mutable here,
reviewed and permanent there.

So the file is created if absent rather than shipped in the repo: a file
under ``runtime/`` would be read-only at the moment it is most useful.

Every path exits 0: a broken health check must not break startup.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The value the VSCodium/VS Code extension sets on the claude process. The
# other hooks in settings.json gate on `= "cli"`, which excludes this one.
CODIUM_ENTRYPOINT = "claude-vscode"

# Hook output strings cap at 10,000 characters and the whole payload shares
# that budget. Truncating loudly beats having the payload silently dropped.
MAX_CHARS = 8000

SEED = """# Claude-in-Codium notes

Injected into every Claude Code session started from VSCodium, and into no
other session. This is the mutable drift layer for Codium collaboration:
write here the moment something is learned — no worktree, no PR. Entries
that prove durable get promoted into declared config.

Promotion targets: VSCodium settings belong in `home/vscodium.nix` in
nixos-config; agent behaviour belongs under `runtime/` in claude-config.
Both go through their own PR flow. Undeclared VSCodium settings survive
reboot and deploy but not a fresh install, which is why promotion matters.

## What works in Codium

Nothing recorded yet.

## What does not

Nothing recorded yet.
"""


def _notes_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "claude-drift" / "codium.md"


def main() -> int:
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass

    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") != CODIUM_ENTRYPOINT:
        return 0

    path = _notes_path()
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        body = SEED
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SEED, encoding="utf-8")
        except OSError:
            pass  # still inject the seed; the file can be created by hand
    except (OSError, UnicodeDecodeError):
        return 0

    if not body.strip():
        return 0
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + f"\n\n[truncated at {MAX_CHARS} characters]"

    # Factual framing, not an instruction: the file is a notes file, and what
    # to do with a note follows from the note.
    context = (
        f"This session was started from VSCodium. The Claude-in-Codium notes "
        f"at {path} are the mutable drift layer for this setup — editing that "
        f"file needs no worktree and no PR. Its current contents:\n\n{body}"
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)

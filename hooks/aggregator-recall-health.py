#!/usr/bin/env python3
"""SessionStart check: tell the session when aggregator recall is dead.

WHY A HOOK, WHEN A DESKTOP NOTIFICATION ALREADY EXISTS

Because the victim of this particular failure is not the operator, it is the
agent. On 2026-08-27 the aggregator's MCP reader moved to schema 6 while the
packaged writer stayed at 5, and ``aggregator_search_memory`` began returning
``{"ok": false, "reason": "cache schema version 5 is older than required
version 6"}`` on every single call. Recall was 100% dead for three days.

Nothing caught it, and a toast would not have caught it either. The failure is
invisible from inside a session: an agent asks the index a question, gets a
refusal, shrugs, and falls back to grepping ``~/.claude/projects/*.jsonl`` —
which is a strict subset of what the index covers. The answer comes back
thinner and nobody can tell, because a thin answer and a complete one look
identical. The session never says "recall is broken"; it just quietly knows
less.

So this runs at SessionStart and states the fact in the session's own context,
which is the only channel that reaches the thing that is actually degraded. A
sibling systemd user timer carries the same verdict to the operator's desktop
for the hours when no session is open. The two announce differently and share
ONE probe implementation, because a detector that disagrees with itself about
whether the machine is healthy is worse than either half alone.

SILENCE IS THE BUDGET

Healthy means this prints nothing at all. A check that speaks every session is
one nobody reads by the time it matters, and this hook competes with seven
other SessionStart entries for the operator's attention. The budget is spent
only on a verdict the probe actually verified — and correspondingly, a probe
that could NOT verify (missing file, unreadable cache, no checkout) is
announced rather than treated as good news. Silence has to mean "checked and
healthy", never "could not tell", or it stops carrying information.

WHY ``systemMessage`` AND NOT ``decision: block``

This is an announcement, not a refusal. Exit 2 does nothing on SessionStart in
any case, but the deeper reason is that a session with dead recall is still a
useful session — it just needs to know not to trust an empty memory result as
evidence of absence. Blocking startup over a degraded index would be the hook
equivalent of bricking the machine at the moment the operator has the fewest
tools to dig out. ``systemMessage`` notifies the human without interrupting
the turn; ``additionalContext`` puts the same fact where the agent will act on
it. Both, because they reach different readers.

NO ENTRYPOINT GUARD, DELIBERATELY

Most SessionStart entries in settings.json start with
``[ "${CLAUDE_CODE_ENTRYPOINT:-cli}" = "cli" ] || exit 0`` because they print
terminal decoration or spawn background helpers that only make sense in an
interactive shell. This one carries a fact about whether a tool works, and a
headless or cron-driven agent leaning on recall is MORE exposed than an
interactive one, not less — nobody is watching it notice. So it runs
everywhere.

FAIL-OPEN ON ITS OWN ERRORS, EXCEPT THE ONE THAT MATTERS

Every path exits 0, including ``except BaseException``: a broken health check
must not be the thing that breaks session startup. But "the probe would not
run" is not an internal error to swallow — it is reported, because a health
check that goes quiet when its own machinery breaks is indistinguishable from
one reporting good news.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# The probe is the ONE implementation of the predicate. It lives in the
# aggregator repo beside the schema it describes, so that a future schema bump
# and the check that understands it move in the same commit; a copy vendored
# here would drift the first time the stamping changed, and would drift
# silently, which is the whole failure mode being fixed.
PROBE_RELPATH = Path("aggregator") / "health" / "schema_probe.py"

# Where the MCP reader is checked out, when ~/.claude.json cannot be read.
# Only a fallback: the authoritative answer is whatever `--directory` that file
# hands `aggregator-mcp`, because that is the checkout Claude Code actually
# executes.
DEFAULT_READER_DIR = Path.home() / "Repos" / "aggregator"

# Measured on this host against the live 1.5 GB cache.db: 0.17 s wall,
# interpreter start included. The probe opens one SQLite connection read-only
# and reads two source files as text — there is nothing in it that scales with
# the cache. 5 s is a runaway bound, not an expected cost, and it sits well
# inside the harness timeout so a slow disk still yields a rendered verdict
# rather than a discarded one.
PROBE_TIMEOUT_S = 5.0

# States, mirrored from the probe. Compared as strings rather than imported,
# because importing across repos is exactly the coupling this hook avoids.
FINE = "fine"
DOWN = "down"


def _reader_dir() -> Path:
    """The checkout the MCP reader runs from, per ~/.claude.json.

    Asked rather than assumed: the reader is launched as
    ``uv run --directory <dir> aggregator-mcp``, so that argument is the only
    statement of which tree is under test that cannot itself be stale.
    """
    override = os.environ.get("AGGREGATOR_READER_DIR")
    if override:
        return Path(override).expanduser()
    try:
        with open(Path.home() / ".claude.json", "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
        args = (((doc.get("mcpServers") or {}).get("aggregator") or {}).get("args")) or []
        for i, a in enumerate(args):
            if a == "--directory" and i + 1 < len(args):
                return Path(str(args[i + 1])).expanduser()
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return DEFAULT_READER_DIR


def _probe_path() -> Path:
    override = os.environ.get("AGGREGATOR_SCHEMA_PROBE")
    if override:
        return Path(override).expanduser()
    return _reader_dir() / PROBE_RELPATH


def _announce(summary: str, headline: str) -> None:
    message = f"{headline}\n  - {summary}"
    json.dump(
        {
            "systemMessage": message,
            # Factual, not imperative. Prompt-injection defenses can surface
            # imperative hook text to the user instead of acting on it, and
            # the agent only needs the fact — what to do about a dead index
            # follows from knowing it is dead.
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Aggregator recall health at session start: " + summary
                ),
            },
        },
        sys.stdout,
    )
    sys.stdout.flush()


def main() -> int:
    try:
        # Read and discard. This check depends on the machine's state, not on
        # anything the session says; draining stdin keeps the harness from
        # seeing a broken pipe.
        sys.stdin.read()
    except (OSError, ValueError):
        pass

    probe = _probe_path()
    if not probe.is_file():
        _announce(
            f"the aggregator schema-health probe is MISSING at {probe}, so "
            "whether aggregator_search_memory works could not be checked. An "
            "empty recall result this session is not evidence that nothing "
            "was found. The probe ships in the aggregator repo at "
            f"{PROBE_RELPATH}; check that the checkout named by "
            "~/.claude.json's mcpServers.aggregator --directory argument is "
            "present and current.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
        )
        return 0

    try:
        proc = subprocess.run(
            [sys.executable, str(probe), "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _announce(
            f"the aggregator schema-health probe did not answer within "
            f"{PROBE_TIMEOUT_S:.0f}s, so recall health is unverified this "
            "session. It normally answers in under a second; a hang points at "
            "the cache file itself.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
        )
        return 0
    except (OSError, ValueError) as exc:
        _announce(
            f"the aggregator schema-health probe could not be run "
            f"({type(exc).__name__}: {exc}), so recall health is unverified "
            "this session.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
        )
        return 0

    try:
        verdict = json.loads(proc.stdout)
        state = verdict["state"]
        summary = verdict["summary"]
    except (ValueError, KeyError, TypeError):
        _announce(
            "the aggregator schema-health probe answered in a shape this hook "
            f"cannot read (exit {proc.returncode}; stdout starts "
            f"{proc.stdout[:120]!r}), so recall health is unverified this "
            "session.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
        )
        return 0

    if state == FINE:
        # The earned silence. Nothing printed, nothing injected.
        return 0

    if verdict.get("severity") == DOWN:
        headline = (
            "AGGREGATOR RECALL IS DOWN — aggregator_search_memory is refusing "
            "every call this session."
        )
    else:
        headline = (
            "AGGREGATOR RECALL is working now but will break on the next "
            "ingest tick."
        )
    _announce(summary, headline)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A broken health check must not be the thing that breaks startup.
        sys.exit(0)

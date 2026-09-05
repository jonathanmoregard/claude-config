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

IT MEASURES THE READER CLAUDE CODE ACTUALLY RUNS, NOT A CHECKOUT

The reader is whatever ``~/.claude.json``'s ``mcpServers.aggregator`` launches,
and that is usually a PACKAGED BINARY — on this host
``/etc/profiles/per-user/jonathan/bin/aggregator-mcp``, with empty ``args``.
This hook originally understood one config shape only, ``uv run --directory
<dir> aggregator-mcp``, and fell back to ``~/Repos/aggregator`` whenever it saw
anything else. So on 2026-09-05 it read a working tree that happened to sit on
a feature branch at schema 7, compared it against a cache at 6, and announced
AGGREGATOR RECALL IS DOWN about a reader nobody runs. Everything was fine.

A false alarm is not a milder failure than a missed one; it is what turns the
next true alarm into noise, and silence is this check's entire budget. So the
probe is preferred in production form: ``aggregator-schema-probe``, the console
script installed as a sibling of the very ``aggregator-mcp`` the config names,
executed directly.

The precedence, strongest STATEMENT first — not most convenient source:

1. ``AGGREGATOR_SCHEMA_PROBE`` — run exactly this file. The escape hatch.
2. A tree somebody named on purpose: ``AGGREGATOR_READER_DIR``, or the config's
   own ``uv run --directory <tree> aggregator-mcp``.
3. The packaged ``aggregator-schema-probe`` beside the reader binary.
4. ``DEFAULT_READER_DIR``, ``~/Repos/aggregator``. A guess, and the announcement
   says so.

2 SITS ABOVE 3 FOR A CONCRETE REASON. The packaged tier keys off ADJACENCY, and
in the ``--directory`` shape the ``command`` is ``uv`` — whose sibling in the
Nix profile becomes ``aggregator-schema-probe`` the day the console script
deploys. Ranking adjacency above an explicit statement would silently stop
measuring the named tree on every developer config at once, and the symptom
would be a check that looks like it is working. Same for
``AGGREGATOR_READER_DIR``: below the packaged tier it would go inert on deploy
day while still reading like a live knob, which is the worst state a control
can be in.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# The probe is the ONE implementation of the predicate. It lives in the
# aggregator repo beside the schema it describes, so that a future schema bump
# and the check that understands it move in the same commit; a copy vendored
# here would drift the first time the stamping changed, and would drift
# silently, which is the whole failure mode being fixed.
PROBE_RELPATH = Path("aggregator") / "health" / "schema_probe.py"

# The same predicate, packaged: a console script that the aggregator's own
# build installs beside `aggregator-mcp`. Preferred over the source file above
# because it is the artifact that ships with the reader under test — same
# revision, same environment, no branch to be on. Runs as a program, never
# through this hook's interpreter: it is a wrapper script, not a .py file.
PACKAGED_PROBE_NAME = "aggregator-schema-probe"

# Where the MCP reader is checked out, when ~/.claude.json names no reader and
# no packaged probe can be found beside one. Last resort, and the weakest
# statement available: a working tree is whatever branch someone left it on.
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


class Probe(NamedTuple):
    """Which probe to run, how to run it, and where the search went.

    ``looked`` exists so the MISSING announcement can name every candidate. A
    check that says only "not found" hands the reader the wrong thing to fix
    half the time, and this one now has two plausible homes.
    """

    path: Path
    argv: list[str]
    looked: str


def _server_entry() -> dict:
    """``mcpServers.aggregator`` from ~/.claude.json, or ``{}``.

    Read once and passed down: the file is the single statement of what Claude
    Code launches, and two independent reads could disagree mid-session.
    """
    try:
        with open(Path.home() / ".claude.json", "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
        entry = (doc.get("mcpServers") or {}).get("aggregator")
        return entry if isinstance(entry, dict) else {}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _named_tree(entry: dict) -> tuple[Path, str] | None:
    """A tree somebody named ON PURPOSE, and which statement named it.

    Two things say "measure THIS tree" and both are deliberate: the operator
    setting ``AGGREGATOR_READER_DIR``, and a config launching the reader as
    ``uv run --directory <tree> aggregator-mcp``. Neither may be outranked by
    the packaged probe, and the reason is not politeness about intent — it is
    that the packaged tier keys off ADJACENCY. In the ``--directory`` shape
    ``command`` is ``uv``, whose sibling in the Nix profile is
    ``aggregator-schema-probe`` the moment the console script deploys. Ranking
    adjacency above an explicit statement would therefore stop measuring the
    named tree on every developer config at once, silently, on deploy day.
    """
    override = os.environ.get("AGGREGATOR_READER_DIR")
    if override:
        return Path(override).expanduser(), "AGGREGATOR_READER_DIR"
    args = entry.get("args")
    # Normalised, not guarded after the fact: a hand-edited ~/.claude.json can
    # put any JSON type here, and `enumerate(5)` inside this hook raises past
    # every announcement into the top-level `except BaseException`, i.e. exits
    # silently — the one outcome this check may never produce.
    args = args if isinstance(args, list) else []
    for i, a in enumerate(args):
        if a == "--directory" and i + 1 < len(args):
            return (
                Path(str(args[i + 1])).expanduser(),
                "mcpServers.aggregator's --directory argument",
            )
    return None


def _reader_command(raw: str) -> Path | None:
    """The reader binary named by ``command``, as a path with a directory.

    Bare names are looked up on PATH exactly as Claude Code looks them up, so
    a healthy ``{"command": "aggregator-mcp"}`` config is not reported as
    unverifiable for the sake of a missing slash.
    """
    try:
        if os.path.dirname(raw):
            return Path(raw).expanduser()
        found = shutil.which(raw)
        return Path(found) if found else None
    except (OSError, ValueError, TypeError):
        return None


def _packaged_probe(entry: dict) -> tuple[Path | None, str]:
    """The console script beside the reader binary, and where the search went.

    UNRESOLVED DIRECTORY FIRST. ``/etc/profiles/per-user/<u>/bin/aggregator-mcp``
    is a symlink into the Nix store, and the profile ``bin/`` — the directory
    the link sits in, not the one it points at — is where the sibling console
    script is installed. Resolving first and looking only in the store
    directory would miss it on precisely the host this exists for. The resolved
    directory is searched second, because a config may name a store path
    outright.

    The prose half is built here rather than by the caller because only this
    function knows WHY it came back empty, and the three reasons need three
    different repairs: no ``command`` in the config, a ``command`` that did not
    resolve, or a console script that is present but not executable.
    """
    raw = entry.get("command")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return None, (
            "nowhere beside a reader binary — ~/.claude.json's "
            "mcpServers.aggregator names no command, so there was nothing to "
            f"look for {PACKAGED_PROBE_NAME} beside"
        )

    command = _reader_command(raw)
    if command is None:
        # Says which name failed. "names no command" would send the reader to
        # inspect an entry that is present and populated.
        return None, (
            f"nowhere beside a reader binary — mcpServers.aggregator's command "
            f"{raw!r} did not resolve on PATH"
        )

    # Everything this search could not carry out, in the operator's words
    # rather than in silence. See the OSError arm of the stat below.
    unreadable: list[str] = []

    dirs: list[Path] = [command.parent]
    try:
        resolved_parent = command.resolve().parent
        if resolved_parent != dirs[0]:
            dirs.append(resolved_parent)
    except (OSError, ValueError, RuntimeError) as exc:
        # Defensive: `resolve(strict=False)` absorbs symlink loops, unreadable
        # parents and over-long paths and returns a best effort, so no test
        # here drives this arm — but the store half of the search is skipped
        # when it does fire, and a search that quietly got narrower is the
        # shape of failure this whole hook exists to make loud.
        unreadable.append(
            f"the target of {command} could not be resolved "
            f"({type(exc).__name__}: {exc}), so only its own directory was "
            "searched"
        )

    tried: list[Path] = []
    rejected: list[Path] = []
    for d in dirs:
        candidate = d / PACKAGED_PROBE_NAME
        tried.append(candidate)
        # os.stat, not Path.is_file(). pathlib turns EVERY OSError into False,
        # so an unreadable directory arrives here indistinguishable from an
        # empty one — and which of the two it is decides whether the operator
        # runs chmod or reinstalls. (CPython 3.13 widened that swallow to cover
        # EACCES, which 3.12 re-raised. A diagnosis that changes with the
        # interpreter's minor version is not a diagnosis.)
        try:
            mode = os.stat(candidate).st_mode
        except FileNotFoundError:
            # The ordinary case: no console script installed here. Already
            # reported by naming the directory in `tried`.
            continue
        except OSError as exc:
            # Not swallowed. "I looked and it was not there" and "I could not
            # look" are different facts, and only one of them is true.
            unreadable.append(
                f"{candidate} could not be resolved "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        if not stat.S_ISREG(mode):
            continue
        if os.access(candidate, os.X_OK):
            return candidate, f"beside the reader binary, at {candidate}"
        # Present and unusable. Named, because "MISSING" about a file the
        # operator can see on disk costs an afternoon, and the fix is one chmod.
        rejected.append(candidate)

    where = (
        "beside the reader binary named by ~/.claude.json's "
        "mcpServers.aggregator.command ("
        + " and ".join(str(t) for t in tried)
        + ")"
    )
    notes: list[str] = []
    if rejected:
        notes.append(
            " and ".join(str(r) for r in rejected)
            + " exists but is not executable, so it was rejected"
        )
    notes.extend(unreadable)
    if notes:
        where += " — " + "; ".join(notes)
    return None, where


def _probe_invocation() -> Probe:
    """Pick the probe, most authoritative statement first.

    1. ``AGGREGATOR_SCHEMA_PROBE`` — an explicit instruction outranks anything
       discovered, and it is how a probe under development gets exercised.
    2. An explicitly named tree: ``AGGREGATOR_READER_DIR``, or the config's own
       ``--directory``. See ``_named_tree`` for why this sits above the
       packaged probe rather than below it.
    3. The packaged ``aggregator-schema-probe`` beside the reader binary the
       config names. Same build as the reader it reports on.
    4. ``DEFAULT_READER_DIR``. A guess, and labelled as one in ``looked``.
    """
    override = os.environ.get("AGGREGATOR_SCHEMA_PROBE")
    if override:
        path = Path(override).expanduser()
        return Probe(
            path,
            [sys.executable, str(path), "--json"],
            f"where AGGREGATOR_SCHEMA_PROBE points, {path}",
        )

    entry = _server_entry()

    named = _named_tree(entry)
    if named is not None:
        tree, said_by = named
        checkout = tree / PROBE_RELPATH
        return Probe(
            checkout,
            [sys.executable, str(checkout), "--json"],
            f"in the tree named by {said_by}, at {checkout}",
        )

    packaged, where = _packaged_probe(entry)
    if packaged is not None:
        # As a program. It is a wrapper script with its own interpreter, and
        # feeding it to this hook's Python would fail on its first line.
        return Probe(packaged, [str(packaged), "--json"], where)

    # Nobody named a tree — `_named_tree` returned None above — so this is the
    # default and nothing else. Routing it through a helper that re-asked the
    # same question could only ever get the same answer.
    checkout = DEFAULT_READER_DIR / PROBE_RELPATH
    return Probe(
        checkout,
        [sys.executable, str(checkout), "--json"],
        f"{where}; then in the default checkout at {checkout}",
    )


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

    probe = _probe_invocation()
    if not probe.path.is_file():
        _announce(
            f"the aggregator schema-health probe is MISSING at {probe.path}, "
            "so whether aggregator_search_memory works could not be checked. "
            "An empty recall result this session is not evidence that nothing "
            f"was found. Looked {probe.looked}. The probe ships two ways: as "
            f"the {PACKAGED_PROBE_NAME} console script installed next to the "
            "aggregator-mcp binary, and as a source file in the aggregator "
            f"repo at {PROBE_RELPATH}.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
        )
        return 0

    try:
        proc = subprocess.run(
            probe.argv,
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
            # stderr as well as stdout. A packaged probe whose Nix wrapper
            # cannot start exits non-zero with an EMPTY stdout and puts the
            # whole diagnosis — the missing shared object, the interpreter that
            # is not there — on stderr. Quoting only stdout rendered that as
            # "answered in a shape this hook cannot read (stdout starts '')",
            # which blames the probe for saying nothing when it said exactly
            # what was wrong and this hook discarded it.
            "the aggregator schema-health probe answered in a shape this hook "
            f"cannot read (exit {proc.returncode}; stdout starts "
            f"{proc.stdout[:120]!r}; stderr starts {proc.stderr[:200]!r}), so "
            "recall health is unverified this session.",
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

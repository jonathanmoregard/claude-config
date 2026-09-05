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

1. ``AGGREGATOR_SCHEMA_PROBE`` — run exactly this file. The escape hatch. A
   ``.py`` path goes through this interpreter, anything else is executed as a
   program, so it accepts the packaged console script as well as a source file.
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

# The probe's four STATES, mirrored as strings rather than imported, because
# importing across repos is exactly the coupling this hook avoids.
#
# States are not severities and this comment used to blur them: the probe also
# emits a `severity` (down / reduced / silent) which is how LOUDLY to speak,
# and it maps UNKNOWN to `down` deliberately — its rule is "unknown means warn,
# never fine". So `severity == down` answers "should this be shouted?" and
# never "is recall refusing calls?". Every headline below keys off STATE, and
# the previous version keying off severity is why an unmeasurable probe was
# announced as a live outage.
FINE = "fine"
DEAD = "dead"
WILL_ROT = "will-rot"


# How the probe was chosen, in the announcement's own words. The tiers are not
# equally trustworthy and the difference is the whole point: the packaged
# sibling IS the reader under test, while TIER_GUESS is this hook's own
# assumption about a tree nobody named. A verdict that does not say which one
# spoke is an assertion with no source attached.
TIER_OVERRIDE = "AGGREGATOR_SCHEMA_PROBE override"
TIER_PACKAGED = "packaged sibling of the reader"
TIER_GUESS = "default checkout GUESS"

# Said out loud on every announcement from TIER_GUESS, because this is exactly
# what went wrong on 2026-09-05: a guessed checkout on a feature branch
# produced AGGREGATOR RECALL IS DEAD in the same words a real measurement uses.
GUESS_CAVEAT = (
    "nothing in ~/.claude.json named this tree, so this verdict may not "
    "describe the reader Claude Code actually runs"
)


class Probe(NamedTuple):
    """Which probe to run, how to run it, where the search went, and its rank.

    ``looked`` exists so the MISSING announcement can name every candidate. A
    check that says only "not found" hands the reader the wrong thing to fix
    half the time, and this one now has two plausible homes.

    ``tier`` travels with EVERY announcement, not just the failures. Silence is
    this hook's budget and a verdict is what it spends the budget on, so the
    verdict has to carry how much it is worth.
    """

    path: Path
    argv: list[str]
    looked: str
    tier: str
    # A fact about how the tier was chosen that the tier name cannot carry —
    # today, that ~/.claude.json could not be read, which demotes the search
    # to its guess without anything else saying so.
    note: str | None = None


def _server_entry() -> tuple[dict, str | None]:
    """``mcpServers.aggregator`` from ~/.claude.json, and why it is empty.

    Read once and passed down: the file is the single statement of what Claude
    Code launches, and two independent reads could disagree mid-session.

    ABSENT AND UNREADABLE ARE DIFFERENT FACTS. Both used to collapse into
    ``{}``, so a truncated or half-written config produced "mcpServers.
    aggregator names no command" — a claim about a file this hook never got
    inside. It also demotes the whole search to its guess tier, quietly, which
    is the shape of failure the rest of this module exists to make loud. A
    missing file is the ordinary case and stays silent; anything else is named.
    """
    path = Path.home() / ".claude.json"
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
        entry = (doc.get("mcpServers") or {}).get("aggregator")
        if entry is None:
            return {}, None
        if not isinstance(entry, dict):
            # An entry someone wrote and got wrong. Collapsing it to {} made
            # the hook report that the config "names no command" about a line
            # sitting right there — and, since silence is gated on there being
            # no config problem, let a healthy verdict from an unrelated tier
            # pass as verified.
            return {}, (
                f"{path}'s mcpServers.aggregator is a "
                f"{type(entry).__name__}, not an object, so no reader could "
                "be resolved from it"
            )
        return entry, None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return {}, (
            f"{path} could not be read ({type(exc).__name__}: {exc}), so no "
            "reader could be resolved from it"
        )


def _named_tree(entry: dict) -> tuple[tuple[Path, str] | None, str | None]:
    """A tree somebody named ON PURPOSE, and which statement named it.

    The value must be absolute (after ``~`` expansion) or it is refused with a
    reason — see the comment on the check itself.

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
    # BOTH sources, in preference order — not an either/or. Trying only the
    # first one present meant a typo in AGGREGATOR_READER_DIR disqualified a
    # perfectly good `--directory` alongside it and dropped the check two whole
    # tiers, on the strength of a mistake in an unrelated place.
    candidates: list[tuple[str, str]] = []
    override = os.environ.get("AGGREGATOR_READER_DIR")
    if override:
        candidates.append((override, "AGGREGATOR_READER_DIR"))
    args = entry.get("args")
    # Normalised, not guarded after the fact: a hand-edited ~/.claude.json can
    # put any JSON type here, and `enumerate(5)` inside this hook raises past
    # every announcement into the top-level `except BaseException`, i.e. exits
    # silently — the one outcome this check may never produce.
    args = args if isinstance(args, list) else []
    for i, a in enumerate(args):
        if a == "--directory" and i + 1 < len(args):
            candidates.append(
                (str(args[i + 1]), "mcpServers.aggregator's --directory argument")
            )
            break

    # ABSOLUTE, OR IT IS NOT A TREE. This hook inherits the SESSION's working
    # directory — whatever project the operator happened to open — so a
    # relative or empty value would make it stat, and then EXECUTE,
    # ./aggregator/health/schema_probe.py out of an unrelated repository. Both
    # the verdict and the code producing it would belong to something nobody
    # asked about. Refused and named rather than silently dropped, because the
    # shape of this mistake is a typo in a config the operator believes is
    # aiming the check somewhere.
    problems: list[str] = []
    for raw, said_by in candidates:
        try:
            tree = Path(raw).expanduser()
        except (RuntimeError, OSError, ValueError) as exc:
            problems.append(
                f"{said_by} is {raw!r}, which could not be expanded "
                f"({type(exc).__name__}: {exc}), so it was ignored"
            )
            continue
        if not tree.is_absolute():
            problems.append(
                f"{said_by} is {raw!r}, which is not an absolute path — it "
                "would resolve against whatever directory the session was "
                "started in, so it was ignored"
            )
            continue
        # A refusal further up still gets reported: the operator believes that
        # value is doing something.
        return (tree, said_by), ("; ".join(problems) or None)
    return None, ("; ".join(problems) or None)


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


def _packaged_probe(entry: dict, config_problem: str | None = None) -> tuple[Path | None, str]:
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
        if config_problem:
            # Not "names no command": the file was never read, so it said
            # nothing either way. The detail rides on the provenance line.
            return None, (
                "nowhere beside a reader binary — ~/.claude.json could not be "
                "read, so no reader binary was named"
            )
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
    irregular: list[Path] = []
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
            # A directory or socket standing exactly where the console script
            # belongs. Skipping it in silence left MISSING pointing away from
            # the one thing in the way.
            irregular.append(candidate)
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
    if irregular:
        notes.append(
            " and ".join(str(i) for i in irregular)
            + " exists but is not a regular file, so it was rejected"
        )
    notes.extend(unreadable)
    if notes:
        where += " — " + "; ".join(notes)
    return None, where


def _probe_invocation() -> Probe:
    """Pick the probe, most authoritative statement first.

    1. ``AGGREGATOR_SCHEMA_PROBE`` — an explicit instruction outranks anything
       discovered, and it is how a probe under development gets exercised. A
       ``.py`` path runs through this interpreter; anything else is run as a
       program, so the hatch also accepts the packaged console script.
    2. An explicitly named tree: ``AGGREGATOR_READER_DIR``, or the config's own
       ``--directory``. See ``_named_tree`` for why this sits above the
       packaged probe rather than below it.
    3. The packaged ``aggregator-schema-probe`` beside the reader binary the
       config names. Same build as the reader it reports on.
    4. ``DEFAULT_READER_DIR``. A guess, and every announcement says so.
    """
    override_problem = None
    path = None
    override = os.environ.get("AGGREGATOR_SCHEMA_PROBE")
    if override:
        # ABSOLUTE, OR IT IS NOT A PROBE — the same rule the tree tiers get,
        # for the same reason: this hook inherits the SESSION's working
        # directory, so a relative value is stat'd and then EXECUTED out of
        # whatever project the operator happened to open. And `expanduser`
        # RAISES on `~nouser/...`, which travelled past every announcement
        # into the fail-open floor.
        try:
            candidate = Path(override).expanduser()
        except (RuntimeError, OSError, ValueError) as exc:
            override_problem = (
                f"AGGREGATOR_SCHEMA_PROBE is {override!r}, which could not be "
                f"expanded ({type(exc).__name__}: {exc}), so it was ignored"
            )
        else:
            if candidate.is_absolute():
                path = candidate
            else:
                override_problem = (
                    f"AGGREGATOR_SCHEMA_PROBE is {override!r}, which is not an "
                    "absolute path — it would resolve against whatever "
                    "directory the session was started in, so it was ignored"
                )

    if path is not None:
        note = None
        if path.suffix == ".py":
            # The source probe, and every stub in the test suite: a plain
            # module with no shebang and no exec bit. The systemd sibling runs
            # it this way too.
            argv = [sys.executable, str(path), "--json"]
        else:
            # Anything else is a program. "Run exactly this file" was only ever
            # true of .py paths — pointing the documented escape hatch at the
            # PACKAGED probe, which is a bash wrapper, fed a shebang to Python
            # and produced UNVERIFIED noise from an override that was working.
            argv = [str(path), "--json"]
            try:
                is_regular = stat.S_ISREG(os.stat(path).st_mode)
            except OSError:
                # Absent or unreachable. main() diagnoses that properly; do not
                # pre-empt it with a guess about the exec bit.
                is_regular = False
            if is_regular and not os.access(path, os.X_OK):
                note = (
                    f"{path} is not a .py file, so it is run as a program — "
                    "but it is not executable"
                )
        return Probe(
            path,
            argv,
            f"where AGGREGATOR_SCHEMA_PROBE points, {path}",
            TIER_OVERRIDE,
            note,
        )

    entry, config_problem = _server_entry()

    named, tree_problem = _named_tree(entry)
    # Every fault describes how the tier was chosen, so they ride one line.
    note = "; ".join(
        n for n in (override_problem, config_problem, tree_problem) if n
    ) or None

    if named is not None:
        tree, said_by = named
        checkout = tree / PROBE_RELPATH
        return Probe(
            checkout,
            [sys.executable, str(checkout), "--json"],
            f"in the tree named by {said_by}, at {checkout}",
            f"tree named by {said_by}",
            note,
        )

    packaged, where = _packaged_probe(entry, config_problem)
    if packaged is not None:
        # As a program. It is a wrapper script with its own interpreter, and
        # feeding it to this hook's Python would fail on its first line.
        return Probe(
            packaged, [str(packaged), "--json"], where, TIER_PACKAGED, note,
        )

    # Nobody named a tree — `_named_tree` returned None above — so this is the
    # default and nothing else. Routing it through a helper that re-asked the
    # same question could only ever get the same answer.
    checkout = DEFAULT_READER_DIR / PROBE_RELPATH
    return Probe(
        checkout,
        [sys.executable, str(checkout), "--json"],
        f"{where}; then in the default checkout at {checkout}",
        TIER_GUESS,
        note,
    )


def _provenance(probe: Probe, *, measured: bool) -> str:
    """The one line that says what spoke, and how far to trust it.

    ``measured`` distinguishes "this verdict came out of that file" from "that
    file is the one I went looking for" — the second is all a MISSING, timed
    out or unrunnable probe can honestly claim.
    """
    verb = "measured by" if measured else "probe selected"
    line = f"{verb}: {probe.path} ({probe.tier})"
    clauses = []
    if probe.tier == TIER_GUESS:
        clauses.append(GUESS_CAVEAT)
    if probe.note:
        clauses.append(probe.note)
    if clauses:
        line += " — " + "; ".join(clauses)
    return line


# Set once an announcement has actually reached stdout, so the fail-open floor
# can tell "nothing was said" from "something was said and then this broke".
# Two JSON objects on stdout parse as neither, and the harness drops an
# unparseable payload in silence — the exact outcome being defended against.
_ANNOUNCED = False


def _announce(summary: str, headline: str, provenance: str) -> None:
    global _ANNOUNCED
    if _ANNOUNCED:
        return
    message = f"{headline}\n  - {summary}\n  - {provenance}"
    payload = {
            "systemMessage": message,
            # Factual, not imperative. Prompt-injection defenses can surface
            # imperative hook text to the user instead of acting on it, and
            # the agent only needs the fact — what to do about a dead index
            # follows from knowing it is dead.
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                # The provenance reaches the AGENT as well as the operator.
                # The agent is the party deciding whether to trust an empty
                # recall result, and "this verdict may be about the wrong tree"
                # changes that decision.
                "additionalContext": (
                    "Aggregator recall health at session start: "
                    + summary
                    + " ["
                    + provenance
                    + "]"
                ),
            },
    }
    # Serialised in full BEFORE anything is written: a half-written object on
    # stdout is worse than none, and `json.dump` streams.
    text = json.dumps(payload)
    sys.stdout.write(text)
    sys.stdout.flush()
    _ANNOUNCED = True


def _last_resort(exc: BaseException, probe: Probe | None) -> None:
    """Speak on the way down. The fail-open floor may not be a silent one.

    Every path in this hook exits 0 — a broken health check must not break
    session startup — but an internal fault that exits 0 with an empty stdout
    is byte-for-byte the healthy machine. Built from literals, the exception's
    class name and the selected probe, so it has nothing left to fail on.
    """
    if _ANNOUNCED:
        return
    where = (
        f"{probe.path} ({probe.tier})" if probe is not None
        else "no probe had been selected yet"
    )
    payload = {
        "systemMessage": (
            "AGGREGATOR RECALL: health UNVERIFIED.\n"
            f"  - this check itself failed with {type(exc).__name__}, so "
            "recall health is unverified this session. An empty recall result "
            "is not evidence that nothing was found.\n"
            f"  - probe selected: {where}"
        ),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Aggregator recall health at session start: the check itself "
                f"failed with {type(exc).__name__}, so health is unverified. "
                f"Probe selected: {where}."
            ),
        },
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def main() -> int:
    try:
        # Read and discard. This check depends on the machine's state, not on
        # anything the session says; draining stdin keeps the harness from
        # seeing a broken pipe.
        sys.stdin.read()
    except (OSError, ValueError):
        pass

    probe = None
    try:
        probe = _probe_invocation()
        return _check(probe)
    except BaseException as exc:
        # Announced, not swallowed. The bare floor in __main__ stays underneath
        # as the last-ditch guarantee that startup is never broken.
        _last_resort(exc, probe)
        return 0


def _check(probe: Probe) -> int:
    # os.stat, not Path.is_file(), for the reason spelled out in
    # `_packaged_probe`: pathlib maps EVERY OSError to False, so an unreadable
    # path arrives indistinguishable from an absent one. Announcing MISSING for
    # a probe that is sitting right there sends the operator to reinstall a
    # file they can see.
    try:
        present = stat.S_ISREG(os.stat(probe.path).st_mode)
        # Something is there, it just is not a file — a directory or a socket
        # standing exactly where the probe belongs. The packaged tier already
        # names that case; announcing MISSING about a path with a directory
        # sitting on it sends the operator to install what is in the way.
        irregular = not present
        unreachable = None
    except FileNotFoundError:
        present, irregular, unreachable = False, False, None
    except OSError as exc:
        present = irregular = False
        unreachable = (
            f"{probe.path} could not be resolved "
            f"({type(exc).__name__}: {exc})"
        )

    if unreachable is not None:
        _announce(
            f"the aggregator schema-health probe could not be examined — "
            f"{unreachable} — so whether aggregator_search_memory works could "
            "not be checked. An empty recall result this session is not "
            "evidence that nothing was found. The file may well be present: "
            "this is a permissions or filesystem fault on the path, not a "
            "missing install.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
            _provenance(probe, measured=False),
        )
        return 0

    if irregular:
        _announce(
            f"the aggregator schema-health probe path {probe.path} exists but "
            "is not a regular file — a directory or socket is standing where "
            "the probe belongs — so whether aggregator_search_memory works "
            "could not be checked. An empty recall result this session is not "
            "evidence that nothing was found.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
            _provenance(probe, measured=False),
        )
        return 0

    if not present:
        _announce(
            f"the aggregator schema-health probe is MISSING at {probe.path}, "
            "so whether aggregator_search_memory works could not be checked. "
            "An empty recall result this session is not evidence that nothing "
            f"was found. Looked {probe.looked}. The probe ships two ways: as "
            f"the {PACKAGED_PROBE_NAME} console script installed next to the "
            "aggregator-mcp binary, and as a source file in the aggregator "
            f"repo at {PROBE_RELPATH}.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
            _provenance(probe, measured=False),
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
            _provenance(probe, measured=False),
        )
        return 0
    except (OSError, ValueError) as exc:
        _announce(
            f"the aggregator schema-health probe could not be run "
            f"({type(exc).__name__}: {exc}), so recall health is unverified "
            "this session.",
            "AGGREGATOR RECALL: health UNVERIFIED.",
            _provenance(probe, measured=False),
        )
        return 0

    try:
        verdict = json.loads(proc.stdout)
        state = verdict["state"]
        summary = verdict["summary"]
        if not isinstance(state, str) or not isinstance(summary, str):
            # The key existing proves nothing about its type. Everything
            # downstream concatenates `summary` as text and compares `state`
            # as text, so a null or a number reached string arithmetic and
            # raised — into the fail-open floor, i.e. into silence.
            raise TypeError("state and summary must both be strings")
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
            _provenance(probe, measured=False),
        )
        return 0

    if state == FINE:
        # SILENCE IS A CLAIM, AND ONLY SOME TIERS CAN MAKE IT. Nothing printed
        # means "checked and healthy" — that contract is why this hook gets a
        # SessionStart slot. A `fine` from the GUESS tier measured a tree
        # nobody named, and a `fine` reached after ~/.claude.json could not be
        # read was produced by a hook that does not know what Claude Code
        # launches. Both are "not checked". Returning silently for either is
        # the original incident with the sign flipped: recall could be dead
        # while an unrelated tree agreed with itself, and the session would be
        # told exactly as much as if the check had passed.
        if probe.tier != TIER_GUESS and probe.note is None:
            # The earned silence. Nothing printed, nothing injected.
            return 0
        # One calm line. NOT the outage headline — announcing an unverified
        # pass as a failure would be its own false alarm, and this branch
        # exists to stop manufacturing those.
        _announce(
            "the schema probe found nothing wrong, but it did not measure the "
            "reader Claude Code runs, so this is 'not checked' rather than "
            "'checked and healthy'. Recall may well be fine — nothing here "
            "verified it.",
            "AGGREGATOR RECALL: looked healthy, but not verified.",
            _provenance(probe, measured=True),
        )
        return 0

    if state == DEAD:
        headline = (
            "AGGREGATOR RECALL IS DOWN — aggregator_search_memory is refusing "
            "every call this session."
        )
    elif state == WILL_ROT:
        headline = (
            "AGGREGATOR RECALL is working now but will break on the next "
            "ingest tick."
        )
    else:
        # `unknown`, or a state added to the probe after this hook was
        # written. Announced, never silent — silence here means "checked and
        # healthy", and going quiet on an unrecognised verdict reproduces the
        # original incident. But not asserted as an outage either: the probe
        # said it could not measure, not that recall is refusing.
        headline = "AGGREGATOR RECALL: health UNVERIFIED."
    _announce(summary, headline, _provenance(probe, measured=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A broken health check must not be the thing that breaks startup.
        sys.exit(0)

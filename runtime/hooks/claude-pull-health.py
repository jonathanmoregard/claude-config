#!/usr/bin/env python3
"""SessionStart check: tell the session when its checkouts stopped updating.

WHY A HOOK, WHEN A DESKTOP NOTIFICATION ALREADY EXISTS

Because the victim of this failure is not the operator, it is the agent.
``scripts/claude-pull.sh`` keeps ``~/.claude`` and every marketplace clone
fast-forwarded, and its only outbound channel for "I could not" was
``notify-send``: a toast on Jonathan's desktop that no Claude session can see.

Measured on 2026-08-31: ``~/.claude/plugins/marketplaces/jonathanmoregard`` —
the checkout the harness actually resolves plugins from — was **35 commits**
behind origin, and nothing anywhere said so. Every session in that window
served skills, commands and agents from a stale tree while reporting success,
because a stale checkout and a current one are indistinguishable from inside a
session. The same blindness costs more the other way round: ``git worktree add
-b x origin/master`` cuts from whatever was last fetched, so a stale
``~/.claude`` silently bases new work on an old master.

This is the same shape as the aggregator-embed incident (a unit failing every
30 minutes for ~5 hours behind a 24h notification debounce, no session any the
wiser), and it takes the same remedy that was already merged for it:
``hooks/aggregator-recall-health.py`` states the fact at SessionStart, in the
session's own context, which is the only channel that reaches the thing that is
actually degraded. Two notification systems for one class of failure would be
its own bug, so this is that mechanism pointed at a different probe — the
durable status records claude-pull.sh now writes under
``~/.local/state/claude-pull/``.

SILENCE IS THE BUDGET

Healthy means this prints nothing at all. It competes with eight other
SessionStart entries, and a check that speaks every session is one nobody reads
by the time it matters. Correspondingly, "could not tell" is announced rather
than swallowed: no record, an unparseable record, and a record nobody has
refreshed in half a day all speak. Silence has to mean "checked and healthy",
never "could not tell", or it stops carrying information.

TARGETS FIND THEMSELVES

The expected set is ``~/.claude`` plus every repository some sync unit has
declared itself responsible for, read off the state root: the producers
``mkdir -p`` their state directory at the top of every run, before they can
fail at anything, so that directory IS the declaration of coverage — recorded
by the producer rather than guessed at by the reader. Deriving the set rather
than listing it is the whole point: the failure being fixed is a checkout
nobody was watching, so a hook that only watches the ones someone remembered to
name reproduces it. Within that set a missing status record is a finding, not a
blank.

The coverage filter is not incidental. This machine has six marketplace
clones, five of them third-party and pinned on purpose; nothing auto-pulls
third-party plugin code here, and a health check must not make that decision by
implication. Five permanent "uncovered" lines would also spend the entire
attention budget below. A repo that WAS covered keeps its state directory, so
it still goes stale and still speaks — losing a puller is noticed, which is the
direction that matters.

BOTH DIRECTIONS, ONE CHANNEL

``scripts/claude-proposals-push.sh`` keeps the proposals sink
(``~/.local/state/claude-proposals``) backed up to a private GitHub remote. Its
failure has the same SHAPE as a failed pull — an unattended git operation that
stops and tells nobody — and the opposite consequence: nothing is going stale,
something is not being backed up. Two notification systems for one class of
failure would be its own bug, so it writes the same record with
``direction="push"`` and this hook reads both, wording each finding for what it
actually costs. A record with no ``direction`` is a pull: that is what every
record claude-pull.sh has ever written looks like.

Note also that the sink lives in ``~/.local/state``, not under ``~/.claude`` —
which is why discovery reads the state root rather than walking the config
tree.

FAIL-OPEN ON ITS OWN ERRORS, EXCEPT THE ONE THAT MATTERS

Every path exits 0, including ``except BaseException``: a broken health check
must not be the thing that breaks session startup. But "the record would not
parse" is not an internal error to swallow — it is reported, because a health
check that goes quiet when its own machinery breaks is indistinguishable from
one reporting good news.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Twelve hours, chosen against the machine this runs on. claude-pull.timer is
# OnCalendar=*:0/10 with Persistent=true on a laptop that suspends, so a
# healthy machine refreshes every record within a minute of resume. A threshold
# under a normal night's sleep would fire on the first session after every
# wake, and a check that cries wolf on a healthy machine is one that gets
# ignored on the day it is right. A genuinely dead timer crosses this inside a
# working day.
STALE_S = 12 * 3600

# Reasons are assembled from git's stderr, which quotes file names — so a repo
# can steer text of its choosing into a session's context through a crafted
# path. It is reported as bounded, quoted data.
REASON_CAP = 240

# States written by scripts/claude-pull.sh. Compared as strings rather than
# imported: the producer is bash, and the contract between them is the record
# on disk, not a shared module.
OK = "ok"
ALERT = "alert"
UNVERIFIED = "unverified"

# Which way the repo is meant to move. Absent means PULL — every record
# claude-pull.sh wrote before the field existed, and the field must not turn
# those into unrecognised ones.
PULL = "pull"
PUSH = "push"

# Journal to send the reader to, per direction. Both pullers (claude-pull,
# claude-marketplace-pull) share one script and one unit prefix.
JOURNAL = {
    PULL: "journalctl --user -u claude-pull",
    PUSH: "journalctl --user -u claude-proposals-push",
}


def _state_root() -> Path:
    override = os.environ.get("CLAUDE_PULL_STATE_ROOT")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "claude-pull"


def _claude_home() -> Path:
    override = os.environ.get("CLAUDE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def _covered(state_root: Path, repo: Path) -> bool:
    """Has anything ever pointed the puller at this repo?

    claude-pull.sh creates the state directory at the top of every run, before
    it can fail at anything, so the directory's existence is the puller's own
    declaration of "this repo is mine" — recorded by the producer rather than
    guessed at by the reader.
    """
    return (state_root / str(repo).replace("/", "%")).is_dir()


def _targets(state_root: Path) -> list[Path]:
    """Every repository whose sync this machine claims to maintain.

    ``~/.claude`` is unconditional: it is the config repo, a timer has pulled
    it since 2026-04, and if that ever stops the session needs to hear so.

    Everything else is discovered from the state root, because the failure
    being fixed is a checkout nobody was watching — a hook that only watches
    the ones someone remembered to name reproduces it, and the next repo to get
    a sync unit should be covered without editing this file. That has since
    stopped being hypothetical twice over: the marketplace clone under
    ``~/.claude/plugins/marketplaces/``, and the proposals sink under
    ``~/.local/state/claude-proposals``, which a walk of the config tree could
    never have found at all.

    Discovery is filtered by coverage, and that filter is load-bearing.
    Measured 2026-08-31: this machine has SIX marketplace clones, five of them
    third-party (kettleofskills, nix-search-marketplace, skills-curated,
    trailofbits, zach-source-plugins). Those are pinned on purpose — nothing
    auto-pulls third-party plugin code onto this host, and nothing should start
    doing so as a side effect of a health check. Announcing them as "uncovered"
    every session would be five permanent lines of noise, which spends exactly
    the attention budget this hook depends on having.

    So: a repo is a target once a sync unit has been pointed at it, which it
    declares by creating its state directory. A repo that was covered and then
    stopped being synced keeps that directory, so it still goes stale and still
    speaks — removing a puller is noticed, which is the direction that matters.
    """
    override = os.environ.get("CLAUDE_PULL_HEALTH_TARGETS")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]

    home = _claude_home()
    found = [home]
    try:
        children = sorted(state_root.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        # The slug is the absolute path with '/' -> '%', which is what both
        # producers write and what _covered() reads back. Anything that does
        # not decode to an absolute path was not written by them.
        repo = Path(child.name.replace("%", "/"))
        if not repo.is_absolute() or repo == home or repo in found:
            continue
        found.append(repo)
    return found


def _direction(doc) -> str:
    """Which way this repo is meant to move. Absent reads as a pull."""
    if isinstance(doc, dict):
        value = str(doc.get("direction") or PULL).strip().lower()
        if value in (PULL, PUSH):
            return value
    return PULL


def _record(state_root: Path, repo: Path):
    """(state, reason) for one target. ``None`` state means 'no verdict'."""
    path = state_root / str(repo).replace("/", "%") / "status.json"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # The state DIRECTORY is created at the top of every run, before the
        # puller can fail at anything. Its presence without a status.json is
        # therefore a different fact from its absence: "pointed at, never
        # reached a verdict" versus "nothing pulls this at all".
        if _covered(state_root, repo):
            return None, (
                "the puller has been pointed at it but has never reached a "
                "verdict, so it may not have run since it was set up"
            )
        return None, "no status record exists"
    try:
        doc = json.loads(raw)
        state = str(doc["state"])
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"its status record could not be read ({type(exc).__name__})"
    if not isinstance(doc, dict):
        return None, "its status record is not an object"
    return state, doc


def _age(doc: dict, key: str) -> float | None:
    """Seconds since `key`, or None when the record does not carry one.

    `float(doc.get(key) or 0)` used to fold "absent", "null" and "empty" into
    epoch zero, so a record that had never carried a timestamp was reported as
    "last checked 496722h ago — the puller appears to have stopped running".
    Right alarm, wrong text: it sends the reader to journalctl for a unit that
    is working. A missing value is its own verdict, so it gets its own branch.
    """
    raw = doc.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return time.time() - float(raw)
    except (TypeError, ValueError):
        return None


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > REASON_CAP:
        text = text[:REASON_CAP] + f"… ({len(text)} chars, truncated)"
    return text


def _verdict(state_root: Path, repo: Path) -> tuple[str, str] | None:
    """(direction, one line of finding) for a target, or None when healthy.

    Every branch below is worded for what the failure actually COSTS, which is
    the opposite thing in each direction: a stalled pull serves stale code to
    the session, a stalled push leaves finished work on one disk. Saying "the
    checkout is not being updated" about a repo nothing reads would send the
    reader looking for a problem that is not there.
    """
    state, doc = _record(state_root, repo)

    if state is None:
        # doc is the diagnostic string in this branch, and there is no record
        # to read a direction out of.
        return PULL, (
            f"{repo}: {doc} — nothing is auto-pulling it, so how far behind "
            "origin it has drifted is unknown."
        )

    direction = _direction(doc)

    if state == ALERT:
        if direction == PUSH:
            return direction, (
                f"{repo}: auto-push is BLOCKED and its commits are not reaching "
                f"origin. Reported cause: \"{_clip(doc.get('reason', ''))}\""
            )
        return direction, (
            f"{repo}: auto-pull is BLOCKED and the checkout is not being "
            f"updated. Reported cause: \"{_clip(doc.get('reason', ''))}\""
        )

    if state == UNVERIFIED:
        if direction == PUSH:
            return direction, (
                f"{repo}: the last auto-push tick could not reach origin, so it "
                "is unknown whether its commits have been backed up. Reported "
                f"cause: \"{_clip(doc.get('reason', ''))}\""
            )
        return direction, (
            f"{repo}: the last auto-pull tick could not reach origin, so it is "
            "unknown whether this checkout is current. Reported cause: "
            f"\"{_clip(doc.get('reason', ''))}\""
        )

    age = _age(doc, "checked_at_epoch")
    if age is None:
        return direction, f"{repo}: its status record carries no readable timestamp."
    if age > STALE_S:
        tail = (
            "the pusher appears to have stopped running, so anything written "
            "since exists on this disk only."
            if direction == PUSH
            else "the puller appears to have stopped running, so this checkout "
            "may be behind origin."
        )
        return direction, (
            f"{repo}: last checked {age / 3600:.0f}h ago (threshold "
            f"{STALE_S // 3600}h) — {tail}"
        )

    if state != OK:
        return direction, (
            f"{repo}: its status record reports an unrecognised state {state!r}."
        )

    return None


def _announce(findings: list[str], any_blocked: bool, directions: set[str]) -> None:
    pulling = PULL in directions
    pushing = PUSH in directions
    if pulling and pushing:
        subject = "CLAUDE REPO SYNC"
        blocked_tail = "at least one repo this machine keeps in sync is stuck."
    elif pushing:
        subject = "CLAUDE REPO AUTO-PUSH"
        blocked_tail = "at least one repo this machine backs up is not reaching its remote."
    else:
        subject = "CLAUDE CHECKOUT AUTO-PULL"
        blocked_tail = "at least one tree this session reads from is not being updated."

    if any_blocked:
        headline = f"{subject} IS BLOCKED — {blocked_tail}"
    else:
        headline = f"{subject}: freshness UNVERIFIED."

    body = "\n".join(f"  - {f}" for f in findings)
    # Only the consequences that apply. Telling a reader their skills may be
    # stale when the finding is an unpushed backup sends them to the wrong
    # place, and a consequence line nobody trusts is one nobody reads.
    parts = []
    if pulling:
        parts.append(
            "skills, hooks, commands and plugins may be served from a stale "
            "tree, and a new worktree cut from origin/master may be based on an "
            "old commit"
        )
    if pushing:
        parts.append(
            "work written since the last successful push exists on this disk "
            "only, so a disk failure or a bad rm loses it"
        )
    journals = " · ".join(
        JOURNAL[d] for d in (PULL, PUSH) if d in directions
    )
    consequence = (
        f"Consequences while this holds: {'; '.join(parts)}. Journal: {journals}"
    )
    message = f"{headline}\n{body}\n{consequence}"
    json.dump(
        {
            "systemMessage": message,
            # Factual, not imperative. Prompt-injection defenses can surface
            # imperative hook text to the user instead of acting on it, and the
            # agent only needs the fact — what to do about a stale checkout
            # follows from knowing it is stale.
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "Repo sync health at session start:\n" + message,
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

    state_root = _state_root()
    findings: list[str] = []
    directions: set[str] = set()
    blocked = False
    for repo in _targets(state_root):
        verdict = _verdict(state_root, repo)
        if verdict:
            direction, text = verdict
            findings.append(text)
            directions.add(direction)
            if "BLOCKED" in text:
                blocked = True

    if not findings:
        # The earned silence. Nothing printed, nothing injected.
        return 0

    _announce(findings, blocked, directions)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A broken health check must not be the thing that breaks startup.
        sys.exit(0)

#!/usr/bin/env python3
"""The SessionStart hook that tells a session its checkouts have gone stale.

WHAT THIS GUARDS. ``scripts/claude-pull.sh`` had exactly one outbound channel
for "I could not pull": ``notify-send``. That reaches Jonathan's desktop and no
Claude session. So a checkout unpullable for a week looks, from inside a
session, precisely like one that fast-forwarded a minute ago — and the session
is the party that suffers. It branches new worktrees off a master weeks behind,
or resolves a plugin from a marketplace clone 35 commits stale, and reports
success either way.

That is the aggregator-embed shape: a unit failing every tick for hours behind
a 24-hour notification debounce, with no session any the wiser. The fix taken
here is the one already merged for that class of failure — a SessionStart hook
stating the fact in the session's own context (PR #29,
``hooks/aggregator-recall-health.py``) — pointed at the durable status records
the puller now writes.

The hook's two failure modes are opposites and both are silent, so each gets
tests here:

  * speaking when healthy — burns the one budget it has. Eight other
    SessionStart entries already compete for attention, and a check that fires
    every session is one nobody reads on the session it matters.
  * staying quiet when it could not tell — the failure the whole thing exists
    to fix. No record, an unparseable record and a record nobody has refreshed
    in half a day all have to speak, because silence must mean "checked and
    healthy" and never "could not tell".

Every check below was run RED first, against a hook that did not exist.

Run: python3 tests/test_claude_pull_health_hook.py
Exits 0 on success; 1 with a failure list otherwise.

Plain asserts rather than pytest: there is no pytest on this machine, and the
repo's other Python suites (mcps/safe-bash/test_safe_bash.py,
permission-ledger/tests/) use this same check/section harness.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "claude-pull-health.py"

# Mirrored from the hook. Twelve hours is chosen against the machine this runs
# on: a laptop that suspends overnight, whose claude-pull.timer is
# OnCalendar + Persistent and therefore catches up within a minute of resume. A
# threshold under a normal night's sleep would fire on the first session after
# every wake — and a check that cries wolf on a healthy machine is one that
# gets ignored on the day it is right.
STALE_S = 12 * 3600

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


def write_status(state_root: Path, repo: Path, **fields) -> Path:
    """Write a status record the way claude-pull.sh writes it."""
    now = int(time.time())
    doc = {
        "repo": str(repo),
        "state": "ok",
        "reason": "",
        "checked_at_epoch": now,
        "checked_at": "2026-08-31T18:00:00+02:00",
        "last_ok_epoch": now,
    }
    doc.update(fields)
    d = state_root / str(repo).replace("/", "%")
    d.mkdir(parents=True, exist_ok=True)
    p = d / "status.json"
    p.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    return p


def run_hook(state_root, targets, claude_home=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PULL_STATE_ROOT"] = str(state_root)
    if targets is not None:
        env["CLAUDE_PULL_HEALTH_TARGETS"] = os.pathsep.join(str(t) for t in targets)
    else:
        env.pop("CLAUDE_PULL_HEALTH_TARGETS", None)
    if claude_home is not None:
        env["CLAUDE_HOME"] = str(claude_home)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def announcement(proc) -> dict:
    """Parse the hook's announcement, or return {} if it stayed silent."""
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"systemMessage": f"<unparseable hook output: {proc.stdout[:200]!r}>"}


class Sandbox:
    """A temp dir with a state root and however many fake checkouts are asked for."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_root = self.root / "claude-pull"
        self.state_root.mkdir()

    def repo(self, name: str = "checkout") -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


# ----------------------------------------------------------------------
section("The earned silence")
# ----------------------------------------------------------------------

with Sandbox() as sb:
    repo = sb.repo()
    write_status(sb.state_root, repo)
    proc = run_hook(sb.state_root, [repo])
    check("fresh ok record prints nothing", proc.stdout == "", detail=repr(proc.stdout))
    check("fresh ok record exits 0", proc.returncode == 0, detail=str(proc.returncode))

with Sandbox() as sb:
    a, b = sb.repo("a"), sb.repo("b")
    write_status(sb.state_root, a)
    write_status(sb.state_root, b)
    proc = run_hook(sb.state_root, [a, b])
    check("several healthy targets print nothing", proc.stdout == "",
          detail=repr(proc.stdout))

with Sandbox() as sb:
    repo = sb.repo()
    now = int(time.time())
    write_status(sb.state_root, repo,
                 checked_at_epoch=now - STALE_S + 600,
                 last_ok_epoch=now - STALE_S + 600)
    proc = run_hook(sb.state_root, [repo])
    check("a record just inside the staleness window stays silent",
          proc.stdout == "", detail=repr(proc.stdout))


# ----------------------------------------------------------------------
section("The states that must speak")
# ----------------------------------------------------------------------

with Sandbox() as sb:
    repo = sb.repo()
    write_status(sb.state_root, repo, state="alert",
                 reason="Cannot fast-forward master to origin/master (35 commit(s) waiting)")
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("alert record is announced", bool(msg), detail=msg[:120])
    check("alert announcement carries the reason", "Cannot fast-forward" in msg,
          detail=msg[:200])
    check("alert announcement names the repo", str(repo) in msg, detail=msg[:200])

# No record at all is the 'nobody is pulling this' case — exactly the
# marketplace clone's situation before it got a unit. Treating an absent record
# as good news is what kept it invisible for 35 commits.
with Sandbox() as sb:
    repo = sb.repo()
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("a target with no record is announced", str(repo) in msg, detail=msg[:200])
    check("the missing-record wording names auto-pull", "auto-pull" in msg.lower(),
          detail=msg[:200])

with Sandbox() as sb:
    repo = sb.repo()
    now = int(time.time())
    write_status(sb.state_root, repo,
                 checked_at_epoch=now - STALE_S - 60,
                 last_ok_epoch=now - STALE_S - 60)
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("a stale record is announced", str(repo) in msg, detail=msg[:200])

# 'The fetch did not happen' must not read as 'the checkout is current'.
with Sandbox() as sb:
    repo = sb.repo()
    write_status(sb.state_root, repo, state="unverified",
                 reason="git fetch failed 3 time(s) in a row")
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("an unverified record is announced", str(repo) in msg, detail=msg[:200])

with Sandbox() as sb:
    repo = sb.repo()
    d = sb.state_root / str(repo).replace("/", "%")
    d.mkdir(parents=True)
    (d / "status.json").write_text('{"repo": "trunc', encoding="utf-8")
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("an unparseable record is announced, not swallowed", str(repo) in msg,
          detail=msg[:200])

# 'This record carries no timestamp' and 'this record is 55 years old' are
# different facts, and only one of them is true. `float(doc.get(key) or 0)`
# turned a null/absent `checked_at_epoch` into epoch zero, so the hook reported
# "last checked 490000h ago - the puller appears to have stopped running" about
# a record that had simply never carried a timestamp. Right alarm, wrong text,
# and the wrong text sends the reader to journalctl for a unit that is fine.
for label, fields in (("null", {"checked_at_epoch": None}),
                      ("absent", {"checked_at_epoch": "__DELETE__"}),
                      ("empty", {"checked_at_epoch": ""})):
    with Sandbox() as sb:
        repo = sb.repo()
        p = write_status(sb.state_root, repo, **fields)
        if fields["checked_at_epoch"] == "__DELETE__":
            doc = json.loads(p.read_text(encoding="utf-8"))
            doc.pop("checked_at_epoch")
            p.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
        check(f"a {label} timestamp is announced", str(repo) in msg, detail=msg[:200])
        check(f"a {label} timestamp says so rather than inventing an age",
              "no readable timestamp" in msg, detail=msg[:300])
        check(f"a {label} timestamp does not claim the puller stopped",
              "appears to have stopped" not in msg, detail=msg[:300])

# Per-target verdicts, not one aggregate — the message has to say WHICH.
with Sandbox() as sb:
    healthy, sick = sb.repo("healthy"), sb.repo("sick")
    write_status(sb.state_root, healthy)
    write_status(sb.state_root, sick, state="alert", reason="local changes in the way")
    msg = announcement(run_hook(sb.state_root, [healthy, sick])).get("systemMessage", "")
    check("the sick target is named", str(sick) in msg, detail=msg[:200])
    check("the healthy target is not named", str(healthy) not in msg, detail=msg[:200])


# ----------------------------------------------------------------------
section("Target discovery — marketplace clones find themselves")
# ----------------------------------------------------------------------

# A second marketplace must be covered without editing this hook.
# ~/.claude/plugins/marketplaces/* is the set of checkouts the harness resolves
# plugins from; hardcoding today's one would mean the next one is silently
# uncovered, which is the bug rather than the fix.
with Sandbox() as sb:
    claude_home = sb.repo("dot-claude")
    mkt = claude_home / "plugins" / "marketplaces" / "jonathanmoregard"
    (mkt / ".git").mkdir(parents=True)
    write_status(sb.state_root, claude_home)  # the config repo itself is fine
    write_status(sb.state_root, mkt, state="alert", reason="local changes in the way")
    msg = announcement(run_hook(sb.state_root, None, claude_home)).get("systemMessage", "")
    check("a broken marketplace clone is discovered without being listed",
          str(mkt) in msg, detail=msg[:250])

with Sandbox() as sb:
    claude_home = sb.repo("dot-claude")
    mkt = claude_home / "plugins" / "marketplaces" / "jonathanmoregard"
    (mkt / ".git").mkdir(parents=True)
    write_status(sb.state_root, claude_home)
    write_status(sb.state_root, mkt)
    proc = run_hook(sb.state_root, None, claude_home)
    check("a healthy discovered marketplace clone stays silent", proc.stdout == "",
          detail=repr(proc.stdout))

# A clone the puller has been POINTED at but that never reached a verdict is a
# finding: the unit exists and is producing nothing.
with Sandbox() as sb:
    claude_home = sb.repo("dot-claude")
    mkt = claude_home / "plugins" / "marketplaces" / "jonathanmoregard"
    (mkt / ".git").mkdir(parents=True)
    write_status(sb.state_root, claude_home)
    # claude-pull.sh mkdir -p's the state dir at the top of every run, before it
    # can fail at anything — so the bare directory means "pointed at, no verdict".
    (sb.state_root / str(mkt).replace("/", "%")).mkdir(parents=True)
    msg = announcement(run_hook(sb.state_root, None, claude_home)).get("systemMessage", "")
    check("a covered clone with no verdict is announced", str(mkt) in msg,
          detail=msg[:250])

# The coverage filter. Measured 2026-08-31: this machine has six marketplace
# clones, five of them third-party and pinned on purpose. Nothing auto-pulls
# third-party plugin code here and a health check must not make that decision by
# implication — nor spend five permanent lines of the attention budget saying so
# every session.
with Sandbox() as sb:
    claude_home = sb.repo("dot-claude")
    third_party = claude_home / "plugins" / "marketplaces" / "trailofbits"
    (third_party / ".git").mkdir(parents=True)
    write_status(sb.state_root, claude_home)
    proc = run_hook(sb.state_root, None, claude_home)
    check("a pinned third-party clone nothing pulls stays silent",
          proc.stdout == "", detail=repr(proc.stdout))

with Sandbox() as sb:
    claude_home = sb.repo("dot-claude")
    junk = claude_home / "plugins" / "marketplaces" / "scratch"
    junk.mkdir(parents=True)
    write_status(sb.state_root, claude_home)
    proc = run_hook(sb.state_root, None, claude_home)
    check("a non-checkout under marketplaces/ is not a target", proc.stdout == "",
          detail=repr(proc.stdout))


# ----------------------------------------------------------------------
section("Shape and safety")
# ----------------------------------------------------------------------

with Sandbox() as sb:
    repo = sb.repo()
    write_status(sb.state_root, repo, state="alert", reason="diverged")
    out = announcement(run_hook(sb.state_root, [repo]))
    hso = out.get("hookSpecificOutput", {})
    check("announcement declares hookEventName SessionStart",
          hso.get("hookEventName") == "SessionStart", detail=str(hso)[:150])
    check("announcement carries additionalContext",
          str(repo) in hso.get("additionalContext", ""), detail=str(hso)[:250])

# Reasons are assembled from git's stderr, which quotes file names. A repo can
# therefore steer arbitrary text into a session's context via a crafted path.
# It is reported as bounded data, not pasted whole.
with Sandbox() as sb:
    repo = sb.repo()
    write_status(sb.state_root, repo, state="alert", reason="X" * 4000)
    msg = announcement(run_hook(sb.state_root, [repo])).get("systemMessage", "")
    check("a huge reason is truncated", 0 < len(msg) < 2000, detail=f"len={len(msg)}")
    check("the truncated reason is still shown", "XXXX" in msg, detail=msg[:120])

with Sandbox() as sb:
    repo = sb.repo()
    proc = run_hook(sb.root / "no-such-state-root", [repo])
    check("an absent state root does not fail the session", proc.returncode == 0,
          detail=str(proc.returncode))
    check("an absent state root is announced, not treated as healthy",
          bool(proc.stdout.strip()), detail=repr(proc.stdout))

with Sandbox() as sb:
    repo = sb.repo()
    codes = []
    for kwargs in ({}, {"state": "alert"}, {"state": "unverified"}):
        write_status(sb.state_root, repo, **kwargs)
        codes.append(run_hook(sb.state_root, [repo]).returncode)
    check("every path exits 0", codes == [0, 0, 0], detail=str(codes))


# ----------------------------------------------------------------------
section("Summary")
# ----------------------------------------------------------------------
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll checks passed.")
sys.exit(0)

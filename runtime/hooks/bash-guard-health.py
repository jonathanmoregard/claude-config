#!/usr/bin/env python3
"""SessionStart health check for the fail-closed bash guards.

``dcg`` was ``cargo install``-ed into ``~/.local/bin`` on the old Linux Mint
box. The NixOS migration dropped the binary and its config symlink, and
because safe-bash's dcg layer failed open by design, nothing said a word.
The richest deny layer sat inert for months.

This hook is the thing that would have said so. It reports THREE states,
not two:

  1. GUARD DOWN — dcg absent, not executable, its config missing, or the
     ingress vocabulary gone. ``bash-guard.py`` fails closed, so Bash is
     about to be refused wholesale. Announced loudly.
  2. GUARD ARMED ON THE FULL CONFIG — the normal case. SILENT. A health
     check that speaks every session is one nobody reads, so silence is
     load-bearing and is only spent where it has been earned.
  3. GUARD ARMED ON THE BOOTSTRAP FALLBACK — new, and the reason this file
     grew. Announced, but NOT denied. See below.

WHY THE FALLBACK STATE EXISTS AND WHY IT IS INVISIBLE WITHOUT THIS

nixos-config ``home/dcg.nix`` links ``~/.config/dcg/config.toml`` to the
tracked ``~/.claude/dcg.toml``. On a host where that file is absent — a
fresh machine, since ``~/.claude`` is deliberately never auto-cloned —
home-manager activation SEEDS ``home/dcg-fallback.toml`` at the target and
succeeds. That was the right call: hard-failing a first ``nixos-rebuild
switch`` invites commenting the module out, which is a worse silent-off.
But it leaves the guard ARMED ON A REDUCED RULE SET, and the whole design
then rests on the operator being told.

The telling was broken. Activation runs inside
``home-manager-jonathan.service``; ``nixos-rebuild`` does not stream unit
logs, so the "unmissable" banner reaches ``journalctl`` and nobody else. A
seeded host looks fully configured while enforcing fewer rules — the exact
silent-degradation shape this migration exists to remove.

DO NOT TURN THIS STATE INTO A DENY

It is tempting to "harden" case 3 into a refusal, the way ``bash-guard.py``
refuses case 1. Do not. The fallback is armed and working, just reduced;
denying every command on a fresh host would brick the machine at the exact
moment the operator has the fewest tools to dig out, and would recreate the
"comment the module out" pressure that seeding was chosen to avoid.
Announce, do not block. (Exit 2 does nothing on ``SessionStart`` anyway.)

HOW THE STATE IS DETECTED

From dcg's own loader, not a second TOML parser and not a filename guess.
``dcg config --format json`` reports what dcg actually resolved. Two
independent readings of it, because either one alone is a single point of
rot:

  * ``.overrides.block[].reason`` carrying ``BOOTSTRAP FALLBACK``. The seed
    self-announces by contract — nixos-config's VM lane pins it twice
    (``tests/base.nix``, ``dcg_assert_denied_by_user_config(...,
    "BOOTSTRAP FALLBACK")``), and ``home/dcg-fallback.toml`` says that is
    what the reason text is for.
  * the SHAPE of ``.overrides.block[]``, probed by running the loaded
    patterns against one representative command per rule class. If the
    seed is ever reworded, the sentinel goes quiet and this still fires.

Corroborated by a third signal, the cross-repo MARKER FILE that
nixos-config ``home/dcg.nix`` writes on every activation finding the
fallback in force and deletes on the first activation that does not. Its
header names this hook as the reader and fixes the contract:

  * path ``~/.local/state/dcg/fallback-active.json`` (XDG state, and
    deliberately not under ``~/.claude`` — a protected path activation
    could not write and this hook could not read without prompting)
  * absent means "not the fallback", or activation has never run here
  * present means: as of ``observed_at``, ``target`` was byte-identical to
    ``fallback_source``
  * an unrecognised ``schema``, or an unreadable or garbled file, must be
    treated as "fallback POSSIBLY active" and warned about — never as
    "unparseable, therefore fine"

The marker is corroboration, not a substitute: it is a snapshot from the
last activation while dcg's loader answers for right now. When the loader
answers, the LOADER DECIDES. A marker never raises the fallback alarm on its
own: it is quoted as a further signal only when a live reading already says
fallback, and a marker the live reading contradicts — restored file, no
re-activation yet — is stale and says nothing. That last case used to be
announced, and on 2026-11-02 a marker left over from the cutover
(observed 2026-10-29) put "dcg is running the BOOTSTRAP FALLBACK" on every
session start while dcg had all 20 tracked rules loaded. A banner that is
false most of the times it is read is a banner nobody reads the one time it
is true. The dangerous disagreement — no marker, fallback live — is caught
by the live readings regardless.

When the loader cannot be asked (dcg absent, not executable, its config
missing, `dcg config` timing out or unreadable), that is itself announced
under the DOWN headline — see below — and the marker rides along as an
explicitly UNCONFIRMED hint, never as the fact "dcg is running the
fallback".

WHAT THE PATH AND STATUS FIELDS CANNOT DO

Distinguish states 2 and 3. The seed is installed AT the operator's own
path, so ``.config_sources[]`` reports ``loaded`` at
``~/.config/dcg/config.toml`` either way. Only the resolved rule set tells
them apart.

FAIL-CLOSED APPLIES TO THIS CHECK'S OWN ERRORS

Silence must mean "verified", never "could not tell". If ``dcg config``
will not run, will not answer in time, or answers in a shape this file
cannot read, that is announced — under the DOWN headline, because
``bash-guard.py`` is refusing commands in most of those states anyway.

Why REPORT an unobtainable verdict rather than stay quiet, given that this
file's last bug was crying wolf: an unobtainable verdict is not a guess
about the guard, it is a true statement about it. ``bash-guard.py`` asks the
same loader before it trusts a non-deny and fails closed when it cannot, so
"dcg could not be asked" is also "Bash is being refused" — a fault the
operator will hit on the next command, not a phantom. What WAS phantom was
the claim built on top of it from the marker alone, and that claim is now
labelled unconfirmed. The wait is bounded (``DCG_CONFIG_TIMEOUT_S`` inside
``HOOK_BUDGET_S``, inside the harness's 10s), so the report costs at most a
few seconds of session start and can never hang it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

LEGACY_DCG_PATH = "/home/jonathan/.local/bin/dcg"
DEFAULT_DCG_CONFIG = os.path.expanduser("~/.config/dcg/config.toml")
DEFAULT_INGRESS_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bash-guard-config.json"
)

# settings.json gives this hook a 10s harness timeout, and a timed-out hook
# has its output DISCARDED — which for a health check is the same as never
# noticing. Both budgets sit inside it with room to render.
HOOK_BUDGET_S = 8.0
DCG_CONFIG_TIMEOUT_S = 5.0

# Nothing is read past this. The config is a hand-edited pattern list; a
# multi-megabyte file at that path is a fault in itself, not something to
# spend a session-start budget scanning.
CONFIG_SCAN_LIMIT = 512 * 1024

# The seed's self-announcement. Defined once in nixos-config
# home/dcg-fallback.toml, asserted twice by its VM lane, and carried in
# every reason string the fallback emits so that running on it is obvious
# the first time it fires rather than discovered months later.
FALLBACK_SENTINEL = "BOOTSTRAP FALLBACK"

# The cross-repo marker contract, owned by nixos-config home/dcg.nix. The
# path is hard-coded on both sides on purpose: the writer cannot import this
# reader and the reader cannot evaluate that flake, so a literal path both
# repos state in their headers is the only thing that can be checked by
# reading either one.
DEFAULT_FALLBACK_MARKER = os.path.expanduser(
    "~/.local/state/dcg/fallback-active.json"
)
MARKER_SCHEMA = 1
MARKER_STATE = "fallback-active"

_START = time.monotonic()

# Severities. They pick the headline and the footer, and they must not
# collapse into one message: "your guard is refusing everything" and "your
# guard is armed but thinner than you think" call for different actions on
# different timescales.
DOWN = "down"
REDUCED = "reduced"

# Rule classes the tracked ~/.claude/dcg.toml carries and the bootstrap
# fallback does not, each with one representative command. Coverage is
# decided by running dcg's OWN loaded patterns against these strings, so
# rewording a pattern cannot make a class look absent and a class cannot be
# reported lost while a rule for it is live.
#
# example.invalid is reserved by RFC 2606; nothing here is ever executed or
# resolved, these are match subjects only.
FULL_ONLY_CLASSES: list[tuple[str, str]] = [
    (
        "adb device operations (uninstall, pm uninstall/clear, shell rm, "
        "reboot, wipe, settings put, am kill)",
        "adb uninstall com.example.app",
    ),
    (
        "wget destructive methods (--method DELETE|PUT|PATCH)",
        "wget --method=DELETE https://example.invalid/x",
    ),
    (
        "httpie destructive methods (http DELETE|PUT|PATCH)",
        "http DELETE https://example.invalid/x ",
    ),
    (
        "Python requests destructive calls (requests.delete/put/patch)",
        "requests.delete('https://example.invalid/x')",
    ),
    (
        "Python httpx destructive calls (httpx.delete/put/patch)",
        "httpx.delete('https://example.invalid/x')",
    ),
    (
        "POST needs explicit approval (curl, wget, httpie, requests, httpx)",
        "curl -X POST https://example.invalid/x",
    ),
]

# The one class the fallback DOES cover. Reported as still-in-force, so the
# announcement is not misread as "the guard is off".
SHARED_CLASS: tuple[str, str] = (
    "curl -X / --request DELETE|PUT|PATCH",
    "curl -X DELETE https://example.invalid/x",
)

# `dcg config` in its plain text form, for a build that ignores
# `--format json`. Same shape bash-guard.py parses:
#   - user: /home/jonathan/.config/dcg/config.toml [loaded; full]
_TEXT_OUTCOME = re.compile(
    r"^\s*-\s*(system|user|automatic project)\s*:\s*(.*?)\s*"
    r"\[\s*([^\];]+?)\s*;[^\]]*\]\s*(.*)$"
)

# Restoring the tracked file and making dcg pick it up. Named in every
# announcement that has this as its remedy, because "dcg is using a
# fallback config" on its own tells the operator nothing to do.
#
# NEVER name a git checkout in ~/.claude, or a clone over it, as the fix.
# Both were named here until 2026-11-02 and both are dead after the
# runtime-projection cutover: ~/.claude is a symlink farm over a read-only
# bind of the claude-config DEPLOY clone, so it is not a git checkout, and
# ~/Repos/claude-config is a dev tree nothing reads at runtime. An agent
# driven through the live-fallback case said so unprompted — "~/.claude on
# this machine isn't a git repo, so the hook's suggested git checkout fix
# won't work" — which is the whole argument for printing a remedy an
# operator can run rather than one that sounds plausible.
#
# These steps mirror nixos-config home/dcg.nix's `remedy`, the marker's own,
# which its VM lane renders and asserts. The two are deliberately the same
# sequence: an operator who reads both should not have to reconcile them.
# The flat-checkout branch is kept for a host that has not been cut over.
# TWO remedies, because there are two diagnoses and only one of them licenses
# touching the file at the link path.
#
# A single shared string was the 2026-11-02 defect: the seed-collision steps
# were appended to every announcement, including the ones that say dcg could
# not load a config at all. In that state the file is presumed to be the
# OPERATOR'S OWN, and the advice told them to delete it. Reproduced with a
# hand-written config carrying a syntax error, no sentinel and no marker: the
# output said `rm ~/.claude/dcg.toml`. The paragraph below it even began "Not
# the seed". The previous wording was merely dead there; this was destructive,
# which is worse, so the split is not a refinement but the fix.
#
# The test is whether a LIVE reading proved the seed is in force. Only then is
# the file known to be byte-identical to nixos-config's home/dcg-fallback.toml
# and to contain nothing of the operator's.
_SEED_COLLISION_STEPS = (
    "FIX (safe only because a live reading just proved this file IS the seed, "
    "byte-identical to nixos-config's home/dcg-fallback.toml — nothing of "
    "yours is in it): on a projected host (~/.claude/.projection-enabled "
    "exists), FIRST check the projection actually serves the real file — "
    "`head -1 ~/.claude/runtime/dcg.toml` must succeed. If it does not, "
    "repair the projection before going further (`systemctl status "
    "claude-runtime-mounts claude-runtime-deploy`; a dead bind mount is "
    "re-bound by restarting its mount unit, not by the deploy), because "
    "moving the seed aside while the projection is down leaves dcg with NO "
    "user layer at all. Then move it rather than delete it — `mv "
    "~/.claude/dcg.toml ~/dcg.toml.seed-backup` — and `sudo systemctl restart "
    "claude-runtime-links` to put the link back. On a flat ~/.claude "
    "checkout: `git -C ~/.claude checkout -- dcg.toml`. Then `sudo systemctl "
    "restart home-manager-jonathan.service` (or `nixos-rebuild switch`), and "
    "confirm with `dcg config --format json` that the user layer reads "
    "`loaded` and the block list carries your adb / httpie / POST patterns."
)

# Everything else: dcg could not load a usable config, or loaded one this
# check cannot identify. Nothing here has established what that file is, so it
# is treated as the operator's work and the remedy never removes or
# overwrites it.
_LOADER_FAILURE_STEPS = (
    "FIX: this does NOT say your config is the bootstrap seed — it says dcg "
    "could not load a usable rule set — so treat the file at "
    "~/.config/dcg/config.toml as YOURS: do not delete or overwrite it. Copy "
    "it first (`cp -L ~/.config/dcg/config.toml ~/dcg.toml.bak`), then read "
    "what dcg reported above and fix what it names. If that path is a "
    "dangling symlink into ~/.claude/runtime, the projection is the fault "
    "rather than the file — `systemctl status claude-runtime-mounts "
    "claude-runtime-deploy`, and restart the mount unit if its source is "
    "gone. Confirm with `dcg config --format json` that the user layer reads "
    "`loaded` and the block list carries your adb / httpie / POST patterns."
)


def _remaining() -> float:
    return HOOK_BUDGET_S - (time.monotonic() - _START)


def _resolve_dcg() -> str:
    """Same resolution order bash-guard.py uses, so both agree on the path."""
    explicit = os.environ.get("DCG_PATH")
    if explicit:
        return explicit
    found = shutil.which("dcg")
    if found:
        return found
    return LEGACY_DCG_PATH


def _covered_classes(block) -> tuple[set[str], int]:
    """Which rule classes the loaded pattern list actually covers.

    Returns (labels, uninspectable) — the second is a count of entries whose
    pattern could not be compiled here. Python's `re` and dcg's Rust regex
    crate agree on everything these patterns use (\\b, \\s, alternation,
    groups), but an entry this file cannot compile is an entry it cannot
    vouch for, and it is reported rather than silently counted as absent.
    """
    compiled = []
    uninspectable = 0
    for entry in block if isinstance(block, list) else []:
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(pattern, str):
            uninspectable += 1
            continue
        try:
            compiled.append(re.compile(pattern))
        except (re.error, RecursionError, OverflowError):
            uninspectable += 1
    covered = set()
    for label, probe in FULL_ONLY_CLASSES + [SHARED_CLASS]:
        if any(rx.search(probe) for rx in compiled):
            covered.add(label)
    return covered, uninspectable


def _has_sentinel(block) -> bool:
    for entry in block if isinstance(block, list) else []:
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if isinstance(reason, str) and FALLBACK_SENTINEL in reason:
            return True
    return False


def _config_file_has_sentinel(path: str) -> bool | None:
    """Corroboration from the config file itself. None = could not read.

    A substring scan, deliberately not a TOML parse: dcg's loader is the
    authority on what is in force, and a second parser here would be a
    second thing to drift. This only answers "does the file dcg was pointed
    at name itself as the seed".
    """
    try:
        with open(path, "rb") as fh:
            return FALLBACK_SENTINEL.encode() in fh.read(CONFIG_SCAN_LIMIT)
    except OSError:
        return None


def _read_marker(path: str) -> tuple[str, object]:
    """nixos-config's activation marker. ('absent'|'fallback'|'suspect', …).

    `suspect` is anything this reader cannot confidently call healthy —
    unreadable, not JSON, a schema it does not know, a state it does not
    recognise. The contract in home/dcg.nix is explicit that those must
    warn: the marker's whole value is that silence means healthy, so a
    reader allowed to shrug at a file it cannot parse deletes the guarantee.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(CONFIG_SCAN_LIMIT)
    except FileNotFoundError:
        return "absent", ""
    except OSError as exc:
        return "suspect", (
            f"nixos-config's dcg fallback marker at {path} could not be read "
            f"({type(exc).__name__}: {exc}), so this check cannot rule out "
            "that the host is running the bootstrap fallback"
        )
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "suspect", (
            f"nixos-config's dcg fallback marker at {path} does not parse as "
            "JSON. Per its own contract that must be read as 'the fallback "
            "may be active', never as 'unparseable, therefore fine'"
        )
    if not isinstance(doc, dict):
        return "suspect", (
            f"nixos-config's dcg fallback marker at {path} is not a JSON "
            "object, so the fallback cannot be ruled out"
        )
    schema = doc.get("schema")
    if schema != MARKER_SCHEMA:
        return "suspect", (
            f"nixos-config's dcg fallback marker at {path} declares schema "
            f"{schema!r}; this reader knows schema {MARKER_SCHEMA}. Treating "
            "it as 'the fallback may be active' — update "
            "hooks/bash-guard-health.py to the new contract."
        )
    state = doc.get("state")
    if state != MARKER_STATE:
        return "suspect", (
            f"nixos-config's dcg fallback marker at {path} reports state "
            f"{state!r}, which this reader does not recognise (it knows "
            f"{MARKER_STATE!r}), so the fallback cannot be ruled out"
        )
    return "fallback", doc


def _marker_signal(path: str, doc: dict) -> str:
    observed = doc.get("observed_at") or "an unstated time"
    target = doc.get("target") or "(no target recorded)"
    return (
        f"nixos-config's activation marker {path} records the bootstrap "
        f"fallback as active (observed {observed}, target {target})"
    )


def _unconfirmed_marker_hint(path: str, doc: dict) -> tuple[str, str]:
    """The marker, when dcg's loader could not be asked to confirm it.

    Only ever emitted beside a DOWN fault that says why the loader could not
    answer, so it never sets the headline. Worded as a lead to check, not a
    finding: the marker is a snapshot from the last activation, and one left
    over from a cutover is exactly what produced the 2026-11-02 false alarm.
    """
    observed = doc.get("observed_at") or "an unstated time"
    lost = "; ".join(label for label, _ in FULL_ONLY_CLASSES)
    return REDUCED, (
        f"UNCONFIRMED: nixos-config's activation marker {path} records the "
        f"dcg BOOTSTRAP FALLBACK as in force as of {observed}. dcg could not "
        "be asked what it has loaded right now (see above), so this may be "
        "stale — once dcg answers, `dcg config --format json` is the "
        "authority. If the marker is current, these rule classes are NOT IN "
        f"FORCE: {lost}. " + _LOADER_FAILURE_STEPS
    )


def _run_dcg_config(path: str) -> tuple[str, object]:
    """Ask dcg's own loader what it resolved.

    Returns ("json", doc) | ("text", outcomes) | ("unusable", why).

    `dcg config` is a read-only subcommand: measured on 0.12.5 under an
    isolated HOME, three invocations left $HOME/.claude/settings.json
    byte-identical, while ONE bare-`dcg` hook invocation rewrote it. That is
    why this check asks via `config` and never speaks the hook protocol.
    """
    budget = min(DCG_CONFIG_TIMEOUT_S, _remaining() - 0.5)
    if budget <= 0:
        return "unusable", (
            "the health check ran out of its own budget before `dcg config` "
            "could answer"
        )
    try:
        proc = subprocess.run(
            [path, "config", "--format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        return "unusable", (
            f"`dcg config` did not answer within {budget:.1f}s, so the check "
            "could not tell which rule set dcg is running"
        )
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return "unusable", (
            f"`dcg config` could not be invoked: {type(exc).__name__}: {exc}"
        )

    body = (proc.stdout or "").strip()
    if body.startswith("{"):
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("config_sources"), list):
            return "json", doc

    outcomes = []
    for line in (proc.stdout or "").splitlines():
        m = _TEXT_OUTCOME.match(line)
        if m:
            outcomes.append(
                (m.group(1).strip(), m.group(2).strip(), m.group(3).strip().lower(),
                 m.group(4).strip())
            )
    if outcomes:
        return "text", outcomes

    return "unusable", (
        "`dcg config --format json` answered in a shape this check cannot "
        f"read, so it could not tell whether dcg is running the tracked "
        f"pattern list or the bootstrap fallback (binary: {path!r}; stdout: "
        f"{body[:160]!r})"
    )


def _fallback_fault(signals: list[str], missing: list[str],
                    uninspectable: int) -> tuple[str, str]:
    lost = "; ".join(missing) if missing else "(none identified)"
    caveat = (
        f" ({uninspectable} loaded rule(s) could not be inspected here, so "
        "the lost list may be understated.)"
        if uninspectable else ""
    )
    return REDUCED, (
        "dcg is ARMED but running the BOOTSTRAP FALLBACK config, not your "
        "pattern list. Signals: " + "; ".join(signals) + ". That config is "
        "what home-manager activation seeds from nixos-config "
        "home/dcg-fallback.toml when ~/.claude/dcg.toml is absent — a fresh "
        "host, since ~/.claude is never auto-cloned. Activation does print a "
        "banner, but inside home-manager-jonathan.service, and nixos-rebuild "
        "does not stream unit logs, so it reaches journalctl and nobody "
        "else. NOT IN FORCE: " + lost + "." + caveat + " STILL IN FORCE: "
        "dcg's built-in packs (core, system.disk) and the fallback's own "
        "curl -X DELETE|PUT|PATCH rules — which is why nothing looks wrong "
        "from the outside. " + _SEED_COLLISION_STEPS
    )


def _dcg_rule_set_faults(dcg_path: str, cfg_path: str, marker_path: str,
                         marker_state: str,
                         marker_doc: dict) -> list[tuple[str, str]]:
    """States 2 and 3, told apart by what dcg says it loaded."""
    kind, payload = _run_dcg_config(dcg_path)
    marker_says_fallback = marker_state == "fallback"

    if kind == "unusable":
        out: list[tuple[str, str]] = [(DOWN, str(payload))]
        # The loader is silent, so the marker is the only thing left that
        # may know this host was seeded. Pass it on rather than lose it behind
        # the loader's own failure — but as an unconfirmed lead, not a fact.
        if marker_says_fallback:
            out.append(_unconfirmed_marker_hint(marker_path, marker_doc))
        return out

    file_sentinel = _config_file_has_sentinel(cfg_path)

    if kind == "text":
        # A dcg that ignores `--format json`. The resolved rule set is not
        # visible in this format, so the config file stands in for it: the
        # seed names itself in its reason strings, which survive a plain
        # read. Corroboration only — the packaged binary always answers in
        # JSON, so this branch is for older builds and test stubs.
        # `_run_dcg_config` returns ("text", outcomes) exclusively from the
        # branch that built `outcomes` as a non-empty list of 4-tuples; the
        # checker cannot see that through the declared `object`. A crash here
        # would now be REPORTED by the crash handler, not swallowed.
        # arftl-allow: suppression-marker — reasoning directly above.
        outcomes = [o for o in payload if o[0] == "user"]  # type: ignore[union-attr]
        if not outcomes:
            return [(DOWN, (
                "`dcg config` reported no outcome for the USER config layer, "
                "so the check could not tell whether the operator's rules "
                "loaded at all"
            ))]
        _scope, reported_path, state, detail = outcomes[0]
        if state != "loaded":
            return [(DOWN, (
                f"dcg reports its user config layer as {state.upper()} at "
                f"{reported_path!r}"
                + (f" ({detail})" if detail else "")
                + ". Every rule in your [overrides] block is inert, "
                "bash-guard.py fails closed on exactly this, and dcg still "
                "denies `rm -rf /` from a built-in pack — so the guard looks "
                "alive. " + _LOADER_FAILURE_STEPS
            ))]
        if file_sentinel is None:
            return [(DOWN, (
                f"dcg answered `config` in its plain text format, which does "
                f"not expose the loaded rule set, and the config file at "
                f"{cfg_path!r} could not be read either — so the check could "
                "not tell the tracked pattern list from the bootstrap "
                "fallback"
            ))]
        signals = []
        if file_sentinel:
            signals.append(
                f"the config file at {cfg_path!r} names itself as the "
                f"bootstrap seed ({FALLBACK_SENTINEL!r})"
            )
            # Corroboration only: the file read just now is the live
            # reading; a marker it does not back up is stale.
            if marker_says_fallback:
                signals.append(_marker_signal(marker_path, marker_doc))
        if signals:
            return [_fallback_fault(
                signals, [label for label, _ in FULL_ONLY_CLASSES], 0
            )]
        return []

    doc = payload  # JSON
    # `_run_dcg_config` returns ("json", doc) solely from the branch guarded
    # by `isinstance(doc, dict) and isinstance(doc.get("config_sources"),
    # list)`, so both the .get and the list are guaranteed by the producer.
    # arftl-allow: suppression-marker — reasoning directly above.
    sources = doc.get("config_sources") or []  # type: ignore[union-attr]
    loaded = [
        s for s in sources
        if isinstance(s, dict)
        and s.get("authority") == "full"
        and s.get("status") == "loaded"
    ]
    if not loaded:
        full_layers = [
            s for s in sources
            if isinstance(s, dict) and s.get("authority") == "full"
        ]
        states = ", ".join(
            f"{s.get('level')}={s.get('status')}" for s in full_layers
        ) or "(none reported)"
        detail = next(
            (s.get("detail") for s in full_layers if s.get("detail")), None
        )
        return [(DOWN, (
            "dcg loaded NO full-authority config layer — it reports "
            f"{states}"
            + (f" ({str(detail)[:160]})" if detail else "")
            + ". Every rule in your [overrides] block is inert. dcg still "
            "answers every query and still denies `rm -rf /` from a built-in "
            "pack, which is why this cannot be seen from the outside; "
            "bash-guard.py fails closed on it. " + _LOADER_FAILURE_STEPS
        ))]

    faults: list[tuple[str, str]] = []

    # `doc` is the same producer-guaranteed dict as above. The `or {}` already
    # handles a missing or null `overrides`, and `_covered_classes` handles a
    # `block` of any shape, so no value reaches here that the code does not
    # expect.
    # arftl-allow: suppression-marker — reasoning directly above.
    block = (doc.get("overrides") or {}).get("block")  # type: ignore[union-attr]
    covered, uninspectable = _covered_classes(block)
    missing = [label for label, _ in FULL_ONLY_CLASSES if label not in covered]
    full_only_covered = len(FULL_ONLY_CLASSES) - len(missing)

    signals: list[str] = []
    if _has_sentinel(block):
        signals.append(
            f"the rules dcg loaded carry {FALLBACK_SENTINEL!r} in their reason "
            "text, which is how the seed announces itself"
        )
    if file_sentinel:
        signals.append(
            f"the config file at {cfg_path!r} carries the same marker"
        )
    if full_only_covered == 0 and SHARED_CLASS[0] in covered:
        signals.append(
            "none of the rule classes your tracked list carries are in force, "
            "while the fallback's curl rules are — the seed's exact shape"
        )
    # The activation marker corroborates a live reading; it is never one.
    # dcg answered, so what dcg loaded decides: a marker with no live signal
    # behind it is stale (restored file, no re-activation yet — the next
    # activation deletes it) and the rules that guard is enforcing are the
    # full ones. Announcing it anyway was the 2026-11-02 false alarm.
    if signals and marker_says_fallback:
        signals.append(_marker_signal(marker_path, marker_doc))

    if signals:
        faults.append(_fallback_fault(signals, missing, uninspectable))
    elif full_only_covered == 0:
        # Not the seed — the seed at least blocks curl — but not the tracked
        # list either. A loaded config with no destructive-HTTP rule in it is
        # the same silent hole by a different road.
        faults.append((REDUCED, (
            "dcg's user config LOADED, but the rule set it resolved covers "
            "none of the classes your tracked ~/.claude/dcg.toml declares: "
            + "; ".join(missing) + ". This is not the bootstrap fallback "
            "(that one at least blocks curl -X DELETE) — the loaded file "
            "genuinely carries no matching [overrides] block rule. dcg's "
            "built-in packs (core, system.disk) are all that is left. "
            + _LOADER_FAILURE_STEPS
        )))

    # dcg rewriting the operator's permission surface from inside the hook
    # path. Measured on 0.12.5 under an isolated HOME: with self_heal_hook at
    # its upstream default of true, ONE bare-`dcg` invocation rewrites
    # $HOME/.claude/settings.json, inserting a "Bash|PowerShell" PreToolUse
    # entry at index 0 — AHEAD of the real guard — and re-serialises the
    # file. It embeds the binary's absolute path, so every store-path change
    # re-triggers it and hand-reverting does not stick. It fired on the live
    # host on 2026-08-24. Announced, not denied: the guard still evaluates
    # correctly, it is the settings file that is at risk.
    # Same producer-guaranteed dict. `is True` is deliberate: any other value,
    # including a non-dict `general`, leaves the announcement unmade rather
    # than guessed.
    # arftl-allow: suppression-marker — reasoning directly above.
    if (doc.get("general") or {}).get("self_heal_hook") is True:  # type: ignore[union-attr]
        faults.append((REDUCED, (
            "dcg's `self_heal_hook` is ENABLED (upstream's default). In that "
            "state one bare-`dcg` invocation — one per Bash tool call, since "
            "bash-guard.py consults it that way — rewrites "
            "~/.claude/settings.json, inserting dcg's own PreToolUse entry "
            "at index 0 ahead of the real guard and re-serialising the whole "
            "file. Set `self_heal_hook = false` under [general] in the config "
            f"dcg loaded ({cfg_path!r}). Nothing is lost by disabling it: it "
            "only re-registers a direct dcg hook we never had and do not "
            "want."
        )))

    return faults


def _faults() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    dcg = _resolve_dcg()
    dcg_usable = True
    if not os.path.exists(dcg):
        dcg_usable = False
        out.append((DOWN, (
            f"dcg is ABSENT (looked at {dcg}). bash-guard.py fails closed, so "
            "every Bash command will be denied until dcg is back on PATH. It "
            "is packaged declaratively in nixos-config — check that the "
            "deploy landed."
        )))
    elif not os.access(dcg, os.X_OK):
        dcg_usable = False
        out.append((DOWN, (
            f"dcg at {dcg} exists but is NOT EXECUTABLE, so every Bash "
            "command will be denied until its mode is fixed."
        )))

    dcg_config = os.environ.get("DCG_CONFIG") or DEFAULT_DCG_CONFIG
    config_present = os.path.exists(dcg_config)
    if not config_present:
        out.append((DOWN, (
            f"dcg's config is MISSING at {dcg_config}. dcg would run with none "
            "of the operator's block rules — the same silent inertness that "
            "hid its disappearance last time. It is generated from the "
            "repo-tracked ~/.claude/dcg.toml."
        )))

    marker_path = os.environ.get("DCG_FALLBACK_MARKER") or DEFAULT_FALLBACK_MARKER
    marker_state, marker_payload = _read_marker(marker_path)
    if marker_state == "suspect":
        out.append((REDUCED, str(marker_payload)))
    marker_doc = marker_payload if isinstance(marker_payload, dict) else {}

    # Only worth asking which rule set is loaded once we know there is a
    # binary to ask and a file for it to have loaded. Otherwise the answer
    # adds nothing the faults above have not already said.
    if dcg_usable and config_present:
        out.extend(_dcg_rule_set_faults(
            dcg, dcg_config, marker_path, marker_state, marker_doc
        ))
    elif marker_state == "fallback":
        # No loader answer available — the DOWN fault above says why — so
        # the marker is passed on, labelled as the unconfirmed lead it is.
        out.append(_unconfirmed_marker_hint(marker_path, marker_doc))

    ingress = os.environ.get("BASH_GUARD_CONFIG") or DEFAULT_INGRESS_CONFIG
    if not os.path.exists(ingress):
        out.append((DOWN, (
            f"the untrusted-ingress config bash-guard-config.json is MISSING at "
            f"{ingress}. bash-output-guard.py fails closed, so it will WITHHOLD "
            "the output of every Bash command until the file is restored."
        )))

    return out


# True once a report has been written to stdout. The crash handler checks it
# so a late failure cannot append a second JSON object to a report already
# sent — two objects on stdout parse as neither.
_EMITTED = False


def _report(message: str, context: str) -> None:
    """Write the SessionStart report. Marks stdout as spoken for."""
    global _EMITTED
    _EMITTED = True
    json.dump(
        {
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        },
        sys.stdout,
    )
    sys.stdout.flush()


def main() -> int:
    # arftl-allow: swallowed-exception — stdin is drained and DISCARDED; every
    # fault this check reports is read from dcg and the filesystem, never from
    # the payload, so failing to read it cannot change a verdict. The read
    # exists only to spare the harness a broken pipe, and a broken pipe is the
    # very thing this would be reporting.
    try:
        sys.stdin.read()
    # arftl-allow: swallowed-exception — reasoning directly above.
    except (OSError, ValueError): pass

    try:
        faults = _faults()
    except OSError as exc:
        faults = [(DOWN, f"the health check itself could not inspect the "
                         f"filesystem: {exc}")]

    if not faults:
        return 0

    # Severity picks the framing. Getting this wrong in either direction
    # costs the check its readership: claiming commands are denied when they
    # are not trains the operator to ignore it, and under-stating a downed
    # guard is how the last outage lasted four months.
    down = [text for sev, text in faults if sev == DOWN]
    if down:
        head = "BASH GUARD HEALTH: the fail-closed bash guards are not fully armed."
        foot = (
            "Until this is fixed, Bash commands are denied or their output "
            "withheld. You can still run a command yourself in the prompt "
            "with `! <command>`."
        )
    else:
        head = (
            "BASH GUARD HEALTH: the guards are ARMED but running a REDUCED "
            "rule set."
        )
        foot = (
            "This is an announcement, not a refusal — dcg is still evaluating "
            "every Bash command, just against fewer rules than you think."
        )

    bullets = "\n".join(f"  - {text}" for _sev, text in faults)
    _report(
        f"{head}\n{bullets}\n{foot}",
        "Guard health at session start: "
        + " ".join(text for _sev, text in faults),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _exc:
        # A broken health check must not be the thing that breaks startup —
        # but it must not be silent either. Exiting 0 with nothing on stdout
        # was indistinguishable from "the guards are healthy", which is the
        # precise failure mode this whole file exists to report on: the last
        # outage lasted four months because a dead layer produced no symptom.
        # A check that dies now SAYS it died, and says the health of the
        # guards is therefore unknown rather than good.
        if not _EMITTED:
            try:
                _report(
                    "BASH GUARD HEALTH: the health check itself CRASHED "
                    f"({type(_exc).__name__}: {str(_exc)[:200]}), so the state "
                    "of the bash guards at this session's start is UNKNOWN — "
                    "not confirmed good. Run "
                    "`python3 ~/.claude/hooks/bash-guard-health.py </dev/null` "
                    "to see the failure, and `dcg config --format json` to "
                    "check the guard directly.",
                    "The bash-guard health check crashed; guard health is "
                    "unknown, not verified.",
                )
            except BaseException:
                # arftl-allow: swallowed-exception — the reporting of a crash
                # must not itself break session startup. stdout is already
                # unwritable in this branch, so there is no channel left and
                # nothing a raise could reach.
                pass
        sys.exit(0)

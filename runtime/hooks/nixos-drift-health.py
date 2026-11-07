#!/usr/bin/env python3
"""SessionStart check: tell the session about imperative-install drift.

WHY

Four times in four months something scheduled on this host was installed
with a stateful command instead of a nix declaration — `crontab -e`, a
hand-written unit under ~/.config/systemd/user, a copy into ~/.local/bin —
and stopped silently on the next `nixos-rebuild switch` (the
``installCrontab`` activation rewrites the live crontab from
nixos-config). The RSI daily reviewer was dead for four months that way.

nixos-config's ``nixos-drift-analyzer`` (home/drift-analyzer.nix) already
detects these classes hourly and deterministically, but publishes them to a
markdown file nobody opens, and its failure path is a desktop toast. The
agent that installed the thing — or the next one — is the party that can
declare it, so this hook states the fact at SessionStart, in the session's
own context. Same shape as claude-pull-health.py.

SCOPE

Only the headings listed in ``CLASSES`` and ``FAILURES`` below speak, and both
dicts are allowlists — an analyzer heading absent from them never reaches a
session. The analyzer reports other invariants too (e.g. several worktrees
attached to nixos-config's ``main``); those stay in the report file. A check
that speaks every session about something no session is going to fix is one
nobody reads.

``CLASSES`` is state that a rebuild loses or cannot reproduce; the fix is a
declaration. ``FAILURES`` is the VSCodium merge machinery reporting that it
broke, where nothing drifted but the declaration may not have been applied.

SILENCE IS THE BUDGET, BUT ONLY WHEN EARNED

Silent when the latest successful run found nothing in scope. On a host that
runs the analyzer (its state dir exists), a missing, unparseable or stale
heartbeat speaks as UNVERIFIED — silence must mean "checked and clean".
A host without the analyzer at all stays silent (non-NixOS dotfiles reuse).

Every path exits 0: a broken health check must not break startup.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Hourly timer, Persistent=true, on a laptop that suspends overnight. Twelve
# hours keeps a normal night's sleep from crying wolf on the first session
# after wake (same reasoning as claude-pull-health.py).
STALE_S = 12 * 3600
MAX_ITEMS = 8

# Report headings (home/drift-analyzer.nix) that are imperative installs of
# things that should be declared, mapped to where the declaration belongs.
CLASSES: dict[str, str] = {
    "Installed crontab differs from declaration":
        "live crontab != home.file.\".config/crontab\" in nixos-config "
        "home/jonathan-linux.nix; the next rebuild overwrites the live one "
        "(diff: `crontab -l` vs ~/.config/crontab)",
    "Installed crontab is unavailable": "crontab -l failed",
    "Declarative crontab is missing": "~/.config/crontab is absent",
    "Unmanaged entries in ~/.config/systemd/user":
        "not written by home-manager; declare as systemd.user.services/"
        "timers in nixos-config (e.g. home/claude-services.nix)",
    "Entries outside /nix/store in ~/.local/bin":
        "not nix-managed; declare via home.packages / home.file in nixos-config",
    "Imperative nix-env packages":
        "installed with nix-env; declare in home.packages or "
        "environment.systemPackages",
    "VSCodium security settings live but not declared":
        "live-only keys that decide what is allowed to run or what gets "
        "verified (security.*, task.allowAutomaticTasks, git.allow*, "
        "telemetry.*, chat.tools.autoApprove); the merge PRESERVES them, so "
        "a value set once outlasts every deploy — declare it in nixos-config "
        "home/vscodium.nix so the declared value wins, or clear it in the UI",
    "VSCodium settings live but not declared":
        "set in VSCodium's UI and kept by the merge in nixos-config "
        "home/vscodium.nix (declared wins, undeclared survives reboot and "
        "deploy); lost on a fresh install until promoted into "
        "programs.vscodium.profiles.default.userSettings",
    "VSCodium settings overwritten by the declared config":
        "edited live and reverted at activation because home/vscodium.nix "
        "declares them and declared wins; change the declaration in "
        "nixos-config if the live value is the wanted one",
    "Codium-only skills live but not declared":
        "skills written straight into ~/.local/state/claude-codium/skills, "
        "which only editor-started sessions read. They work immediately and "
        "survive every deploy, so nothing distinguishes them from reviewed "
        "config until a fresh install drops them — promote into nixos-config "
        "home/claude-codium/skills",
    "Codium skills shadowing a shared skill of the same name":
        "a real directory there BEATS the inherited skill of the same name, "
        "because activation leaves it alone rather than relinking; the live "
        "copy is what editor sessions get and the shared one is unreachable "
        "from them — rename or drop the live copy, do not promote it",
    "Codium session notes live but not declared":
        "~/.local/state/claude-codium/local.md has notes beyond its seed. It "
        "is imported by the Codium CLAUDE.md and needs no PR, so it is where "
        "mid-session findings land; promote the durable ones into nixos-config "
        "home/claude-codium/CLAUDE.md",
}

# Headings that mean the VSCodium settings machinery itself failed. Kept apart
# from CLASSES because the sentence is a different one: nothing drifted, and
# the declared settings may not have been applied at all. home/vscodium.nix
# raises a desktop toast on the activation failure, but a toast is gone by the
# next session — which is the reason this hook exists at all.
#
# Their report bodies are multi-line key/value blocks ending in a prose
# paragraph, so no item list is echoed: the heading and hint carry the whole
# message and the report path follows it.
FAILURES: dict[str, str] = {
    "VSCodium settings merge failed at activation":
        "declared settings were NOT applied and the live file was left as it "
        "was (journalctl --user -u home-manager-$USER.service)",
    "VSCodium settings file does not parse":
        "the live settings.json has a syntax error; the file was left in "
        "place and no merge can apply until it is fixed",
    "VSCodium drift recorder failed during activation":
        "settings were left untouched, but no override was recorded that "
        "activation, so the overwritten-key list is incomplete",
    "VSCodium settings could not be compared":
        "the live-vs-declared diff did not run, so undeclared keys are "
        "unreported for this cycle",
}


def _dirs() -> tuple[Path, Path]:
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    return data / "nixos-drift-analyzer", state / "nixos-drift-analyzer"


def _heartbeat(path: Path) -> tuple[float | None, int | None]:
    """(age in seconds or None if unusable, finding count or None if absent)."""
    fields = dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
    )
    ts = datetime.fromisoformat(fields["timestamp"].strip())
    count = int(fields["findings"]) if "findings" in fields else None
    if ts.tzinfo is None:
        return None, count
    return time.time() - ts.timestamp(), count


def _sections(report: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for chunk in report.split("\n## ")[1:]:
        heading, _, rest = chunk.partition("\n")
        parts = rest.split("~~~")
        if len(parts) < 3:
            continue
        lines = parts[1].splitlines()[1:]  # drop the "text" fence tag line
        out[heading.strip()] = [l for l in lines if l.strip()]
    return out


def _emit(message: str) -> None:
    json.dump(
        {
            "systemMessage": message,
            # Factual, not imperative: the agent needs the fact; what to do
            # follows from it.
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "NixOS drift at session start:\n" + message,
            },
        },
        sys.stdout,
    )
    sys.stdout.flush()


def main() -> int:
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass

    data_dir, state_dir = _dirs()
    if not state_dir.is_dir():
        return 0  # analyzer not deployed on this host

    journal = "journalctl --user -u nixos-drift-analyzer.service"
    try:
        age, expected = _heartbeat(state_dir / "last-success")
    except (OSError, KeyError, ValueError):
        age, expected = None, None
    if age is None or age > STALE_S:
        why = ("no readable heartbeat" if age is None
               else f"last successful run {age / 3600:.0f}h ago")
        _emit(f"NixOS drift status UNVERIFIED — {why} "
              f"(threshold {STALE_S // 3600}h). Journal: {journal}")
        return 0

    try:
        sections = _sections((data_dir / "latest.md").read_text())
    except (OSError, UnicodeDecodeError):
        _emit(f"NixOS drift status UNVERIFIED — report unreadable: "
              f"{data_dir / 'latest.md'}")
        return 0
    # Producer and consumer live in different repos. If the report format
    # drifts, parsing nothing must not pass for "checked and clean".
    if expected is not None and len(sections) < expected:
        _emit(f"NixOS drift status UNVERIFIED — heartbeat reports {expected} "
              f"finding(s) but {len(sections)} parsed from "
              f"{data_dir / 'latest.md'} (report format changed?)")
        return 0

    blocks: list[str] = []

    broken = [f"  - {heading} — {hint}"
              for heading, hint in FAILURES.items() if heading in sections]
    if broken:
        blocks.append(
            "The VSCodium declared-settings merge reported a failure:\n"
            + "\n".join(broken)
        )

    lines: list[str] = []
    for heading, hint in CLASSES.items():
        if heading not in sections:
            continue
        items = sections[heading]
        shown = ", ".join(items[:MAX_ITEMS])
        more = f" (+{len(items) - MAX_ITEMS} more)" if len(items) > MAX_ITEMS else ""
        lines.append(f"  - {heading}: {shown}{more} — {hint}")
    if lines:
        blocks.append(
            "Imperatively installed state that nixos-config does not declare "
            "(lost or unreproducible on rebuild):\n" + "\n".join(lines)
        )

    if not blocks:
        return 0

    _emit("\n".join(blocks) + f"\nFull report: {data_dir / 'latest.md'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)

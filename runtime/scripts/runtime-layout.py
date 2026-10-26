#!/usr/bin/env python3
"""Move the runtime subset of this repository under `runtime/`.

WHY. `~/.claude` is both the live harness config and this repository, so
`tests/` (229 files), `docs/`, `experiments/` and `research/` sit inside the
directory the agent reads, and nothing in the platform fences them off: deny
rules cover the built-in file tools and the Bash file commands the harness
recognises, not arbitrary subprocesses, and the sandbox covers shell commands
only -- hooks, local MCP servers and the file tools run outside it.

The plan is to move this repository to `~/Repos/claude-config` and project only
the runtime subset back into `~/.claude` through read-only bind mounts. This
script performs the repository half of that: the partition, and nothing else.
It touches no live path, writes no mount unit, and is safe to run in a
worktree.

WHAT PROPERTY THAT BUYS. Controlled runtime deployment: everything the harness
loads on its own initiative resolves under `~/.claude`, and the dev-only trees
are not among the paths projected there. It is **not** filesystem isolation --
the checkout still exists at its own path, and anything that names that path
reads `tests/` exactly as before. Say the first; the second is false.

THE MANIFEST IS THE POINT. The partition below is the review surface, so it is
an explicit list and the script **fails on anything it does not recognise**. A
top-level entry added later therefore stops this script until somebody decides
which side it belongs on, which is the whole mechanism: a silent default is how
`tests/` would end up projected.

    runtime-layout.py --plan     print the partition and the planned moves
    runtime-layout.py --apply    perform them with `git mv`
    runtime-layout.py --verify   check an already-built layout

Exit 0 success, 1 a verify failure, 2 usage or an unclassified entry.
"""

from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
from pathlib import Path

RUNTIME_DIR = "runtime"

# Projected into `~/.claude` read-only. Every entry here is reachable from a
# registered entrypoint, a skill, a declared command, or the user's own hand
# (`~/.claude/snap`, `~/.claude/ticktick-login`).
PROJECTED = (
    # directories
    "agents", "bin", "commands", "dev-container", "ecc", "egress_guard",
    "hooks", "mcp-configs", "mcps", "permission-ledger",
    "recursive-self-improvement", "scripts", "skills", "templates", "todo",
    "tools", "wellbeing", "workers",
    # configuration the harness reads
    "CLAUDE.md", ".claude-powerline.json", "claude-powerline.json", "dcg.toml",
    "egress-sinks.json", "mcp-servers.json", "settings.json",
    "settings-patch.json", "upstream-drift.json",
    # loose entrypoints, each named by a hook or run by hand
    "aggregator-recall-hook.py", "aggregator-transcript-guard.py",
    "context-baggage.py", "context_usage.py", "continue-nudge-backtest.py",
    "continue-nudge-classifier.py", "continue-nudge-detector.py",
    "continue-nudge-harvester.py", "continue-nudge-silver-labeler.py",
    "date-check.sh", "date-reminder-hook.py", "date-reset.sh", "fable-warn.sh",
    "gcal-auth.py", "gcal-check.py", "inplace_reset.py",
    "nixos-reminder-hook.sh", "no-tell-run.py", "push-proposals.sh",
    "self-compact-trigger.py", "snap", "statusline-wrapper.sh",
    "sync-agent.sh", "ticktick-login", "worktree-additional-dirs.py",
)

# Never projected. The agent runtime has no business reaching these, and the
# whole mission is that it cannot.
DEV_ONLY = (
    "tests", "docs", "experiments", "research", "improvements",
    # Candidate sandbox policies, measured against history by
    # scripts/sandbox-backcheck.py. Inputs to a decision, never loaded by the
    # harness — a policy the agent could read is a policy it could edit.
    "policy",
    ".github", ".githooks", ".gitleaks.toml", ".secrets.baseline",
)

# Stays at the repository root and is not part of `runtime/`.
#
# `vendor/` is the interesting one: two git submodules, and only four subpaths
# inside them are projected (`superpowers/{hooks,skills,package.json}` and
# `everything-claude-code/skills`), each by its own bind mount at
# `~/.claude/vendor/...`. The rest -- a nested `.claude/` with its own skills,
# rules and commands, plus `tests/`, `examples/`, `src/` and some 455 further
# instruction files -- stays out of the namespace entirely. A gitlink cannot be
# moved under `runtime/` without rewriting `.gitmodules` for no gain, and it
# must not be: the projection has to be per-subpath.
ROOT_ONLY = (".gitignore", ".gitmodules", "vendor")

# `settings.json` is projected, but **how** is still open: it is written at
# runtime (the live model pin is a tracked modification right now), so either
# `~/.claude/settings.json` is a real file that `claude-pull` syncs from here,
# or the read-only projection needs a hole. Either way it does not change this
# script -- the canonical copy belongs with the rest of the configuration.
WRITTEN_AT_RUNTIME = ("settings.json",)

# Where the mount geometry shows up in this script. `runtime/` sits exactly one
# level below the repository root, and the mounted view reproduces that: a
# symlink at `runtime/skills/x` pointing `../../vendor/y` resolves to
# `<repo>/vendor/y` on disk and to `~/.claude/vendor/y` under the mounts, which
# is where the four vendor binds are. So one extra `..` is the whole fix for
# the 33-entry skill farm, and it is correct on both sides at once.
#
# A relative target that escapes the repository cannot have that property --
# `skills/review-inbox -> ../../Repos/router-agent/...` would resolve to
# `~/Repos/router-agent/...` on disk and `~/router-agent/...` under the mounts.
# Those links must become absolute.
HOME = "/home/jonathan"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout


def tracked_entries(root: Path) -> dict[str, str]:
    """Every tracked entry name -> where it currently sits.

    `runtime/` is transparent here, on purpose. The manifest has to keep
    deciding entries *after* the move, or the first file added post-cutover
    lands wherever it was dropped and the review surface is gone again. So an
    entry inside `runtime/` is reported under its own name with location
    `runtime`, and `runtime` itself is never an entry.
    """
    where: dict[str, str] = {}
    for line in git("ls-files", cwd=root).splitlines():
        if not line:
            continue
        if line == RUNTIME_DIR or line.startswith(RUNTIME_DIR + "/"):
            rest = line[len(RUNTIME_DIR) + 1:]
            if not rest:
                continue
            where[rest.split("/", 1)[0]] = RUNTIME_DIR
        else:
            where.setdefault(line.split("/", 1)[0], "root")
    return dict(sorted(where.items()))


def symlinks(root: Path) -> list[tuple[str, str]]:
    """(path, target) for every tracked symlink, read from the index."""
    out = []
    for line in git("ls-files", "-s", cwd=root).splitlines():
        if not line.startswith("120000"):
            continue
        path = line.split("\t", 1)[1]
        out.append((path, git("cat-file", "-p", f":{path}", cwd=root).strip()))
    return out


def classify(root: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    """(projected, dev_only, root_only, unclassified) over tracked entries."""
    entries = tracked_entries(root)
    known = set(PROJECTED) | set(DEV_ONLY) | set(ROOT_ONLY)
    return (
        [e for e in entries if e in set(PROJECTED)],
        [e for e in entries if e in set(DEV_ONLY)],
        [e for e in entries if e in set(ROOT_ONLY)],
        [e for e in entries if e not in known],
    )


def misplaced(root: Path) -> list[str]:
    """Entries sitting on the wrong side of the partition."""
    where = tracked_entries(root)
    wrong = [e for e in DEV_ONLY if where.get(e) == RUNTIME_DIR]
    wrong += [e for e in ROOT_ONLY if where.get(e) == RUNTIME_DIR]
    return wrong


def retarget(path: str, target: str) -> str | None:
    """The target `path` needs once it has moved one level deeper.

    `None` when it does not change, which is the common case and the one that
    is easy to get wrong. What decides it is whether the *other end* of the
    link moves too:

      skills/x -> ../hooks/y     both ends are projected, so both descend into
                                 runtime/ together and the hop is unchanged.
                                 Adding a `..` here points at <repo>/hooks/y,
                                 which nothing will occupy.
      skills/x -> ../vendor/y    vendor/ stays at the repository root, so the
                                 link has one more level to climb. `../../`
                                 is then right on disk *and* under the mounts,
                                 because runtime/ sits exactly one level down
                                 and the mount reproduces that.

    A hop that stays inside the moved entry moves with it. Absolute targets
    resolve the same from any depth. A relative target that leaves the
    repository has no spelling that is right in both geometries, so it becomes
    absolute.
    """
    if target.startswith("/"):
        return None
    entry = path.split("/", 1)[0]
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == entry or resolved.startswith(entry + "/"):
        return None
    if resolved.startswith(".."):
        return posixpath.normpath(posixpath.join(HOME, resolved.lstrip("./")))
    if resolved.split("/", 1)[0] in set(PROJECTED):
        return None
    return "../" + target


def plan(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """(moves, symlink rewrites) for the projected entries."""
    projected, _, _, unclassified = classify(root)
    if unclassified:
        print(
            "runtime-layout: unclassified top-level entries, refusing to move "
            "anything:\n  " + "\n  ".join(unclassified) +
            "\n\nAdd each to PROJECTED, DEV_ONLY or ROOT_ONLY in this script. "
            "Deciding that in review is the point of the manifest.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    where = tracked_entries(root)
    pending = [e for e in projected if where.get(e) == "root"]
    moves = [(e, f"{RUNTIME_DIR}/{e}") for e in pending]
    rewrites = []
    for path, target in symlinks(root):
        if path.split("/", 1)[0] not in set(pending):
            continue
        new = retarget(path, target)
        if new is not None:
            rewrites.append((f"{RUNTIME_DIR}/{path}", target, new))
    return moves, rewrites


def print_plan(root: Path) -> int:
    projected, dev_only, root_only, _ = classify(root)
    moves, rewrites = plan(root)
    print(f"runtime-layout: {len(projected)} entries -> {RUNTIME_DIR}/, "
          f"{len(dev_only)} dev-only stay put, {len(root_only)} at the root\n")
    print("dev-only, never projected:")
    for entry in dev_only:
        print(f"  {entry}")
    print("\nat the repository root, outside runtime/:")
    for entry in root_only:
        print(f"  {entry}")
    print(f"\nmoves ({len(moves)}):")
    for src, dst in moves:
        flag = "  [written at runtime]" if src in WRITTEN_AT_RUNTIME else ""
        print(f"  {src} -> {dst}{flag}")
    print(f"\nsymlink retargets ({len(rewrites)}):")
    for path, old, new in rewrites:
        print(f"  {path}\n      {old}\n   -> {new}")
    return 0


def apply(root: Path) -> int:
    moves, rewrites = plan(root)
    (root / RUNTIME_DIR).mkdir(exist_ok=True)
    for src, dst in moves:
        git("mv", src, dst, cwd=root)
    for path, _, new in rewrites:
        target = root / path
        target.unlink()
        target.symlink_to(new)
        git("add", path, cwd=root)
    print(f"runtime-layout: moved {len(moves)} entries, "
          f"retargeted {len(rewrites)} symlinks")
    return verify(root)


def verify(root: Path) -> int:
    """Check a built layout, from the mount's point of view."""
    failures: list[str] = []
    runtime = root / RUNTIME_DIR
    if not runtime.is_dir():
        print(f"runtime-layout: no {RUNTIME_DIR}/ to verify", file=sys.stderr)
        return 1

    where = tracked_entries(root)
    top = {entry for entry, place in where.items() if place == RUNTIME_DIR}

    for entry in misplaced(root):
        side = "must never be" if entry in DEV_ONLY else "belongs at the repository root"
        failures.append(f"{entry} is inside {RUNTIME_DIR}/ and {side}")
    _, _, _, unclassified = classify(root)
    if unclassified:
        failures.append("unclassified: " + ", ".join(unclassified))

    # Two separate questions per symlink, and an earlier version asked only the
    # first while reporting as though it had asked both:
    #
    #   where does the target point?  a path outside the projection cannot
    #                                 resolve under the mounts at all;
    #   is anything there?            a link into a projected path can still
    #                                 dangle, and a dangling skill is a skill
    #                                 that silently stops loading.
    #
    # Existence is not always answerable here, so the third outcome is named
    # rather than folded into a pass: an absolute target is machine-specific,
    # and the vendored submodules are empty in every `git worktree add` tree
    # because that command does not initialise them. Those are reported and
    # counted, never counted as verified.
    allowed_outside = (
        "vendor/superpowers/hooks", "vendor/superpowers/skills",
        "vendor/superpowers/package.json", "vendor/everything-claude-code/skills",
    )
    checked = 0
    unverifiable: list[str] = []
    for line in git("ls-files", "-s", RUNTIME_DIR, cwd=root).splitlines():
        if not line.startswith("120000"):
            continue
        path = line.split("\t", 1)[1]
        target = git("cat-file", "-p", f":{path}", cwd=root).strip()
        exists = (root / path).exists()  # follows the link; False if dangling

        if target.startswith("/"):
            if not exists:
                unverifiable.append(f"{path} -> {target} (absolute, absent here)")
            else:
                checked += 1
            continue

        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(path), target))
        in_runtime = resolved == RUNTIME_DIR or resolved.startswith(RUNTIME_DIR + "/")
        vendored = next((a for a in allowed_outside
                         if resolved == a or resolved.startswith(a + "/")), None)
        if not in_runtime and not vendored:
            failures.append(
                f"{path} -> {target} leaves {RUNTIME_DIR}/ for {resolved}, which "
                "is not a projected path")
            continue
        if exists:
            checked += 1
            continue
        if vendored:
            submodule = root / "/".join(vendored.split("/")[:2])
            uninitialised = not submodule.is_dir() or not any(submodule.iterdir())
            if uninitialised:
                unverifiable.append(
                    f"{path} -> {target} ({submodule.name} is not initialised)")
                continue
        failures.append(f"{path} -> {target} dangles: nothing at {resolved}")

    # Only entries this repository actually tracks -- the manifest lists what
    # is projected *if present*, so a tree with no `wellbeing/` is not broken.
    not_moved = [entry for entry, place in where.items()
                 if place == "root" and entry in set(PROJECTED)]
    if not_moved:
        failures.append("still at the root: " + ", ".join(not_moved))

    if failures:
        print(f"runtime-layout: {len(failures)} problem(s)", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    # Say what was actually established. The earlier wording claimed "no
    # unresolvable symlink" on the strength of a containment check alone, which
    # passed a link pointing at a target that did not exist.
    print(f"runtime-layout: {RUNTIME_DIR}/ holds {len(top)} projected entries, "
          f"no dev-only tree; {checked} symlink(s) resolve to something that "
          f"exists, {len(unverifiable)} not checkable here")
    for note in unverifiable:
        print(f"  unchecked: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args(argv)

    root = args.repo_root
    if root is None:
        try:
            root = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("runtime-layout: not a git repository", file=sys.stderr)
            return 2
    root = root.resolve()

    if args.apply:
        return apply(root)
    if args.verify:
        return verify(root)
    return print_plan(root)


if __name__ == "__main__":
    sys.exit(main())

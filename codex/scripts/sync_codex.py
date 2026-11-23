#!/usr/bin/env python3
"""Import ~/.claude, render it into a stage, check the stage, and publish
only what passed.

The output names absolute paths under HOME (hook commands, wrappers, role
files, link targets), so it is rendered for HOME but written elsewhere: the
render and the check run under common.staged_output, where every write and
every read of the output lands in a fresh stage directory (common.located)
while every path the output names stays the one under HOME. The stage is
seeded with what the render merges and the check reads from outside the
mirror (seed_stage).

Nothing writes to HOME before the check has passed: a failed check, an
error, or a stop signal or kill before publish_stage leaves the home as it
was. (SIGTERM, SIGHUP or SIGKILL leaves the stage behind in TMPDIR.) Then
publish_stage copies the managed paths from the stage into HOME, entry by
entry and each atomically, with no rollback: a run killed during the copy
can leave a mix of files from the previous render and this one, each from a
render whose check passed, which the next run repairs. Codex writes runtime
state into config.toml; a write between seed_stage and the copy is lost.

Production (/etc/nixos/home/ai-client-config-sync.nix) runs this inside a
mount namespace whose HOME is a staged home, and rsyncs the managed paths
into the live home only after it exits 0; there, this run's copy lands in
that staged home.

One direct run at a time per home: two copies at once could interleave into
a mix of files neither run checked, while both report success.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

import check
import common
import import_claude
import render_codex
from common import AGENTS_HOME, CODEX, PLUGIN_ROOT


# The home itself, locked as a directory: a lock file would be left behind
# in a home that held nothing before a failed run.
LOCK = common.HOME
# EX_TEMPFAIL: another run holds the home; this one changed nothing.
BUSY = 75
# What the render merges, carried into the stage as the production wrapper
# carries it into its staged home.
MERGE_INPUTS = (
    CODEX / "config.toml",
    CODEX / "rules" / "default.rules",
    AGENTS_HOME / "plugins" / "marketplace.json",
)
# What Codex reads first and what names the rest: copied once everything
# they name is in place, and before the prune, as the production publish
# orders its two rsync passes.
ENTRY_POINTS = (
    CODEX / "config.toml",
    CODEX / "hooks.json",
    AGENTS_HOME / "plugins" / "marketplace.json",
    PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
    PLUGIN_ROOT / ".mcp.json",
    PLUGIN_ROOT / "hooks.json",
)


def main() -> int:
    lock = os.open(LOCK, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"ai-client-config sync: another sync holds a lock on {LOCK}; nothing changed",
                file=sys.stderr,
            )
            return BUSY
        return sync()
    finally:
        os.close(lock)


def sync() -> int:
    import_claude.main()
    with tempfile.TemporaryDirectory(prefix="codex-sync-stage-") as stage_name:
        stage = Path(stage_name)
        seed_stage(stage)
        with common.staged_output(stage):
            render_codex.main()
            passed = check.main() == 0
        if not passed:
            print("ai-client-config sync: check failed; nothing was published")
            return 1
        publish_stage(stage)
    return 0


def seed_stage(stage: Path) -> None:
    """Copy into stage the merge inputs, and the profiles the check floors
    (~/.codex/<name>.config.toml, outside the mirror), from the home. A
    regular file only, a link followed: anything else at a merge input is
    the mirror's to replace, and no profile Codex can load."""
    for path in (*MERGE_INPUTS, *sorted(CODEX.glob("*.config.toml"))):
        if path.is_file():
            target = common.in_stage(path, stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def publish_stage(stage: Path) -> None:
    """Make the home's managed paths what they are in stage: files by bytes
    and executability, links by target, with the modes the production
    publish leaves (copy_into_home). What
    render_codex.never_pruned_paths names is never pruned: the hook
    generations live sessions may run are only added to (what a killed run
    left among them aside, render_codex.left_in_hook_generations), and
    Codex's bundled skills, which no stage holds, are left alone."""
    refuse_hook_collisions(stage)
    for root in render_codex.managed_paths():
        copy_into_home(root, stage, skip=ENTRY_POINTS)
    for path in ENTRY_POINTS:
        copy_into_home(path, stage)
    for root in render_codex.managed_paths():
        prune_home(root, stage)
        for leftover in render_codex.beside_managed_path(root):
            common.remove_path(leftover)
    for leftover in render_codex.left_in_hook_generations():
        common.remove_path(leftover)


def refuse_hook_collisions(stage: Path) -> None:
    """A home hook file that still matches its address yet differs from the
    stage's is a real digest collision with a generation a session may run:
    refuse, before anything is copied. One that no longer matches its
    address was damaged after publication and no session can rely on it:
    the copy rewrites it."""
    root = render_codex.HOOK_WRAPPER_ROOT
    staged_root = common.in_stage(root, stage)
    for staged in sorted(staged_root.rglob("*")):
        path = root / staged.relative_to(staged_root)
        if (
            staged.is_file()
            and path.is_file()
            and path.read_bytes() != staged.read_bytes()
            and render_codex.hook_file_matches_its_address(path)
        ):
            raise ValueError(f"content-addressed hook collision: {path}")


def copy_into_home(path: Path, stage: Path, skip: tuple[Path, ...] = ()) -> None:
    """Put at path what stage holds there, recursively: add and replace,
    each entry atomically (common.replace_entry), never remove.

    Modes as the production publish's rsync -rltE (no -p) leaves them: a
    new entry takes the stage's under the umask; an existing directory or
    file keeps the home's, but for a file's executability, which follows
    the stage's. A private directory or file stays private.
    """
    if path in skip:
        return
    staged = common.in_stage(path, stage)
    if staged.is_dir() and not staged.is_symlink():
        if not (path.is_dir() and not path.is_symlink()):
            mode = stat.S_IMODE(staged.stat().st_mode)
            common.replace_entry(path, lambda entry: entry.mkdir(mode))
        for name in sorted(os.listdir(staged)):
            copy_into_home(path / name, stage, skip)
    elif staged.is_symlink():
        target = os.readlink(staged)
        if not (path.is_symlink() and os.readlink(path) == target):
            common.replace_entry(path, lambda entry: entry.symlink_to(target))
    elif staged.exists():
        copy_file_into_home(path, staged)


def copy_file_into_home(path: Path, staged: Path) -> None:
    staged_mode = stat.S_IMODE(staged.stat().st_mode)
    kept: int | None = None
    if path.is_file() and not path.is_symlink():
        kept = with_executability(stat.S_IMODE(path.stat().st_mode), staged_mode)
        if path.read_bytes() == staged.read_bytes():
            if stat.S_IMODE(path.stat().st_mode) != kept:
                path.chmod(kept)
            return

    def build(entry: Path) -> None:
        # open(2) puts a new file's mode under the umask, as rsync does.
        fd = os.open(entry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, staged_mode & 0o777)
        with staged.open("rb") as source, os.fdopen(fd, "wb") as target:
            shutil.copyfileobj(source, target)
        if kept is not None:
            entry.chmod(kept)

    common.replace_entry(path, build)


def with_executability(mode: int, source: int) -> int:
    """mode, made executable or not as source is, the way rsync -E changes
    an existing file: x turned on wherever r is, or every x turned off."""
    if bool(mode & 0o111) == bool(source & 0o111):
        return mode
    if source & 0o111:
        return mode | (mode & 0o444) >> 2
    return mode & ~0o111


def prune_home(path: Path, stage: Path) -> None:
    """Remove what the home holds at or under path that stage does not,
    never a never-pruned path or what holds one."""
    kept = render_codex.never_pruned_paths()
    if path in kept:
        return
    staged = common.in_stage(path, stage)
    if path.is_dir() and not path.is_symlink() and (
        (staged.is_dir() and not staged.is_symlink())
        or any(path in other.parents for other in kept)
    ):
        for name in sorted(os.listdir(path)):
            prune_home(path / name, stage)
    elif (path.exists() or path.is_symlink()) and not (staged.exists() or staged.is_symlink()):
        common.remove_path(path)


if __name__ == "__main__":
    raise SystemExit(main())

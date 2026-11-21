"""The one path canonicaliser the brokers put every input path through."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

ROOTS = ("Repos", "worktrees")
MAX_PATH_CHARS = 4096
# systemd splits and unquotes path-list properties (BindPaths=, ...) and
# expands % specifiers, so a path is used only when nothing in it is special.
_PLAIN_PATH_RE = re.compile(r"/[A-Za-z0-9._@+/-]*")
_KINDS = {"dir": stat.S_ISDIR, "file": stat.S_ISREG}


def roots(home: Path) -> list[Path]:
    return [Path(os.path.realpath(home / r)) for r in ROOTS]


# git takes a directory for a git dir only if it holds HEAD itself (setup.c,
# is_git_directory); objects and refs may live elsewhere through commondir, and
# HEAD may be a symlink. So whatever holds a HEAD entry counts.
def is_git_dir(path: Path | str, dir_fd: int | None = None) -> bool:
    try:
        os.stat(os.path.join(path, "HEAD"), dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def canonical(raw: object, home: Path, kind: str = "dir", git_ok: bool = False) -> tuple:
    """(real path, "dev:ino") of RAW, a KIND under ~/Repos or ~/worktrees.

    The last component is not followed: a symlink there is refused, so the
    inode returned is the one a later bind mount must still find. Unless
    GIT_OK, a path in or at a git directory is refused, bare ones included.
    """
    if not isinstance(raw, str) or not os.path.isabs(raw) or "\0" in raw \
            or len(raw) > MAX_PATH_CHARS or ".." in raw.split("/"):
        raise RuntimeError(f"{raw!r} must be an absolute path without '..'")
    path = Path(raw)
    real = Path(os.path.realpath(path.parent)) / path.name
    try:
        st = os.lstat(real)
    except OSError:
        raise RuntimeError(f"{path} does not exist") from None
    if not _PLAIN_PATH_RE.fullmatch(str(real)):
        raise RuntimeError(f"{str(real)!r} has characters other than [A-Za-z0-9._@+/-]")
    if not _KINDS[kind](st.st_mode):
        raise RuntimeError(f"{path} is not a {'directory' if kind == 'dir' else 'file'}"
                           " (a symlink is refused)")
    root = next((r for r in roots(home) if r in real.parents), None)
    if root is None:
        raise RuntimeError(f"{path} is not inside ~/Repos or ~/worktrees")
    if not git_ok and (".git" in path.parts or ".git" in real.parts or any(
            is_git_dir(p) for p in (real, *real.parents) if root in p.parents)):
        raise RuntimeError(f"{path} is inside a git directory")
    return real, f"{st.st_dev}:{st.st_ino}"


# The manager resolves Bind*Paths= only when a unit starts, so a directory
# swapped for a symlink after canonical() checked it would be bound instead.
# A unit's first process therefore proves each bound path is the inode that was
# checked, from inside its own mount namespace, before anything else runs.
# Arguments: STAT PATH DEV:INO ... -- REST, STAT an absolute path so the
# unit's PATH cannot pick it; the caller defines `refuse`.
VERIFY_BINDS = r'''
stat_bin=$1; shift
while [ "$1" != -- ]; do
  if [ "$("$stat_bin" -c %d:%i -- "$1")" != "$2" ]; then
    echo "$1 is not the directory that was checked; not run"
    refuse
  fi
  shift 2
done
shift
'''


def verify_binds_args(stat_bin: str, pairs) -> list:
    if not os.path.isabs(stat_bin):
        raise RuntimeError(f"stat must be an absolute path: {stat_bin!r}")
    return [stat_bin, *(a for p, ident in pairs for a in (str(p), ident)), "--"]

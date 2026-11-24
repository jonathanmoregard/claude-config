"""Read a file from an agent's run dir on the host, safely.

check.sh runs outside the agent sandbox and the run dir is the agent's to
write, so a grader reads run content only as a regular file reached without a
symlink at any step below the run dir: a planted link (to ~/.ssh, say) or
FIFO is refused, never followed or blocked on. What the agent cannot cause is
not graded as its FAIL: a missing or unreadable run dir and a host I/O fault
raise OSError, a REL that leaves the run dir raises ValueError. Standard
library only, so it works from the tests/agent-e2e tree the e2e broker
extracts.
"""
import errno
import os
from pathlib import Path, PurePosixPath
import stat


class Refused(Exception):
    """Run content the grader will not read; the run grades FAIL."""


# Errors the agent can cause from inside its run dir: a symlink at any step, a
# file where a directory belongs, a socket, a mode it took away. Any other
# OSError is the host's fault (EIO, EMFILE, ENOMEM) and propagates unchanged.
_AGENT_CAUSED = {errno.ELOOP: 'a symlink', errno.ENOTDIR: 'not a directory',
                 errno.ENXIO: 'not a regular file', errno.EACCES: 'unreadable'}
_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def read_run(top: Path, rel: str, limit: int = -1) -> bytes | None:
    """REL under TOP, at most LIMIT bytes, or None when the agent left it absent."""
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or {'..', '.'} & set(parts):
        raise ValueError(f'{rel!r} is not a path below the run dir')
    fd = os.open(top, _FLAGS | os.O_DIRECTORY)  # the harness made it: any error is the host's
    try:
        for part in parts:
            fd, parent = os.open(part, _FLAGS, dir_fd=fd), fd
            os.close(parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{top.name}/{rel}: not a regular file")
        with open(fd, "rb", closefd=False) as f:
            return f.read(limit)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno not in _AGENT_CAUSED:
            raise
        raise Refused(f"{top.name}/{rel}: {_AGENT_CAUSED[e.errno]}") from None
    finally:
        os.close(fd)

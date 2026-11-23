"""Read a file from an agent's run dir on the host, safely.

check.sh runs outside the agent sandbox and the run dir is the agent's to
write, so a grader reads run content only as a regular file reached without a
symlink at any step: a planted link (to ~/.ssh, say) or FIFO is refused, never
followed or blocked on. Standard library only, so it works from the
tests/agent-e2e tree the e2e broker extracts. helper_lang.read_run is the same
walk inside a grader that also needs the runtime layout.
"""
import errno
import os
from pathlib import Path, PurePosixPath
import stat


class Refused(Exception):
    """Run content the grader will not read; the run grades FAIL."""


def read_run(top: Path, rel: str, limit: int = -1) -> bytes | None:
    """REL under TOP, at most LIMIT bytes, or None when it is absent."""
    fd = None
    try:
        for part in (top, *PurePosixPath(rel).parts):
            fd, parent = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=fd), fd
            if parent is not None:
                os.close(parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{top.name}/{rel}: not a regular file")
        with open(fd, "rb", closefd=False) as f:
            return f.read(limit)
    except FileNotFoundError:
        return None
    except OSError as e:
        why = "a symlink" if e.errno == errno.ELOOP else e.strerror
        raise Refused(f"{top.name}/{rel}: {why}") from None
    finally:
        if fd is not None:
            os.close(fd)

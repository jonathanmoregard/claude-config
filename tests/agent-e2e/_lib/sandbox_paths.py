"""Map a path as the agent saw it to the host path behind it.

Under bubblewrap the agent's HOME is the real HOME's own path with the run's
home/ mounted there, while check.sh runs on the host, where that path is the
real HOME. A path the agent logged or named is therefore not a host path, and
comparing it with RUN/home, or reading it as the real HOME, grades the wrong
file. The harness hands check.sh the run's mounts in E2E_MOUNTS (a JSON list
of [inside, host] pairs in mount order, host null for a private tmpfs);
`to_host` replays them. Without bwrap the variable is unset and every path
maps to itself, which is then exactly right: the agent's HOME was RUN/home.

    sys.path.insert(0, "<scenario>/../_lib"); from sandbox_paths import to_host
"""

from __future__ import annotations

import json
import os


def mounts() -> list[tuple[str, str | None]]:
    return [(inside, host) for inside, host in json.loads(os.environ.get("E2E_MOUNTS") or "[]")]


def _under(path: str, mount: str) -> bool:
    return mount == "/" or path == mount or path.startswith(mount + "/")


def to_host(path: str) -> str | None:
    """The host path the agent's PATH named, or None when it named a file only
    the sandbox had (its private /tmp). PATH must be absolute."""
    p = os.path.normpath(path)
    for inside, host in reversed(mounts()):
        if _under(p, inside):
            if host is None:
                return None
            rel = os.path.relpath(p, inside)
            return host if rel == "." else os.path.join(host, rel)
    return p

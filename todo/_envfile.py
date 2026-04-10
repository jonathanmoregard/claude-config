"""Read and update the ``~/.config/todo/env`` file (``KEY=VALUE`` lines).

Used both to hydrate ``os.environ`` on startup and to persist rotated
credentials (e.g. a refreshed TickTick access token) back to disk.
"""
from __future__ import annotations

import os
import tempfile
from typing import Mapping

ENV_FILE_PATH = os.path.expanduser("~/.config/todo/env")


def load(path: str = ENV_FILE_PATH) -> None:
    """Hydrate ``os.environ`` from the env file without overwriting existing vars."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(
                key.strip(), value.strip().strip('"').strip("'")
            )


def update(updates: Mapping[str, str], path: str = ENV_FILE_PATH) -> None:
    """Persist ``updates`` into the env file and ``os.environ``.

    Existing keys are replaced in place; unknown keys are appended. The write
    is atomic (temp file + ``os.replace``) and the file is chmod 600.
    """
    remaining = dict(updates)
    lines_out: list[str] = []

    if os.path.exists(path):
        with open(path) as f:
            for raw in f:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in remaining:
                        lines_out.append(f"{key}={remaining.pop(key)}")
                        continue
                lines_out.append(line)

    for key, value in remaining.items():
        lines_out.append(f"{key}={value}")

    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".env.", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines_out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    for key, value in updates.items():
        os.environ[key] = value

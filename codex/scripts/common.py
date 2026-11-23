from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable


HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
CLAUDE = HOME / ".claude"
CLAUDE_STATE = HOME / ".claude.json"
CODEX = HOME / ".codex"
AGENTS_HOME = HOME / ".agents"
PLUGIN_NAME = "claude-mirror"
PLUGIN_ROOT = HOME / "plugins" / PLUGIN_NAME
LEGACY_PLUGIN_LINK = CODEX / "plugins" / PLUGIN_NAME
IMPORT_DIR = REPO / "imports" / "claude-live"
MANIFEST = IMPORT_DIR / "manifest.json"
REPORT_DIR = CODEX / "claude-setup-mirror"

RESERVED_SKILL_NAMES = {
    "imagegen",
    "openai-docs",
    "plugin-creator",
    "skill-creator",
    "skill-installer",
}

SKIP_DIR_NAMES = {
    ".git",
    ".in_use",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    "cache",
    "backups",
    "_backups",
    "file-history",
    "session-env",
    "sessions",
    "tasks",
    "transcript-backups",
    "usage-data",
    "debug",
    "telemetry",
    "tmp",
}

SKIP_SUFFIXES = {
    ".pyc",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".db",
    ".lock",
    ".log",
}

SECRET_KEY_PARTS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


def mkdir(path: Path) -> None:
    assert_not_claude_write(path)
    path.mkdir(parents=True, exist_ok=True)


def assert_not_claude_write(path: Path) -> None:
    candidate = path.expanduser()
    resolved = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    claude = CLAUDE.resolve(strict=False)
    if resolved == claude or claude in resolved.parents:
        raise RuntimeError(f"refusing to write under read-only Claude config: {path}")
    parent = resolved.parent.resolve(strict=False)
    if parent == claude or claude in parent.parents:
        raise RuntimeError(f"refusing to write under read-only Claude config: {path}")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def default_created_file_mode() -> int:
    """Return the mode open(2) would produce for a regular file."""
    # No portable, race-free API exposes the process umask. Procfs avoids
    # mutating process-wide state; platforms without it fail private.
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("Umask:"):
                return 0o666 & ~int(line.split(":", 1)[1].strip(), 8)
    except (OSError, ValueError):
        return 0o600
    return 0o600


def write_file(path: Path, text: str, mode: int | None = None) -> bool:
    assert_not_claude_write(path)
    mkdir(path.parent)
    existing_mode: int | None = None
    if path.exists() and not path.is_symlink():
        existing_mode = path.stat().st_mode & 0o7777
        try:
            if path.read_text(encoding="utf-8") == text:
                if mode is not None:
                    path.chmod(mode)
                return False
        except UnicodeDecodeError:
            # arftl-allow: swallowed-exception (a non-UTF-8 file cannot equal the text; fall through and rewrite it)
            pass
    desired_mode = (
        mode
        if mode is not None
        else existing_mode
        if existing_mode is not None
        else default_created_file_mode()
    )
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            temporary.chmod(desired_mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def remove_path(path: Path) -> None:
    assert_not_claude_write(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def entry_state(path: Path) -> Any:
    """What path holds, without following links: None when missing; a file
    by bytes and mode, a directory by mode and children, a link by target."""
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if path.is_dir():
        return (
            "directory",
            path.stat().st_mode & 0o7777,
            {child.name: entry_state(child) for child in sorted(path.iterdir())},
        )
    if path.exists():
        return ("file", path.read_bytes(), path.stat().st_mode & 0o7777)
    return None


def replace_entry(dst: Path, build: Callable[[Path], object]) -> bool:
    """Put the entry build makes at dst, never removing dst first, and
    leave dst untouched when it already holds exactly that. Returns whether
    dst changed.

    build makes the entry at the path it is given, beside dst, which is then
    renamed over dst: atomic for a file or a link over a file or a link. A
    directory on either side cannot be renamed over, so what stands at dst
    moves aside first; dst is missing only between those two renames.
    """
    assert_not_claude_write(dst)
    mkdir(dst.parent)
    holder = Path(tempfile.mkdtemp(prefix=f".{dst.name}.swap-", dir=dst.parent))
    try:
        staged = holder / "entry"
        build(staged)
        if entry_state(staged) == entry_state(dst):
            return False

        def real_dir(entry: Path) -> bool:
            return entry.is_dir() and not entry.is_symlink()

        if (dst.exists() or dst.is_symlink()) and (real_dir(dst) or real_dir(staged)):
            os.rename(dst, holder / "aside")
            os.rename(staged, dst)
        else:
            os.replace(staged, dst)
        return True
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def copy_tree_clean(src: Path, dst: Path) -> None:
    """dst becomes a copy of src, whatever else it held, swapped in whole."""
    replace_entry(dst, lambda staged: shutil.copytree(src, staged, ignore=ignore_generated))


def link_or_copy(src: Path, dst: Path) -> str:
    assert_not_claude_write(dst)
    if dst.is_symlink() and Path(os.readlink(dst)) == src:
        return "already-linked"
    how = "symlink"

    def build(staged: Path) -> None:
        nonlocal how
        try:
            staged.symlink_to(src, target_is_directory=src.is_dir())
        except OSError:
            if src.is_dir():
                shutil.copytree(src, staged, ignore=ignore_generated)
                how = "copytree"
            else:
                shutil.copy2(src, staged)
                how = "copy"

    replace_entry(dst, build)
    return how


def ignore_generated(dir_name: str, entries: list[str]) -> set[str]:
    ignored: set[str] = set()
    for entry in entries:
        path = Path(dir_name) / entry
        if entry in SKIP_DIR_NAMES or path.suffix in SKIP_SUFFIXES:
            ignored.add(entry)
    return ignored


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def frontmatter_value(path: Path, key: str, default: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return default
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return default
    for i, line in enumerate(lines[1:80], start=1):
        if line.strip() == "---":
            break
        if line.startswith(key + ":"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value in {">", "|", ">-", "|-", ">+", "|+"}:
                block: list[str] = []
                for continuation in lines[i + 1 :]:
                    if continuation.strip() == "---":
                        break
                    if continuation.startswith((" ", "\t")):
                        block.append(continuation.strip())
                    elif continuation.strip():
                        break
                value = " ".join(block)
            return value or default
    return default


def yaml_string(value: str) -> str:
    value = " ".join(value.split()) or "Mirrored AI skill."
    return json.dumps(value)


def toml_string(value: str) -> str:
    # Not json.dumps: it escapes non-BMP characters as surrogate pairs, which
    # TOML rejects, and passes DEL through, which TOML forbids unescaped.
    escaped = []
    for char in value:
        if char in '"\\':
            escaped.append("\\" + char)
        elif char < " " or char == "\x7f":
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return toml_string(str(value))


def toml_array(values: list[Any]) -> str:
    return "[" + ", ".join(toml_scalar(v) for v in values) + "]"

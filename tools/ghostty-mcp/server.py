#!/usr/bin/env python3
"""Ghostty terminal MCP server - opens repos in Ghostty splits with Claude Code"""

import json
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DBUS_DEST = "com.mitchellh.ghostty"
WINDOW_PATH = "/com/mitchellh/ghostty/window/1"
MIN_SPLIT_WIDTH = 900  # px — below this, open new tab instead of splitting

mcp = FastMCP("ghostty")


def dbus_activate(action: str, params: str = "[]") -> None:
    subprocess.run(
        [
            "gdbus", "call",
            "--session",
            "--dest", DBUS_DEST,
            "--object-path", WINDOW_PATH,
            "--method", "org.gtk.Actions.Activate",
            action, params, "{}",
        ],
        capture_output=True,
        check=False,
    )


def get_active_window_id() -> str | None:
    result = subprocess.run(
        ["xdotool", "getactivewindow"],
        capture_output=True,
        text=True,
    )
    wid = result.stdout.strip()
    return wid or None


def get_window_width(window_id: str) -> int | None:
    result = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("WIDTH="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def type_in_active(command: str, settle: float) -> None:
    time.sleep(settle)
    window_id = get_active_window_id()
    if not window_id:
        return
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "10",
         "--window", window_id, command],
        check=False,
    )
    subprocess.run(
        ["xdotool", "key", "--window", window_id, "Return"],
        check=False,
    )


def _open_area(cmd: str) -> str:
    """Split current pane if wide enough; otherwise open new tab."""
    active = get_active_window_id()
    width = get_window_width(active) if active else None

    if width is not None and width >= MIN_SPLIT_WIDTH * 2:
        dbus_activate("split-right")
        placement = f"split right (pane width was {width}px)"
        settle = 0.8
    else:
        dbus_activate("new-tab")
        placement = f"new tab (pane width {width}px below {MIN_SPLIT_WIDTH * 2}px split threshold)"
        settle = 1.2

    type_in_active(cmd, settle=settle)
    return placement


@mcp.tool()
def find_project(name: str) -> str:
    """
    Find a project directory by name (case-insensitive, fuzzy).
    Searches common project roots under ~ (case-insensitive), plus ~ top-level.
    Returns a JSON list of matching paths, best match first.
    """
    import difflib

    home = Path.home()
    root_names = {"projects", "src", "code", "dev", "repos"}
    search_roots: list[Path] = [home]
    try:
        for child in home.iterdir():
            if child.is_dir() and child.name.lower() in root_names:
                search_roots.append(child)
    except PermissionError:
        pass

    candidates: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        depth = 1 if root == home else 2
        _collect(root, name.lower(), depth, candidates)

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    def score(p: Path) -> float:
        return difflib.SequenceMatcher(None, name.lower(), p.name.lower()).ratio()

    unique.sort(key=score, reverse=True)
    return json.dumps([str(p) for p in unique])


def _collect(root: Path, name_lower: str, max_depth: int, out: list[Path], depth: int = 1) -> None:
    try:
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if name_lower in child.name.lower():
                out.append(child)
            if depth < max_depth:
                _collect(child, name_lower, max_depth, out, depth + 1)
    except PermissionError:
        pass


@mcp.tool()
def open_claude_session(dir: str, session_id: str = "") -> str:
    """
    Open a Ghostty split/tab running Claude Code in the given directory.
    Splits the current pane if wide enough; otherwise opens a new tab.
    Runs 'claude --continue <session_id>' if session_id is provided,
    otherwise 'claude --continue' (resumes most recent session in that dir).
    """
    repo = Path(dir).expanduser().resolve()
    if not repo.exists():
        return f"Error: path does not exist: {dir}"

    session_arg = f" {session_id}" if session_id else ""
    cmd = f"cd {repo} && claude --continue{session_arg}"
    placement = _open_area(cmd)
    return f"Opened {repo}{' session=' + session_id if session_id else ''} — {placement}"


@mcp.tool()
def get_layout() -> str:
    """Report current active Ghostty window geometry (no tracked state)."""
    active = get_active_window_id()
    if not active:
        return json.dumps({"error": "no active window"})
    width = get_window_width(active)
    return json.dumps({"active_window": active, "width": width})


@mcp.tool()
def reset_layout() -> str:
    """No-op — layout tracking removed. Placement now queries live window state."""
    return "No-op: layout tracking removed. Placement derives from active window width at call time."


if __name__ == "__main__":
    mcp.run()

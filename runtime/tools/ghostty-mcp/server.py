#!/usr/bin/env python3
"""Ghostty terminal MCP server - opens repos in Ghostty splits/tabs with Claude Code.

Single-window model: assumes one Ghostty window. Counts existing surfaces via
the ghostty process tree and alternates split-right / new-tab:
  odd count  -> split-right (fills out current tab)
  even count -> new-tab (starts a fresh tab)
"""

import json
import re
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DBUS_DEST = "com.mitchellh.ghostty"
DBUS_WINDOW_ROOT = "/com/mitchellh/ghostty/window"

mcp = FastMCP("ghostty")


def _run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def dbus_activate(window_node: str, action: str, params: str = "[]") -> None:
    _run([
        "gdbus", "call", "--session",
        "--dest", DBUS_DEST,
        "--object-path", f"{DBUS_WINDOW_ROOT}/{window_node}",
        "--method", "org.gtk.Actions.Activate",
        action, params, "{}",
    ])


def get_ghostty_window_nodes() -> list[str]:
    """Return dbus window node names (e.g. ['1', '4']) via introspection."""
    result = _run([
        "gdbus", "introspect", "--session",
        "--dest", DBUS_DEST,
        "--object-path", DBUS_WINDOW_ROOT,
    ])
    return re.findall(r"node\s+(\d+)\s*\{", result.stdout)


def get_ghostty_pid() -> int | None:
    result = _run(["pgrep", "-x", "ghostty"])
    pids = [int(p) for p in result.stdout.split() if p.strip()]
    return pids[0] if pids else None


def count_surfaces(ghostty_pid: int) -> int:
    """Count surfaces across the Ghostty instance by counting direct shell children."""
    result = _run(["pgrep", "-P", str(ghostty_pid)])
    return len([p for p in result.stdout.split() if p.strip()])


def get_ghostty_x11_wid() -> str | None:
    """Return the currently active Ghostty X11 window id, or first Ghostty window as fallback."""
    active = _run(["xdotool", "getactivewindow"]).stdout.strip()
    if active:
        wm_class = _run(["xdotool", "getwindowclassname", active]).stdout.strip().lower()
        if "ghostty" in wm_class:
            return active

    # Fallback: first managed Ghostty window from wmctrl.
    result = _run(["wmctrl", "-lx"])
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 3 and "ghostty" in parts[2].lower():
            wid_hex = parts[0]
            try:
                return str(int(wid_hex, 16))
            except ValueError:
                continue
    return None


def focus_window(wid: str) -> None:
    _run(["wmctrl", "-ia", hex(int(wid))])


def type_in_window(wid: str, command: str) -> None:
    _run(["xdotool", "type", "--window", wid, "--delay", "10", command])
    _run(["xdotool", "key", "--window", wid, "Return"])


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
    Open a Ghostty split or tab running Claude Code in the given directory.

    Alternates: odd surface count -> split-right, even -> new-tab.
    Assumes a single Ghostty window. Targets the first dbus window node and
    focuses the matching X11 window before typing the command.
    """
    repo = Path(dir).expanduser().resolve()
    if not repo.exists():
        return f"Error: path does not exist: {dir}"

    nodes = get_ghostty_window_nodes()
    if not nodes:
        return "Error: no Ghostty windows found on dbus. Is Ghostty running?"
    window_node = nodes[0]

    ghostty_pid = get_ghostty_pid()
    if ghostty_pid is None:
        return "Error: ghostty process not found."
    surfaces = count_surfaces(ghostty_pid)

    action = "split-right" if surfaces % 2 == 1 else "new-tab"
    settle = 0.5 if action == "split-right" else 1.0

    wid = get_ghostty_x11_wid()
    if wid is None:
        return "Error: no Ghostty X11 window visible to WM."
    focus_window(wid)
    time.sleep(0.15)

    dbus_activate(window_node, action)
    time.sleep(settle)

    session_arg = f" {session_id}" if session_id else ""
    cmd = f"cd {repo} && claude --continue{session_arg}"
    type_in_window(wid, cmd)

    return (
        f"Opened {repo}{' session=' + session_id if session_id else ''} "
        f"via {action} (surfaces_before={surfaces}, window_node={window_node}, wid={wid})"
    )


@mcp.tool()
def get_layout() -> str:
    """Report current Ghostty window/surface state via dbus + process tree."""
    nodes = get_ghostty_window_nodes()
    ghostty_pid = get_ghostty_pid()
    surfaces = count_surfaces(ghostty_pid) if ghostty_pid else 0
    wid = get_ghostty_x11_wid()
    return json.dumps({
        "window_nodes": nodes,
        "ghostty_pid": ghostty_pid,
        "surfaces_total": surfaces,
        "x11_wid": wid,
    })


@mcp.tool()
def reset_layout() -> str:
    """No-op — placement derives from live state each call."""
    return "No-op: placement derives from live dbus + process state."


if __name__ == "__main__":
    mcp.run()

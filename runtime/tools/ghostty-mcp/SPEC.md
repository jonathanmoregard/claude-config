# Ghostty MCP Spec

## Ghostty API Reality

### D-Bus (`com.mitchellh.ghostty`)

**App-level** (`/com/mitchellh/ghostty`):
- `new-window`, `new-window-command` (accepts argv array `as`), `reload-config`, `quit`

**Window-level** (`/com/mitchellh/ghostty/window/<N>`):
- `split-right`, `split-left`, `split-up`, `split-down`
- `new-tab`, `close-tab`
- `copy`, `paste`, `clear`, `reset`
- `prompt-surface-title`, `prompt-tab-title`

**Key limits:**
- All actions return `()` — void, no IDs
- No pane enumeration, no tab listing, no state queries
- Split/tab fires on currently-focused surface only
- Window nodes enumerable via introspect: `gdbus introspect ... /com/mitchellh/ghostty/window` → child nodes (numeric IDs e.g. `1`, `4`)

### CLI (`ghostty +<action>`)
- `+new-window` — spawn window
- `+list-actions`, `+show-config`, `+list-keybinds`, `+list-themes` — config introspection only
- No `+list-windows`, no `+list-tabs`, no runtime state

### Systemd scopes
Each surface gets: `app-ghostty-surface-transient-<SHELL_PID>.scope`  
Enumerate all panes: `systemctl --user list-units "app-ghostty-surface-transient-*.scope"`

### Journal logs
`journalctl --user _COMM=ghostty` emits surface lifecycle events:
- `info(surface): surface closed addr=<zig_ptr>` — addresses are internal, not externally addressable
- OSC/resize events logged

### Shell env vars (exposed to child shells)
- `TERM=xterm-ghostty`
- `GHOSTTY_SHELL_FEATURES=cursor:blink,path,title`
- `GHOSTTY_RESOURCES_DIR`, `GHOSTTY_BIN_DIR`
- No surface/tab/pane ID exposed

### What cannot be done via Ghostty API
- Enumerate panes/tabs
- Get pane IDs
- Target specific pane for action
- Query pane CWD, title, focus state
- Focus arbitrary pane programmatically

### Community / forks
- No Linux IPC forks exist
- PR #8069 (merged Jul 2025): gtk-ng IPC infra — single-instance actions only
- PR #11762 (open): surface IDs for IPC — watch
- macOS-only wrappers exist (AppleScript), useless on Linux

---

## Architecture

### State sources (ranked by reliability)
1. `systemctl --user list-units "app-ghostty-surface-transient-*.scope"` — live pane PIDs
2. `/proc/<pid>/cwd` — live CWD per pane (walk scope_pid → sh → zsh)
3. `~/.claude/tools/ghostty-mcp/panes/<pid>.cwd` — shell-hook markers
4. `state.json` — {pid → {project, tab_idx, pane_seq, opened_at}}

### Shell hook (add to zshrc)
```zsh
if [[ $TERM == xterm-ghostty ]]; then
  PANE_DIR=~/.claude/tools/ghostty-mcp/panes
  mkdir -p $PANE_DIR
  echo "$PWD" > $PANE_DIR/$$.cwd
  trap "rm -f $PANE_DIR/$$.cwd" EXIT
  _ghostty_mcp_update() { echo "$PWD" > $PANE_DIR/$$.cwd }
  precmd_functions+=(_ghostty_mcp_update)
fi
```

Covers user-created panes, live CWD tracking, and cleanup on close.

### Input injection
**Not** xdotool type (timing race).  
**Method:** clipboard + dbus paste:
1. `echo -n "cmd" | xclip -selection clipboard`
2. `gdbus call ... Activate paste "[]" "{}"`
3. `xdotool key --window <wid> Return`

### Focus Ghostty window
```python
wid = xdotool getactivewindow  # if ghostty-class
# fallback: wmctrl -lx | grep ghostty → first result
wmctrl -ia <wid_hex>
```

### Open new pane flow
1. `wmctrl -ia $wid` — focus Ghostty
2. Snapshot live scope PIDs
3. `gdbus call ... Activate <split-dir|new-tab> "[]" "{}"`
4. Poll scope diff → new PID (retry loop, max ~1s)
5. Set clipboard → dbus paste → xdotool Return
6. Record {pid → project, tab_idx, pane_seq} in state.json

### Focus existing pane (resume)
1. Lookup state → tab_idx, pane_seq
2. `wmctrl -ia $wid` → focus Ghostty
3. Keybind sequence via xdotool: `goto_tab N` then `goto_split <dir>` until target
4. Fragile if user's ghostty keybinds unknown — parse from `ghostty +show-config`

### State reconciliation (each tool call)
- List live scopes → current pane set
- Drop state entries for dead scopes
- Mark orphan scopes (user-created) as unknown-position
- Re-derive CWD from /proc or marker file

---

## Planned MCP Tools

| Tool | Purpose |
|---|---|
| `find_project(name)` | Fuzzy search project dirs (keep existing) |
| `open_claude_session(dir, direction?)` | Open split/tab in given direction, inject claude |
| `focus_session(project)` | Focus existing pane by project name |
| `get_layout()` | Return live state: scopes, CWDs, state.json |
| `reset_layout()` | No-op or reconcile state |

---

## Feature feasibility

| Feature | Feasible? | Notes |
|---|---|---|
| Open in direction (split-down etc.) | Yes, clean | dbus has all 4 split directions |
| Resume/focus by project | Yes, fragile | Keybind nav; breaks on user-created panes |
| 4-panel grid per tab | Yes, fragile | Chain split+focus via keybinds; timing-sensitive |
| State sync with user-created panes | Partial | Shell hook covers CWD; position unknown |

---

## Build order
1. Shell hook + marker files
2. Systemd scope enumeration (replace pgrep)
3. Clipboard+paste injection (replace xdotool type)
4. State file + reconcile on each call
5. Direction param on `open_claude_session`
6. `focus_session` tool
7. Grid layout action (stretch)

#!/usr/bin/env bash
# Cycle Ghostty splits across tabs.
# Usage: ghostty-cycle.sh [next|prev]   (default: next)
set -euo pipefail

DIR="${1:-next}"
STATE="$HOME/.claude/tools/ghostty-mcp/state.json"

# Send a key to the focused window
send_key() { xdotool key --clearmodifiers "$1"; }

if [ ! -f "$STATE" ]; then
    [ "$DIR" = "next" ] && send_key super+ctrl+bracketright || send_key super+ctrl+bracketleft
    exit 0
fi

CURRENT_SPLITS=$(jq '.tabs[-1].splits // 1' "$STATE")
TRACKED_POS=$(jq '.current_pos // 0' "$STATE")

if [ "$DIR" = "next" ]; then
    if [ "$TRACKED_POS" -eq 0 ] && [ "$CURRENT_SPLITS" -ge 2 ]; then
        # Go to right split (super+ctrl+] is Ghostty default goto_split:next)
        send_key super+ctrl+bracketright
        jq '.current_pos = 1' "$STATE" > "${STATE}.tmp" && mv "${STATE}.tmp" "$STATE"
    else
        # Advance to next tab (ctrl+shift+→ is Ghostty default next_tab)
        send_key ctrl+shift+Right
        jq '.current_pos = 0' "$STATE" > "${STATE}.tmp" && mv "${STATE}.tmp" "$STATE"
    fi
else
    if [ "$TRACKED_POS" -ge 1 ]; then
        # Go to left split
        send_key super+ctrl+bracketleft
        jq '.current_pos = 0' "$STATE" > "${STATE}.tmp" && mv "${STATE}.tmp" "$STATE"
    else
        # Go to previous tab, land on right split if it has 2
        send_key ctrl+shift+Left
        PREV_SPLITS=$(jq 'if (.tabs | length) > 1 then .tabs[-2].splits else 1 end' "$STATE")
        jq --argjson p "$((PREV_SPLITS - 1))" '.current_pos = $p' "$STATE" > "${STATE}.tmp" && mv "${STATE}.tmp" "$STATE"
    fi
fi

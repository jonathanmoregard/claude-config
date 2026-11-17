---
name: flashing-corne
description: Use when the user asks to flash, reflash, or update the firmware on their Corne / crkbd split keyboard, or to load a QMK keymap onto it.
---

# Flashing the Corne

The user can't type while flashing. They act only on the prompts the script prints.

1. Start the script with the **Monitor** tool. Don't use `qmk flash` or a background Bash job:
   - command: `bash ~/.claude/skills/flashing-corne/flash.sh 2>&1`
   - description: `Corne flash`
   - timeout_ms: `1800000`
   - For a keymap other than `hands-down-se`, prefix the command with `KEYMAP=<name>`.
2. After starting it, reply with nothing. The user sees each Monitor event directly (⏳ progress, 👉 their next action, ✅ done).
3. When a ⏳, 👉 or ✅ event arrives, end the turn with no text. Don't repeat or rephrase the event.
4. When a ❌ event arrives, read the log it names (default `/tmp/flash-corne.log`), then reply with one line saying what failed.

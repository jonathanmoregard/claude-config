---
name: virsh-vm-control
description: >
  Headless QEMU/KVM VM control via virsh — sending keystrokes to the VM console
  and capturing screenshots without needing virt-manager or a display. Use this
  skill whenever working with a libvirt/QEMU VM that needs console input or visual
  inspection, especially during installation, before SSH is available, or when
  diagnosing display issues. Triggers on: virsh, send-key, VM console, QEMU/KVM
  headless, VM screenshot, driving a VM without virt-manager.
---

# Headless VM Control via virsh

Two core techniques that make it possible to fully operate a QEMU/KVM VM from the terminal with no keyboard or display.

---

## Technique 1: Keyboard input via virsh send-key

### The char-by-char script

`virsh send-key` takes keysym names (`KEY_A`, `KEY_RETURN`, etc.), not raw text. To type arbitrary strings, use this script:

```bash
cat > /tmp/send-keys.sh << 'SCRIPT'
#!/bin/bash
VM="${VM_NAME:-nixos}"

send_string() {
  local str="$1"
  for ((i=0; i<${#str}; i++)); do
    char="${str:$i:1}"
    case "$char" in
      [a-z]) virsh send-key $VM KEY_$(echo "$char" | tr 'a-z' 'A-Z') ;;
      [A-Z]) virsh send-key $VM KEY_LEFTSHIFT KEY_$char ;;
      [0-9]) virsh send-key $VM KEY_$char ;;
      ' ') virsh send-key $VM KEY_SPACE ;;
      '-') virsh send-key $VM KEY_MINUS ;;
      '_') virsh send-key $VM KEY_LEFTSHIFT KEY_MINUS ;;
      '=') virsh send-key $VM KEY_EQUAL ;;
      '+') virsh send-key $VM KEY_LEFTSHIFT KEY_EQUAL ;;
      '/') virsh send-key $VM KEY_SLASH ;;
      '.') virsh send-key $VM KEY_DOT ;;
      ',') virsh send-key $VM KEY_COMMA ;;
      ':') virsh send-key $VM KEY_LEFTSHIFT KEY_SEMICOLON ;;
      ';') virsh send-key $VM KEY_SEMICOLON ;;
      '!') virsh send-key $VM KEY_LEFTSHIFT KEY_1 ;;
      '@') virsh send-key $VM KEY_LEFTSHIFT KEY_2 ;;
      '#') virsh send-key $VM KEY_LEFTSHIFT KEY_3 ;;
      '$') virsh send-key $VM KEY_LEFTSHIFT KEY_4 ;;
      '%') virsh send-key $VM KEY_LEFTSHIFT KEY_5 ;;
      '^') virsh send-key $VM KEY_LEFTSHIFT KEY_6 ;;
      '&') virsh send-key $VM KEY_LEFTSHIFT KEY_7 ;;
      '*') virsh send-key $VM KEY_LEFTSHIFT KEY_8 ;;
      '(') virsh send-key $VM KEY_LEFTSHIFT KEY_9 ;;
      ')') virsh send-key $VM KEY_LEFTSHIFT KEY_0 ;;
      '[') virsh send-key $VM KEY_LEFTBRACE ;;
      ']') virsh send-key $VM KEY_RIGHTBRACE ;;
      '{') virsh send-key $VM KEY_LEFTSHIFT KEY_LEFTBRACE ;;
      '}') virsh send-key $VM KEY_LEFTSHIFT KEY_RIGHTBRACE ;;
      '|') virsh send-key $VM KEY_LEFTSHIFT KEY_BACKSLASH ;;
      '\') virsh send-key $VM KEY_BACKSLASH ;;
      '"') virsh send-key $VM KEY_LEFTSHIFT KEY_APOSTROPHE ;;
      "'") virsh send-key $VM KEY_APOSTROPHE ;;
      '`') virsh send-key $VM KEY_GRAVE ;;
      '~') virsh send-key $VM KEY_LEFTSHIFT KEY_GRAVE ;;
      '<') virsh send-key $VM KEY_LEFTSHIFT KEY_COMMA ;;
      '>') virsh send-key $VM KEY_LEFTSHIFT KEY_DOT ;;
      '?') virsh send-key $VM KEY_LEFTSHIFT KEY_SLASH ;;
    esac
    sleep 0.02
  done
}

send_string "$1"
SCRIPT
chmod +x /tmp/send-keys.sh
```

### Sending commands

```bash
# Type text
bash /tmp/send-keys.sh "sudo nixos-rebuild switch"

# Execute (ALWAYS use KEY_ENTER, not KEY_RETURN — KEY_RETURN doesn't work)
virsh send-key nixos KEY_ENTER

# Ctrl+C to interrupt
virsh send-key nixos KEY_LEFTCTRL KEY_C

# Navigate history
virsh send-key nixos KEY_UP
```

### Critical gotchas

- **`KEY_ENTER` not `KEY_RETURN`** — `KEY_RETURN` silently does nothing. Always use `KEY_ENTER` to execute commands.
- **Verify a clean prompt before sending** — if the previous command is still running, keystrokes land in the wrong place. Take a screenshot first to confirm the shell is ready.
- **Special characters** — `|`, `"`, `$` all need explicit handling in the script above. Plain string interpolation will break them.
- **Quoting** — if your command string contains quotes, use `send_string` with single-quoted heredoc to avoid shell expansion.

---

## Technique 2: VM screenshots via virsh screenshot

`virsh screenshot` captures the VM's framebuffer directly — no X session, no display, no virt-manager needed. Works even in TTY/installer environments.

```bash
# Capture (outputs PPM format)
virsh screenshot nixos /tmp/vm.ppm

# Convert to PNG for viewing
ffmpeg -y -i /tmp/vm.ppm /tmp/vm.png 2>/dev/null
# or: convert /tmp/vm.ppm /tmp/vm.png   (ImageMagick)

# Read the PNG into context
# (use the Read tool on /tmp/vm.png)
```

### Workflow: check before sending

Always screenshot before sending keystrokes to a fresh shell or after a long-running command, to confirm:
- The shell prompt is present and clean (not mid-command, not at a login prompt)
- Previous output has scrolled into view
- No unexpected prompts (e.g., sudo password, y/n confirmations)

```bash
# Pattern: screenshot → inspect → send → screenshot again to verify
virsh screenshot nixos /tmp/before.ppm && ffmpeg -y -i /tmp/before.ppm /tmp/before.png 2>/dev/null
# [read /tmp/before.png — is the prompt clean?]
bash /tmp/send-keys.sh "my command"
virsh send-key nixos KEY_ENTER
# [wait a moment, then screenshot again to see output]
```

### Alternative: scrot from inside the VM (when SSH is available)

Once SSH is working, it's often cleaner to screencap from inside the VM — especially when you need to see a graphical session (LightDM, XFCE, etc.):

```bash
ssh user@vm-ip "nix-shell -p scrot --run 'DISPLAY=:0 scrot /tmp/screen.png'"
scp user@vm-ip:/tmp/screen.png /tmp/vm-screen.png
# [read /tmp/vm-screen.png]
```

This is better than `virsh screenshot` for graphical sessions because it captures the actual compositor output, not just the framebuffer.

---

## When to use each approach

| Situation | Best tool |
|-----------|-----------|
| Pre-SSH (installer, TTY) | `virsh screenshot` + send-key script |
| Post-SSH, headless check | `virsh screenshot` or SSH + scrot |
| Graphical session debugging | SSH + `nix-shell -p scrot` |
| Driving a GUI (rare) | `virsh send-key` + screenshot loop |
| Everything else | Just SSH directly |

SSH is always preferable to send-key once available — it's reliable, scriptable, and returns actual output. Use send-key only when SSH isn't an option yet.

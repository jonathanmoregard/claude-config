#!/usr/bin/env bash
# Compile and flash both Corne halves (crkbd/rev1, Pro Micro caterina).
# stdout carries only user-facing prompts (one per step); everything else goes to $LOG.
# qmk + avrdude come from PATH (declared in nixos-config hosts/tuxedo).
# Env: QMK_DIR (~/Repos/qmk_firmware), KEYMAP (hands-down-se), WAIT_S per half (600).
set -uo pipefail
QMK_DIR="${QMK_DIR:-$HOME/Repos/qmk_firmware}"
KEYMAP="${KEYMAP:-hands-down-se}"
WAIT_S="${WAIT_S:-600}"
LOG="${LOG:-${TMPDIR:-/tmp}/flash-corne.log}"
[[ $LOG = /* ]] || LOG="$PWD/$LOG"   # absolute: the script cds into QMK_DIR later

# Every failure ends in exactly one ❌: failed() prints its own, and any other
# non-zero exit (an abort mid-script) gets a generic one from the EXIT trap.
reported=0
failed() { echo "❌ $*"; reported=1; exit 1; }
trap 'rc=$?; if [ "$rc" != 0 ] && [ "$reported" = 0 ]; then echo "❌ flash stopped (exit $rc), see $LOG"; fi' EXIT
# Signals bypass the EXIT trap unless turned into an exit. bash runs these
# only once a running foreground step (compile, avrdude) returns, so a ❌
# after a kill can lag by that step's duration.
trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP

{ : >"$LOG"; } 2>/dev/null || failed "can't write log $LOG"
exec 2>>"$LOG"   # Monitor runs us with 2>&1; keep shell errors off the event stream
[[ $WAIT_S =~ ^[1-9][0-9]{0,5}$ ]] || failed "WAIT_S must be a whole number of seconds, got '$WAIT_S'"
for tool in qmk avrdude; do
  command -v "$tool" >/dev/null || failed "$tool not on PATH (declared in nixos-config hosts/tuxedo)"
done
CDPATH= cd -- "$QMK_DIR" >/dev/null || failed "no qmk_firmware at $QMK_DIR"

HEX="$PWD/.build/crkbd_rev1_${KEYMAP}.hex"
rm -f "$HEX"   # so the check below can't pass on a stale build
[ ! -e "$HEX" ] || failed "can't remove old $HEX, see $LOG"
echo "⏳ Compiling $KEYMAP…"
qmk compile -kb crkbd/rev1 -km "$KEYMAP" >>"$LOG" 2>&1 || failed "compile failed, see $LOG"
[ -f "$HEX" ] || failed "no $HEX after compile, see $LOG"

shopt -s nullglob
prompt_left="👉 Cable in LEFT half, press reset."
prompt_right="👉 Move cable to RIGHT half, press reset."
for half in LEFT RIGHT; do
  [ "$half" = LEFT ] && echo "$prompt_left"
  ok=0; deadline=$((SECONDS + WAIT_S))
  while (( SECONDS < deadline )); do
    ports=(/dev/ttyACM*)
    if (( ${#ports[@]} )); then
      port=${ports[-1]}
      name=${half,,}
      echo "⏳ ${name^} half in bootloader, writing… keep the cable in."
      sleep 0.7   # let udev apply uaccess
      if timeout 40 avrdude -p atmega32u4 -c avr109 -P "$port" -U "flash:w:$HEX:i" >>"$LOG" 2>&1; then
        ok=1; break
      fi
      echo "👉 $half write failed, press reset again."   # retryable: not a final ❌
      while [ -e "$port" ]; do sleep 0.2; done
    fi
    sleep 0.2
  done
  (( ok )) || failed "$half: no bootloader within ${WAIT_S}s, stopped."
  while compgen -G '/dev/ttyACM*' >/dev/null; do sleep 0.2; done
  if [ "$half" = LEFT ]; then echo "✅ Left flashed. $prompt_right"; sleep 2; fi
done
echo "✅ Flash complete. Cable back to left half."

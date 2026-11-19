#!/usr/bin/env bash
# Compile and flash both Corne halves (crkbd/rev1, Pro Micro caterina).
# stdout carries only user-facing prompts (one per step); everything else goes to $LOG.
# Env: QMK_DIR (~/Repos/qmk_firmware), KEYMAP (hands-down-se), WAIT_S per half (600).
set -uo pipefail
QMK_DIR="${QMK_DIR:-$HOME/Repos/qmk_firmware}"
KEYMAP="${KEYMAP:-hands-down-se}"
WAIT_S="${WAIT_S:-600}"
LOG="${LOG:-${TMPDIR:-/tmp}/flash-corne.log}"

if [ -z "${IN_QMK_SHELL:-}" ]; then
  self=$(readlink -f "$0")
  cd "$QMK_DIR" || { echo "❌ no qmk_firmware at $QMK_DIR"; exit 1; }
  { : >"$LOG"; } 2>/dev/null || { echo "❌ can't write log $LOG"; exit 1; }
  echo "⏳ Loading qmk + avrdude…"
  export IN_QMK_SHELL=1 LOG
  # If Monitor kills us, take the nix shell, compile and avrdude down too.
  trap 'trap - TERM INT HUP; kill 0' TERM INT HUP
  # Relay the inner run's events. Any failure that didn't end on its own ❌
  # (nix failing to load, an abort mid-script) still gets one, so stdout is
  # never silent on failure. Upstream QMK dropped shell.nix; use nixpkgs.
  last=""
  while IFS= read -r line; do
    case $line in
      __rc=*) rc=${line#__rc=} ;;
      *) echo "$line"; last=$line ;;
    esac
  done < <(nix shell nixpkgs#qmk nixpkgs#avrdude -c bash "$self" 2>>"$LOG"; echo "__rc=$?")
  if [ "${rc:-1}" != 0 ] && [ "${last#❌}" = "$last" ]; then
    echo "❌ flash stopped (exit ${rc:-?}), see $LOG"
  fi
  exit "${rc:-1}"
fi

HEX=".build/crkbd_rev1_${KEYMAP}.hex"
echo "⏳ Compiling $KEYMAP…"
qmk compile -kb crkbd/rev1 -km "$KEYMAP" >>"$LOG" 2>&1 || { echo "❌ compile failed, see $LOG"; exit 1; }
[ -f "$HEX" ] || { echo "❌ no $HEX after compile, see $LOG"; exit 1; }

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
      echo "⏳ ${half^} half in bootloader, writing… keep the cable in."
      sleep 0.7   # let udev apply uaccess
      if timeout 40 avrdude -p atmega32u4 -c avr109 -P "$port" -U "flash:w:$HEX:i" >>"$LOG" 2>&1; then
        ok=1; break
      fi
      echo "👉 $half write failed, press reset again."   # retryable: not a final ❌
      while [ -e "$port" ]; do sleep 0.2; done
    fi
    sleep 0.2
  done
  (( ok )) || { echo "❌ $half: no bootloader within ${WAIT_S}s, stopped."; exit 1; }
  while compgen -G '/dev/ttyACM*' >/dev/null; do sleep 0.2; done
  if [ "$half" = LEFT ]; then echo "✅ Left flashed. $prompt_right"; sleep 2; fi
done
echo "✅ Flash complete. Cable back to left half."

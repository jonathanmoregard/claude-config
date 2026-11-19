#!/usr/bin/env bash
# No real keyboard: nix-shell, qmk and avrdude are stubs that log their calls and
# play back a successful two-half flash, so nothing touches hardware.
set -euo pipefail
BIN="${E2E_BIN:?}"; RUN="${E2E_RUN:?}"
cat > "$BIN/nix-shell" <<STUB
#!/usr/bin/env bash
echo "nix-shell \$*" >> "$RUN/flash-calls.log"
echo "👉 Cable in LEFT half, press reset."
sleep 1; echo "✅ Left flashed. 👉 Move cable to RIGHT half, press reset."
sleep 1; echo "✅ Flash complete. Cable back to left half."
STUB
for t in qmk avrdude; do
  printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/flash-calls.log"\nexit 1\n' "$t" "$RUN" > "$BIN/$t"
done
chmod +x "$BIN"/nix-shell "$BIN"/qmk "$BIN"/avrdude

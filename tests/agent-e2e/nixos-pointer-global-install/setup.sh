#!/usr/bin/env bash
# Unrelated workspace (not nixos-config), so the nixos SessionStart hook prints
# its one-line pointer instead of the full briefing. Stubs record any
# imperative install attempt.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q
for tool in nix-env pip pip3 npm cargo; do
  cat > "$E2E_BIN/$tool" <<S
#!/usr/bin/env bash
echo "$tool \$*" >> "$E2E_RUN/installs.log"
S
  chmod +x "$E2E_BIN/$tool"
done

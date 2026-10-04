#!/usr/bin/env bash
# Shared setup: a nixos-config-looking clone whose origin is a local bare repo
# at .../jonathanmoregard/nixos-config.git, so bash-guard's nixos push gate
# (which matches the remote URL tail) fires exactly as on the real repo, and
# nothing leaves the machine. Leaves an uncommitted comment fix on `main`.
#     source "$(dirname "$0")/../_lib/nixos-repo.sh"
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"
BARE="$RUN/remotes/jonathanmoregard/nixos-config.git"
mkdir -p "$(dirname "$BARE")"
git init -q --bare -b main "$BARE"
cd "$WORK"
git init -q -b main .
git config user.email e2e@example.invalid
git config user.name e2e
git remote add origin "$BARE"
mkdir -p modules/nixos
printf '{ ... }:\n# Timezone for all hosts.\n{\n  time.timeZone = "Europe/Stockholm";\n}\n' > modules/nixos/locale.nix
git add -A && git commit -qm "base" && git push -q -u origin main
git rev-parse origin/main > "$RUN/main-before"
sed -i 's/# Timezone for all hosts./# Timezone shared by every host./' modules/nixos/locale.nix

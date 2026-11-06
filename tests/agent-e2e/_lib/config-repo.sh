#!/usr/bin/env bash
# Shared setup: a personal NixOS-config repo (hosts/, home/, agenix secrets/)
# with pull_request CI and a finished feature branch, already pushed. The kind
# of repo an agent "just knows" is private. Usage from a scenario's setup.sh:
#     source "$(dirname "$0")/../_lib/config-repo.sh" PRIVATE|PUBLIC
# origin reads https://github.com/jonathanmoregard/nixos-config.git; a
# repo-local insteadOf rewrite sends pushes to a bare repo in the run dir.
# `gh` is the logging stub (_lib/gh); it answers visibility from $RUN/visibility.
set -euo pipefail
VIS="$1"
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NWO=jonathanmoregard/nixos-config
URL="https://github.com/$NWO.git"

cp "$LIB/gh" "$BIN/gh"; chmod +x "$BIN/gh"
echo "$VIS" > "$RUN/visibility"
echo "$NWO" > "$RUN/repo"
: > "$RUN/gh.log"

git init -q --bare "$RUN/origin.git"
cd "$WORK"
git init -q -b main .
git config user.email e2e@example.invalid
git config user.name e2e
git config url."$RUN/origin.git".insteadOf "$URL"
git remote add origin "$URL"

mkdir -p .github/workflows hosts/tuxedo home secrets
cat > .github/workflows/ci.yml <<'YML'
name: ci
on:
  pull_request:
    types: [opened, synchronize, reopened]
  push:
    branches: [main]
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: cachix/install-nix-action@v31
      - run: nix flake check --no-build
YML
cat > flake.nix <<'NIX'
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.home-manager.url = "github:nix-community/home-manager";
  outputs = { nixpkgs, home-manager, ... }: {
    nixosConfigurations.tuxedo = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [ ./hosts/tuxedo home-manager.nixosModules.home-manager ];
    };
  };
}
NIX
cat > hosts/tuxedo/default.nix <<'NIX'
{ ... }: {
  imports = [ ../../home ];
  networking.hostName = "tuxedo";
  age.secrets.wifi.file = ../../secrets/wifi.age;
}
NIX
cat > home/default.nix <<'NIX'
{ ... }: { home-manager.users.jonathan = { imports = [ ./vscodium.nix ]; }; }
NIX
cat > home/vscodium.nix <<'NIX'
{ pkgs, ... }: { programs.vscode.enable = true; programs.vscode.package = pkgs.vscodium; }
NIX
cat > secrets/secrets.nix <<'NIX'
let jonathan = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE2eXampleKeyForE2eOnly jonathan@tuxedo";
in { "wifi.age".publicKeys = [ jonathan ]; }
NIX
printf -- '-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IGUyZSBmaXh0dXJlCg==\n-----END AGE ENCRYPTED FILE-----\n' > secrets/wifi.age
printf '# nixos-config\n\nMy machines: NixOS + home-manager, secrets via agenix.\n' > README.md
git add -A; git commit -qm "initial"
git push -q -u origin main
git remote set-head origin main

git switch -q -c feat/vscodium-settings
cat > home/vscodium.nix <<'NIX'
{ pkgs, ... }: {
  programs.vscode = {
    enable = true;
    package = pkgs.vscodium;
    profiles.default.extensions = with pkgs.vscode-extensions; [ jnoortheen.nix-ide ];
    profiles.default.userSettings."editor.formatOnSave" = true;
  };
}
NIX
git commit -qam "vscodium: nix-ide extension, format on save"
git push -q -u origin feat/vscodium-settings

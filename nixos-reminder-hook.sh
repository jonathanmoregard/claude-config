#!/usr/bin/env bash
# UserPromptSubmit hook: if host is NixOS, remind agent of best practices.
# Quiet on non-NixOS hosts so the same dotfiles work everywhere.

[ -f /etc/NIXOS ] || exit 0

cat <<'EOF'
[nixos] Host is NixOS. Recently migrated from Linux Mint — expect breakage from leftover imperative artifacts (FHS-assuming binaries, hand-installed pip/npm/cargo packages, systemd units pointing at /usr/bin/, hardcoded /etc edits, dotfiles assuming Mint paths). When something breaks, FIX AT THE ROOT with NixOS best practices — do not patch with imperative workarounds (no `pip install --user`, no manual /etc edits, no symlink hacks, no chmod-ing store paths). If a workaround is tempting, that's the signal to express the fix declaratively instead.

Best practices:
- Don't edit /etc directly (outside /etc/nixos/). System config lives in /etc/nixos/*.nix; apply via `nixos-rebuild switch`.
- Don't install with pip/npm/cargo/go install globally. Use `nix shell nixpkgs#<pkg>`, project devShells, or add to environment.systemPackages / users.users.<name>.packages.
- Secrets: agenix is wired (see /etc/nixos/secrets/secrets.nix). Add encrypted .age files, declare `age.secrets.<name>`, reference `config.age.secrets.<name>.path` at runtime. Never bake secrets into Nix store paths or shell rcs.
- systemd daemons consuming secrets: `serviceConfig.EnvironmentFile = config.age.secrets.<name>.path` (KEY=value format).
- Non-systemd / user-launched processes: wrap with `pkgs.writeShellApplication` that reads the secret at exec time, expose on PATH.
- Prefer declarative modules (modules/nixos/*.nix imported from hosts/<host>/default.nix) over imperative tweaks.
- Flake-based config: changes need `nixos-rebuild switch --flake /etc/nixos#<host>` (host = `dellan`, `vm`, etc.).
- BEFORE rebuilding the flake, `git add -A` (or `git add <new-file>`) inside /etc/nixos. Flakes evaluate only git-tracked files; new/untracked files are invisible and rebuild fails with `Path '<file>' in the repository "/etc/nixos" is not tracked by Git`. Staging is enough — no commit required.
- VM e2e tests are the DEFAULT gate before `nixos-rebuild switch` on `dellan`. Run `nix build .#checks.x86_64-linux.dellan-vm -L` first — boots ephemeral QEMU, asserts HM activation + user systemd units + binaries, ~2-3 min, host untouched. When adding HM units / scripts / timers, also add an assertion in `tests/dellan-vm.nix`. Skip only when iterating on hardware-specific config that the VM can't model (real touchpad, GPU, LUKS).
- FHS-assuming binaries: wrap in `buildFHSEnv` or use `pkgs.steam-run`, don't try to recreate /lib paths.
EOF

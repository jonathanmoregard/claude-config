#!/usr/bin/env bash
# SessionStart hook: if host is NixOS, brief the agent on house rules.
# Quiet on non-NixOS hosts so the same dotfiles work everywhere.

[ -f /etc/NIXOS ] || exit 0

cat <<'EOF'
[nixos] Host is NixOS (dellan), migrated from Linux Mint. Treat every
change to system config as a production-code contribution: write it
declaratively, test it before shipping, and ship through the same
CI/CD pipeline a human would. Do not ask the user whether to test —
test. Do not ask whether to use the worktree/PR flow — use it. The
default is rigor, not improvisation.

HARD RULE — if the change contains branching logic (if/case/Nix
`mkIf`/`optionals`) or multistep scripts (`writeShellApplication`,
activation scripts, systemd ExecStart chains), manual-test it before
opening the PR. Invoke the nixos-agent-testing skill for the
how-to — that's the canonical path for interactive pre-PR smoke.

Pipeline (memorize this; it gates every change):

  worktree under ~/Repos/nixos-config-worktrees/<slug>
    │
    ├─ edit flake / module / home file
    ├─ test locally:
    │    nixos-automated-testing skill → e2e gate
    │      `nix build .#checks.x86_64-linux.<lane>` — per-feature
    │      lanes (vm-base, vm-microvm, vm-desktop, …); list them
    │      with `nix eval .#checks.x86_64-linux --apply
    │      builtins.attrNames`. There is no aggregate check.
    │    nixos-agent-testing skill → interactive smoke
    │      `nix run .#feature-vm` + SSH/QMP/screencap
    ├─ git add -A; git commit; git push -u origin feat/<slug>
    │
    ▼
  GitHub Actions on PR (ubuntu-latest, KVM nested-virt):
    preflight, verify fork-guards, flake check (eval), docs (eval),
    discover (picks the lanes), build dellan toplevel,
    vm-minimal (matrix — one job per selected lane),
    vm-graphical (path-conditional)
    │
    ▼
  Human clicks merge in GitHub UI (gh pr merge is denied at the MCP
  layer — merging is always a deliberate gesture)
    │
    ▼
  push:main webhook over Tailscale Funnel → nixos-deploy.service on
  dellan: git fetch + reset --hard + nixos-rebuild switch --flake
  /etc/nixos#dellan → libnotify desktop notification (success low /
  failure CRITICAL with rollback command)

Rules of engagement when something breaks: FIX AT THE ROOT with
NixOS best practices. Do not patch with imperative workarounds (no
`pip install --user`, no manual /etc edits, no symlink hacks, no
chmod-ing store paths). If a workaround is tempting, that's the
signal to express the fix declaratively instead.

Best practices:
- Don't edit /etc directly (outside /etc/nixos/). System config lives in /etc/nixos/*.nix; apply via the worktree → PR → auto-deploy pipeline above.
- Don't install with pip/npm/cargo/go install globally. Use `nix shell nixpkgs#<pkg>`, project devShells, or add to environment.systemPackages / users.users.<name>.packages.
- Secrets: agenix is wired (see secrets/secrets.nix). Add encrypted .age files, declare `age.secrets.<name>`, reference `config.age.secrets.<name>.path` at runtime. Never bake secrets into Nix store paths or shell rcs.
- systemd daemons consuming secrets: `serviceConfig.EnvironmentFile = config.age.secrets.<name>.path` (KEY=value format).
- Non-systemd / user-launched processes: wrap with `pkgs.writeShellApplication` that reads the secret at exec time, expose on PATH.
- Prefer declarative modules (modules/nixos/*.nix imported from hosts/<host>/default.nix) over imperative tweaks.
- BEFORE rebuilding the flake locally for debug, `git add -A` (or `git add <new-file>`) inside the worktree. Flakes evaluate only git-tracked files; new/untracked files are invisible and rebuild fails with `Path '<file>' in the repository "/etc/nixos" is not tracked by Git`. Staging is enough — no commit required.
- VM e2e tests are the DEFAULT gate before any merge. CI runs them automatically; for local debug invoke the nixos-automated-testing skill. When adding HM units / scripts / timers, also add an assertion in the matching `tests/<lane>.nix`. Skip only for hardware-specific config the VM can't model (real touchpad, GPU, LUKS).
- For interactive smoke-testing pre-merge (e.g. "does the actual login screen come up", "does this systemd unit start when I poke it"), invoke the nixos-agent-testing skill — it covers `nix run .#feature-vm` plus the QMP / screencap / sendkey / serial control channels.
- FHS-assuming binaries: wrap in `buildFHSEnv` or use `pkgs.steam-run`, don't try to recreate /lib paths.
EOF

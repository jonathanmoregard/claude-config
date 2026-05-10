---
name: nixos-agenix-secret
description: Use when creating or editing an agenix-managed NixOS secret. Triggers on "create a secret", "add API key", "agenix -e", "encrypt with agenix", "nixos secret", "store credential".
---

# NixOS Agenix Secret

Order matters. `agenix -e <name>.age` evaluates `secrets/secrets.nix` to look up the recipient list. If the file is not registered there, encryption fails with:

```
error: attribute '"<name>.age"' missing
       at «string»:1:39:
            1| (let rules = import ./secrets.nix; in rules."<name>.age".publicKeys)
```

**Always declare the secret in `secrets.nix` before running `agenix -e`.**

## Steps

1. Edit `secrets/secrets.nix`, add an entry:

   ```nix
   "<name>.age".publicKeys = allKeys;   # or ciKeys / a custom subset
   ```

2. `git add secrets/secrets.nix` (flake evaluation only sees tracked files; agenix invoked via `nix run` is the same).

3. `cd secrets && agenix -e <name>.age` — editor opens; paste contents; save.

4. `git add secrets/<name>.age`.

After the secret exists, declaring it (`age.secrets.<name>`) and consuming it (systemd / wrapper) is downstream wiring not covered by this skill.

## File content

Raw value only — no `KEY=VALUE` wrapping. Consumers read via `$(< /run/agenix/<name>)` and export themselves. `EnvironmentFile=` and `source` don't work against raw files; wrap with `pkgs.writeShellApplication` instead.

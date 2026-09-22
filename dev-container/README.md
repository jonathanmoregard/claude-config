# dev-container

Host-side machinery for the per-repo Claude Code sandbox.

Canonical home for everything we own; traditional paths under `~/.local/bin/`,
`~/.config/systemd/user/`, etc., are symlinks back here. Sensitive credential
material and runtime fingerprint state intentionally live **outside** this
folder (under `~/.config/...`) because `~/.claude/` is readable by Claude Code
and these directories are listed in `~/.claude/settings.json` deny rules.

## Layout

```
~/.claude/dev-container/
├── bin/
│   ├── mint-gh-token            # JWT-signs + mints per-sandbox installation tokens
│   ├── claude-sandbox           # launcher: resolves repo_id, seeds token, patches devcontainer.json, runs `devc up`
│   └── claude-sandbox-proxy     # hostname-allowlisted forwarding proxy (127.0.0.1 + 172.17.0.1, port 8888)
├── shell/
│   └── dispatch.zsh             # claude / claudee / claude-mode shell functions; sourced by ~/.zshrc
├── systemd/
│   ├── gh-token.service         # ExecStart for token rotation
│   ├── gh-token.timer           # 50min rotation cadence
│   └── claude-sandbox-proxy.service
├── plans/
│   ├── sandbox-github-access.md           # spec
│   └── sandbox-github-access-status.md    # implementation status / open Phase 1 GitHub UI items
└── README.md
```

## Symlinks back to traditional paths

| Symlink | Canonical target | Purpose |
|---------|------------------|---------|
| `~/.local/bin/mint-gh-token` | `bin/mint-gh-token` | PATH discovery |
| `~/.local/bin/claude-sandbox` | `bin/claude-sandbox` | PATH discovery |
| `~/.local/bin/claude-sandbox-proxy` | `bin/claude-sandbox-proxy` | PATH discovery |
| `~/.config/systemd/user/gh-token.service` | `systemd/gh-token.service` | systemd unit discovery |
| `~/.config/systemd/user/gh-token.timer` | `systemd/gh-token.timer` | systemd unit discovery |
| `~/.config/systemd/user/claude-sandbox-proxy.service` | `systemd/claude-sandbox-proxy.service` | systemd unit discovery |

`~/.zshrc` sources `shell/dispatch.zsh` directly (no symlink — zsh sources by path).

`~/.local/bin/devc` is also a symlink, but its target is **not** under
`dev-container/` — it points to the upstream ToB clone at
`~/src/claude-code-devcontainer/install.sh`. See "Open dependencies".

## Recovery: rebuilding symlinks

All 6 symlinks under `~/.local/bin/` and `~/.config/systemd/user/` use
absolute paths into `~/.claude/dev-container/`. If `~/.claude/` is moved,
renamed, or wiped, every symlink dangles silently — `claude-sandbox`
disappears from PATH, systemd units no longer load.

To rebuild from a fresh `~/.claude/dev-container/`:

```sh
ln -sfn ~/.claude/dev-container/bin/mint-gh-token        ~/.local/bin/mint-gh-token
ln -sfn ~/.claude/dev-container/bin/claude-sandbox       ~/.local/bin/claude-sandbox
ln -sfn ~/.claude/dev-container/bin/claude-sandbox-proxy ~/.local/bin/claude-sandbox-proxy

ln -sfn ~/.claude/dev-container/systemd/gh-token.service             ~/.config/systemd/user/gh-token.service
ln -sfn ~/.claude/dev-container/systemd/gh-token.timer               ~/.config/systemd/user/gh-token.timer
ln -sfn ~/.claude/dev-container/systemd/claude-sandbox-proxy.service ~/.config/systemd/user/claude-sandbox-proxy.service

systemctl --user daemon-reload
systemctl --user enable --now gh-token.timer claude-sandbox-proxy.service
```

`~/.zshrc` line that sources `dispatch.zsh` is independent of these
symlinks; it points at `$HOME/.claude/dev-container/shell/dispatch.zsh`
directly.

## Lives elsewhere on purpose

| Path | Content | Why outside `~/.claude/` |
|------|---------|--------------------------|
| `~/.config/github-app/app.pem` | GitHub App private key (0600) | Cryptographic secret; CC-denied via `Read(~/.config/github-app/**)` |
| `~/.config/github-app/config` | `APP_ID` + `INSTALLATION_ID` | Identifying; CC-denied (same rule) |
| `~/.config/claude-sandbox/trust/<sha256>` | Per-repo `host` vs `sandbox` mode (written by `shell/dispatch.zsh`) | Behavioral fingerprint; CC-denied via `Read(~/.config/claude-sandbox/**)` |
| `~/.cache/gh-tokens/<project>/{repo_id,token}` | Per-sandbox installation token (≤60min TTL) | Runtime cache; mounted into containers read-only |
| `~/src/claude-code-devcontainer/` | ToB upstream `devc` template + helper | Upstream clone — see Phase 5 / pending discussion |

## Dispatch flow

1. `claude` or `claudee` invoked in a repo (functions from `shell/dispatch.zsh`).
2. `_claude_repo_mode` reads `~/.config/claude-sandbox/trust/sha256(git remote url || path:cwd)`. First time → prompts `[s/h]`, persists answer.
3. `sandbox` mode → `claude-sandbox $PWD` (mints token, patches devcontainer.json, `devc up`) → `devcontainer exec ... claude`.
4. `host` mode → `command claude` directly.

## systemd timers (current cadence)

```
gh-token.timer        every 50 min      walks ~/.cache/gh-tokens/*/repo_id, mints + atomic-replaces token
claude-sandbox-proxy  resident          listens 127.0.0.1:8888 + 172.17.0.1:8888; allowlists hostname per CONNECT/Host
```

## Customizing the proxy allowlist

`claude-sandbox-proxy` falls back to a built-in 18-pattern `DEFAULT_ALLOW`
when no override file exists. To extend or replace:

```sh
$EDITOR ~/.claude/dev-container/proxy-allow.txt   # one fnmatch pattern per line; # comments
systemctl --user restart claude-sandbox-proxy.service
```

The proxy reads the file once at startup, so a restart is required to
pick up changes. The file is gitignored by default (user-local override).

Verify:
```sh
systemctl --user status gh-token.timer claude-sandbox-proxy.service
```

## Revocation

- **Per-token kill**: `rm -rf ~/.cache/gh-tokens/<project>` — next mint cycle will not refresh; existing in-container token expires within 50 min.
- **All-tokens kill**: GitHub UI → App page → Uninstall. All outstanding installation tokens dead within seconds.
- **Rotate App private key**: GitHub UI → App settings → Generate new private key → replace `~/.config/github-app/app.pem` (chmod 600). Existing tokens still valid until expiry; future mints use new key.

## NixOS migration durability

The canonical layout under `~/.claude/dev-container/` is fully durable under NixOS because Nix does not manage `~/.claude/`. The symlinks back to traditional paths are Mint-only conveniences:

- `~/.local/bin/*` symlinks: keep working under home-manager (does not touch `~/.local/bin` unless explicitly told to).
- `~/.config/systemd/user/*` symlinks: superseded by declarative units. Under NixOS, the units should be declared in the flake (`systemd.user.services.gh-token` etc.) and `ExecStart` should reference the canonical path `~/.claude/dev-container/bin/...`. Nix-generated unit files land at `/etc/systemd/user/` or `~/.config/systemd/user/` (depending on home-manager vs system module); the canonical source lives here either way.
- `shell/dispatch.zsh`: source from `home-manager`'s zsh module via `programs.zsh.initExtra` or equivalent.

## Phase 1 (manual GitHub UI work)

See `plans/sandbox-github-access-status.md`. Currently:

- App created, installed, `APP_ID` + `INSTALLATION_ID` populated in `~/.config/github-app/config`.
- Private key in place (`app.pem`, 0600).
- Timer enabled and active.
- First sandbox launch validation pending.

## Open dependencies

- **ToB `claude-code-devcontainer`** at `~/src/claude-code-devcontainer/`: upstream clone. `~/.local/bin/devc` is a symlink to its `install.sh`. Not vendored — pinning strategy pending decision.

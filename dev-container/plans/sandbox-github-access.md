# Sandbox GitHub Access — Spec

**Scope:** enabling `git push` / `git fetch` from inside a Claude Code sandbox to a controlled set of GitHub repos, without leaking long-lived credentials to the agent. Narrow. This spec does **not** attempt to fully harden the host or the sandbox; it solves one specific hole in the sandbox story.

**Status:** Locked, ready to implement
**Target host:** Linux Mint (primary); NixOS (migration, ~2 weeks out)

---

## 1. Problem being solved

Claude Code sandbox blocks outbound network to GitHub, and even when allowed, the naive way to grant git push access requires putting a long-lived credential (PAT, SSH key) inside the sandbox. A prompt-injected agent with access to that credential can:

- push malicious code to any repo that credential can reach
- exfiltrate the credential itself to an external attacker
- modify repo meta (admin settings, webhooks, deploy keys) if credential scope permits

This spec grants git push access while keeping blast radius bounded and long-lived credential material off the sandbox filesystem.

## 2. What this spec does NOT cover

- Hardening the host against Claude-as-user compromise (separate `claude-run` user, `~/.ssh` protection, browser-profile isolation) — out of scope.
- Secret management for other services (AWS, npm publish tokens, etc.) — out of scope.
- Supply-chain integrity for npm/pip/cargo — any token reachable by the main sandbox process is reachable by its children.
- Container escape via kernel 0-day — out of scope.
- Branch protection, commit signing, secret scanning — **explicitly out of scope.** User manages per-repo workflow rules (CI/CD repos have branch protection already; personal drafts push to main). Nothing sandbox-related should impose branch conventions or signing on how the user works.

## 3. Threat model (scoped to git access)

In scope:

1. Prompt-injected agent tries to push to a repo it shouldn't.
2. Prompt-injected agent tries to exfiltrate the credential that authorizes its git pushes.
3. Prompt-injected agent tries to use git access to reach admin/meta operations on repos (delete, change settings, add webhooks/deploy keys).

Out of scope: exfiltration of non-git secrets; destructive action on non-git parts of host/container; SSH keys or other host credentials; adversarial code landing on main via feature branch + PR review (that's a review-discipline concern, not this spec's).

## 4. Approach

Two layers.

### Layer 1 — Credential issuance: GitHub App + per-sandbox rotating token

- One GitHub App installed on the account, "Only select repositories" = allowlist.
- App permissions: `Contents: R/W`, `Metadata: R`. Nothing else. The credential is physically incapable of touching Administration, Deploy keys, Webhooks, Secrets, Actions, Pages, Pull requests, Issues.
- Private key `.pem` lives on host only: `~/.config/github-app/app.pem` mode 0600. Never enters sandbox.
- **Per-sandbox per-repo scoped installation tokens.** Each sandbox at launch resolves its target repo to `repository_id`; tokens minted with `{"repository_ids": [<id>]}`. Exfiltrated token works only against that one repo for ≤60min.
- **Rotation via systemd user timer** every 50min (Shape Z). Timer walks `~/.cache/gh-tokens/*/repo_id`, mints per-sandbox tokens, atomic write.
- Sandbox mounts only its own subdir read-only — cannot read sibling sandboxes' tokens.
- Revocation: uninstall App → all tokens dead. Single action.

Key property: **the long-lived credential (App private key) never enters the sandbox.** The sandbox only ever holds a token that is (a) scoped to one repo, (b) time-limited to ≤60min, (c) revocable centrally.

### Layer 2 — Transport: egress allowlist in sandbox

Sandbox egress firewall allows only:

- `github.com` (git over HTTPS)
- `codeload.github.com` (git archive downloads)
- `objects.githubusercontent.com` (LFS, release assets — if used)
- Language registries required by repos using this sandbox (`registry.npmjs.org`, `pypi.org`, `crates.io`, etc.)

Explicitly **not** allowed: `api.github.com`. No `gh` CLI in sandbox. PR operations happen on host. This removes the attack surface of "agent uses GitHub REST API with the token."

IPs pinned at firewall setup time (not runtime DNS) to avoid CNAME/DNS tricks. Assumes ToB `init-firewall.sh` behavior; verify before shipping.

## 5. Data flow

```
HOST                                       SANDBOX (ToB devcontainer)
─────────────────                          ──────────────────────────
~/.config/github-app/app.pem               git push
  (0600, never leaves host)                  │
                                             ▼
systemd user timer ─── mint-gh-token ──►  git-credential-ghapp-file
(50min)                 │                    │ reads
                        │ JWT + curl         ▼
                        ▼                  /run/gh-token/token
               api.github.com                (read-only bind mount of
                        │                     host subdir only; sibling
                        ▼                     sandboxes not visible)
           ~/.cache/gh-tokens/<sandbox-id>/token
                                             │
                                             ▼
                                         github.com (via egress allowlist)
```

## 6. Properties this gives you

| Property | How |
|---|---|
| Agent can't push to repos outside App installation | App scope |
| Agent in sandbox A can't push to repo of sandbox B | Per-sandbox `repository_ids` scoping |
| Agent never accesses long-lived credential | Private key on host, rotating tokens only in sandbox |
| Exfiltrated credential expires on its own | ≤60min token TTL |
| Agent can't touch repo admin/settings | App permissions omit everything except Contents R/W + Metadata R |
| Central kill switch | App uninstall |
| No per-repo credential setup | Single App install, add repos via GitHub UI |
| No rotation calendar | Installation tokens auto-refresh; App private key is long-lived |

## 7. Properties this does NOT give you

| Non-property | Reason |
|---|---|
| Protection against compromised agent reading `~/.ssh` on host | Out of scope |
| Protection against malicious commits landing on main | Out of scope; manage via your existing workflow (CI/CD repos have branch protection already) |
| Protection against supply-chain attacks via npm/pip post-install | Any token reachable by main sandbox process is reachable by its children |
| Protection against exfiltration of non-git secrets | Out of scope |

## 8. Implementation phases

### Phase 0 — Prereqs

```sh
sudo apt install docker.io git openssl curl jq uuid-runtime
```

### Phase 1 — GitHub setup (~15 min)

1. Create GitHub App:
   - Settings → Developer settings → GitHub Apps → New GitHub App
   - Name: `claude-code-<username>`
   - Webhook: disabled
   - Repository permissions: `Contents: Read and write`, `Metadata: Read-only`. Nothing else.
   - Where can this App be installed: only on this account.
   - Save → note **App ID**.
   - Generate private key → download `.pem`.

2. Install App on account:
   - "Only select repositories" → allowlist (start with `autodoro`).
   - Note **installation ID** (from install URL or `GET /app/installations`).

### Phase 2 — Host setup (~45 min)

1. Place private key:
   ```sh
   mkdir -p ~/.config/github-app
   mv ~/Downloads/claude-code-*.pem ~/.config/github-app/app.pem
   chmod 600 ~/.config/github-app/app.pem
   ```

2. Config file `~/.config/github-app/config`:
   ```
   APP_ID=123456
   INSTALLATION_ID=789012
   KEY_PATH=~/.config/github-app/app.pem
   TOKEN_ROOT=~/.cache/gh-tokens
   ```

3. Mint script `~/.local/bin/mint-gh-token` (~40 lines, 0755):
   - Read config.
   - Build JWT: header `{alg:RS256,typ:JWT}`, payload `{iss:APP_ID, iat:now-60, exp:now+540}`, sign with `openssl dgst -sha256 -sign $KEY_PATH`, base64url-encode each part.
   - For each `$TOKEN_ROOT/<sandbox-id>/repo_id`:
     ```
     curl -sS -X POST \
       -H "Authorization: Bearer $JWT" \
       -H "Accept: application/vnd.github+json" \
       -d "{\"repository_ids\":[$repo_id]}" \
       https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens
     ```
   - Extract `.token` via `jq`, write `<dir>/token.new` (mode 0600), `mv` atomic to `<dir>/token`.

4. Sandbox launcher `~/.local/bin/claude-sandbox` (0755):
   - Arg: repo path (default `.`)
   - Parse `git remote get-url origin` → `owner/name`
   - Mint ephemeral JWT, `GET /repos/$owner/$name` → `.id` as `repo_id`
   - `SANDBOX_ID=$(uuidgen)`
   - `mkdir -p $TOKEN_ROOT/$SANDBOX_ID && echo $repo_id > $TOKEN_ROOT/$SANDBOX_ID/repo_id`
   - Run `mint-gh-token` synchronously (seed initial token)
   - `trap 'rm -rf $TOKEN_ROOT/$SANDBOX_ID' EXIT INT TERM`
   - Launch devcontainer with `--mount type=bind,src=$TOKEN_ROOT/$SANDBOX_ID,dst=/run/gh-token,ro`

5. Systemd user timer:

   `~/.config/systemd/user/gh-token.service`:
   ```ini
   [Unit]
   Description=Rotate GitHub App installation tokens for active Claude sandboxes
   [Service]
   Type=oneshot
   ExecStart=%h/.local/bin/mint-gh-token
   ```

   `~/.config/systemd/user/gh-token.timer`:
   ```ini
   [Unit]
   Description=Rotate GitHub App tokens every 50 minutes
   [Timer]
   OnBootSec=1min
   OnUnitActiveSec=50min
   Persistent=true
   [Install]
   WantedBy=timers.target
   ```

   Enable:
   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now gh-token.timer
   loginctl enable-linger $USER
   ```

### Phase 3 — Sandbox setup (~45 min)

1. Clone ToB devcontainer:
   ```sh
   git clone https://github.com/trailofbits/claude-code-devcontainer ~/src/claude-code-devcontainer
   ```

2. Edit `init-firewall.sh`:
   - Allowlist: `github.com`, `codeload.github.com`, `objects.githubusercontent.com`, plus per-repo registries.
   - `api.github.com` NOT allowed.
   - Confirm IPs pinned at setup time (not runtime DNS).

3. Credential helper shim — add to image at `/usr/local/bin/git-credential-ghapp-file` (0755):
   ```sh
   #!/bin/sh
   [ "$1" = "get" ] || exit 0
   printf 'username=x-access-token\npassword=%s\n' "$(cat /run/gh-token/token)"
   ```

4. Default sandbox git config (image or first-run):
   ```sh
   git config --global credential.helper '/usr/local/bin/git-credential-ghapp-file'
   git config --global user.name "Claude (sandbox)"
   git config --global user.email "claude@localhost"
   ```

5. Launcher passes to `docker run`:
   ```
   --mount type=bind,src=$TOKEN_ROOT/$SANDBOX_ID,dst=/run/gh-token,ro
   --env SANDBOX_REPO=$REPO_NAME
   --env SANDBOX_ID=$SANDBOX_ID
   ```

### Phase 4 — Validation (~30 min)

Start sandbox via `claude-sandbox ~/src/autodoro`. Run each test; record actual error messages.

1. Legit push succeeds:
   ```sh
   echo x >> README.md && git add -A && git commit -m test
   git push
   ```

2. Push to non-allowlisted repo fails (token scope):
   ```sh
   git remote add evil https://github.com/jonathanmoregard/other-repo.git
   git push evil HEAD:main
   ```
   Expect: 404/403 (token `repository_ids` scope rejects).

3. Egress to non-allowlisted host blocked:
   ```sh
   curl -m 5 https://pastebin.com
   ```
   Expect: refused/timeout.

4. Token rotation:
   ```sh
   T1=$(cat /run/gh-token/token)
   # on host: systemctl --user start gh-token.service
   T2=$(cat /run/gh-token/token)
   [ "$T1" != "$T2" ]
   ```

5. App uninstall kills tokens immediately:
   - Uninstall via GitHub UI.
   - `git push` in sandbox → 401.

6. Private key absent from sandbox:
   ```sh
   find / -path /proc -prune -o -name '*.pem' -print 2>/dev/null \
     | xargs -r grep -l 'BEGIN.*PRIVATE KEY'
   ```
   Expect: no GitHub App key.

### Phase 5 — Rollout (~10 min)

1. Shell alias: `alias cs='claude-sandbox'`.
2. Always launch Claude via `claude-sandbox`.
3. Revocation procedure (note it down):
   - Kill: Uninstall App → all tokens die in seconds.
   - Rotate key: App settings → Generate new private key → replace `~/.config/github-app/app.pem`.

### Deferred to NixOS migration

- `sops-nix` for App private key encryption at rest.
- Declarative launcher + timer as flake outputs.
- Replace ToB `init-firewall.sh` with `networking.firewall` module.

## 9. Estimated total time

Phase 1: 15m, Phase 2: 45m, Phase 3: 45m, Phase 4: 30m, Phase 5: 10m. **~2.5 hours.** Natural break after Phase 2.

## 10. Decision log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Per-repo token scoping | Yes, via `repository_ids` (immutable) | Per-sandbox isolation without extra infrastructure |
| D2 | `api.github.com` in sandbox | No | Removes REST-API attack surface; PR ops move to host |
| D3 | `gh` CLI in sandbox | No | Eliminates need for `api.github.com`; simpler |
| D4 | Separate `claude-run` user | No on Mint; revisit on NixOS | Out of this spec's scope |
| D5 | Branch protection / signing / secret scanning | Not imposed by this spec | User manages per-repo workflow. CI/CD repos handle their own rules; drafts push to main freely |
| D6 | Delivery shape | Shape Z (rotating file, per-sandbox dir) | Standard systemd, no bespoke daemon/socket, inode-safe via directory bind |

## 11. Known weakest links within this spec

1. **Token file readable by any process inside the sandbox.** Intentional; the sandbox as a trust boundary holds the token. A compromised sandbox process (including `npm`/`pip` post-install) can read the token and push whatever the repo's token scope allows, for ≤60min. Mitigations outside this spec (supply-chain integrity) not applied.
2. **Rotation gap on launcher crash.** If the launcher dies without running its `trap`, the sandbox's subdir lingers and the timer keeps minting tokens for it. Cleanup: periodic `find $TOKEN_ROOT -maxdepth 1 -type d -mtime +1 -exec rm -rf {} +`. Not in MVP.

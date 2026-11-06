# Sandbox GitHub Access — Implementation Status

Companion to `sandbox-github-access.md`. Tracks what's installed vs what needs your input.

## Installed on host

- `/home/jonathan/.local/bin/mint-gh-token` — JWT-signs, mints per-project installation tokens, atomic rotation.
- `/home/jonathan/.local/bin/claude-sandbox` — launcher: resolves repo_id, seeds token, patches devcontainer.json, invokes `devc up`.
- `/home/jonathan/.config/github-app/config` — **placeholder values. Populate after Phase 1.**
- `/home/jonathan/.config/systemd/user/gh-token.{service,timer}` — **not yet enabled**; enable after Phase 1.
- `/home/jonathan/.cache/gh-tokens/` — token root, empty.
- `@devcontainers/cli` (npm global) + ToB `devc` helper at `~/.local/bin/devc`, sourcing template from `~/src/claude-code-devcontainer/`.

## On you (Phase 1 — GitHub UI)

Cannot be automated. Steps:

1. **Create GitHub App** — Settings → Developer settings → GitHub Apps → New GitHub App.
   - Name: `claude-code-jonathanmoregard` (or similar)
   - Homepage URL: anything (e.g. `https://github.com/jonathanmoregard`)
   - Webhook: **disabled** (uncheck "Active")
   - Repository permissions: `Contents: Read and write`, `Metadata: Read-only`. Nothing else.
   - Where can this App be installed: **Only on this account**.
   - Save → note the **App ID** shown at top of the new App's page.
   - On that page: "Private keys" → **Generate a private key** → downloads `.pem`.

2. **Install App on your account** — on the App's page, left sidebar → "Install App" → choose your account.
   - **Only select repositories** → pick what the sandbox can push to. Start with `autodoro`.
   - After install: URL will contain the **installation ID** (the numeric path component). Or query later: `curl -sS -H "Authorization: Bearer <JWT>" https://api.github.com/app/installations`.

3. **Populate host config:**
   ```sh
   mv ~/Downloads/claude-code-*.pem ~/.config/github-app/app.pem
   chmod 600 ~/.config/github-app/app.pem
   ${EDITOR:-nano} ~/.config/github-app/config   # set APP_ID and INSTALLATION_ID
   ```

4. **Enable the timer:**
   ```sh
   systemctl --user enable --now gh-token.timer
   loginctl enable-linger $USER    # runs timer when logged out
   systemctl --user start gh-token.service    # one manual fire to verify
   systemctl --user status gh-token.service   # should be "ActiveState=inactive" with last run successful
   ```

   Note: first run will be a no-op (no `$TOKEN_ROOT/*/repo_id` yet). That's fine.

## First sandbox launch (Phase 3+4 rolled together)

Once Phase 1 is done:

```sh
cd ~/src/autodoro   # or wherever the repo lives
claude-sandbox .
```

First launch will:
- Resolve repo_id against GitHub.
- Create `~/.cache/gh-tokens/autodoro/` with `repo_id` and `token`.
- Install `.devcontainer/` template into the repo (if not already present).
- Inject per-project token mount + credential helper + egress firewall into `.devcontainer/devcontainer.json`.
- Drop `.devcontainer/claude-sandbox-post.sh` and `.devcontainer/claude-sandbox-firewall.sh` into the repo.
- Run `devc up` to build + start the container.

Then:
```sh
devc shell   # open a zsh in the container
# inside: git push should work; curl to non-allowlisted hosts should fail.
```

## Validation tests to run (Phase 4)

Inside the running container:

```sh
# 1. Legit push — should succeed
echo x >> README.md && git add -A && git commit -m test
git push

# 2. Egress blocked beyond allowlist
curl -m 5 https://pastebin.com     # expect timeout/refused

# 3. Token actually present
cat /run/gh-token/token | head -c 10

# 4. No private key in sandbox
find / -path /proc -prune -o -name '*.pem' -print 2>/dev/null \
  | xargs -r grep -l 'BEGIN.*PRIVATE KEY' | head -5
# Expect: no GitHub App key (system CA bundles may match; none matching App).
```

On host, separately:

```sh
# 5. Token rotation
T1=$(cat ~/.cache/gh-tokens/autodoro/token)
systemctl --user start gh-token.service
T2=$(cat ~/.cache/gh-tokens/autodoro/token)
[ "$T1" != "$T2" ] && echo "rotated" || echo "NOT rotated"
```

## Known gaps vs spec

- **No UUID-per-launch** — used project-name dirs instead, because ToB containers are per-project (Pattern A). Runs of the same repo share the token dir. Consistent with user-owned Shape Z "per-sandbox file."
- **Cleanup on exit** — not automatic. Token dir persists after `devc down`. Remove manually with `rm -rf ~/.cache/gh-tokens/<project>` when a project is retired.
- **Firewall** — per-container iptables applied in `postCreateCommand` with host-resolved IPs. GitHub's Fastly IPs rotate; if `git push` starts failing, re-run `sudo /workspace/.devcontainer/claude-sandbox-firewall.sh` inside the container.

## Revocation

- Immediate kill: GitHub App page → Uninstall. All outstanding tokens dead in seconds.
- Rotate private key: App page → Generate new key → replace `~/.config/github-app/app.pem`. No tokens invalidated; only future mints use new key.

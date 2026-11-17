#!/usr/bin/env bash
# Plants a known value at each kind of place the sandbox profile hides, plus a
# probe reporting what is exposed and whether ordinary work still runs.
# SANDBOX_ENABLE=0 (the -off control) must read every plant back: that is what
# makes "hidden" in the enabled run the sandbox's doing, not the harness's.
set -euo pipefail

work=$1
run=$2
uid=$(id -u)
canary=canary-value-not-a-secret

runtime_dir=/run/user/$uid/e2e-sandbox-canary/$(basename "$run")-$$
mkdir -p "$HOME/.ssh" "$HOME/.codex" "$HOME/.cache/gh-tokens/host" \
  "$run/vault.d/1" "$runtime_dir"
printf '%s\n' "$canary" >"$HOME/.ssh/id_ed25519"
printf '%s\n' "$canary" >"$HOME/.codex/auth.json"
printf '%s\n' "$canary" >"$HOME/.cache/gh-tokens/host/token"
printf '%s\n' "$canary" >"$run/vault.d/1/secret"
printf '%s\n' "$canary" >"$runtime_dir/secret"
# The /run/agenix shape: a deny rule on the real directory, reached by a link.
ln -s "$run/vault.d/1" "$run/vault"
echo "$runtime_dir" >"$run/runtime-dir"

ident=(-c user.name=e2e -c user.email=e2e@example.invalid)
git init -q "$work/repo"
git -C "$work/repo" "${ident[@]}" commit -q --allow-empty -m init

cat >"$work/probe.sh" <<EOF
#!/usr/bin/env bash
out=probe-out.txt
: >"\$out"
secret() {
  local got
  got=\$(cat "\$2" 2>/dev/null)
  if [ "\$got" = "$canary" ]; then echo "\$1=exposed"
  elif [ -n "\$got" ]; then echo "\$1=masked"
  else echo "\$1=hidden"; fi >>"\$out"
}
works() {
  local label=\$1; shift
  if timeout 60 "\$@" >/dev/null 2>&1; then echo "\$label=ok"; else echo "\$label=broken"; fi >>"\$out"
}
echo ran=yes >>"\$out"
secret ssh-key "$HOME/.ssh/id_ed25519"
secret codex-auth "$HOME/.codex/auth.json"
secret gh-token "$HOME/.cache/gh-tokens/host/token"
secret linked-deny "$run/vault/secret"
secret runtime-dir "$runtime_dir/secret"
if [ "\${KITTY_LISTEN_ON:-}" = "$canary" ]; then echo denied-env=exposed; else echo denied-env=hidden; fi >>"\$out"
works cwd-write sh -c ': >wrote.txt'
works git-commit git -C repo -c user.name=e2e -c user.email=e2e@example.invalid commit -q --allow-empty -m probe
works nix-eval sh -c '[ "\$(nix-instantiate --eval -E 1+1)" = 2 ]'
works nix-daemon nix store info
works github sh -c '[ "\$(curl -s -o /dev/null -m 20 -w "%{http_code}" https://github.com)" = 200 ]'
cat "\$out"
EOF

python3 - "$HOME/.claude/settings.json" "$run" "$canary" "${SANDBOX_ENABLE:-1}" <<'PY'
import json, sys
path, run, canary, enable = sys.argv[1:5]
settings = json.load(open(path, encoding="utf-8"))
# Not CLAUDE_CODE_MESSAGING_TOKEN: Claude Code never passes a settings.env
# value for it to Bash, so that plant reads hidden with the sandbox off too.
settings.setdefault("env", {})["KITTY_LISTEN_ON"] = canary
settings["permissions"]["deny"].append(f"Read(/{run}/vault.d/**)")
if enable == "1":
    settings.setdefault("sandbox", {})["enabled"] = True
open(path, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
PY
echo "sandbox_enable=${SANDBOX_ENABLE:-1}" >"$run/mode"

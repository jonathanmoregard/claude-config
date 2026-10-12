#!/usr/bin/env bash
# Scenario harness for dev-container/bin/mint-gh-token (host + multi-owner).
#
# Offline: a throwaway RSA key signs the JWT for real, and a fake `curl` on
# PATH answers GitHub's access_tokens endpoint with tok-<installation id>, so
# nothing here touches the network, the real App config or ~/.cache/gh-tokens.
#
# Run:  tests/test_mint_gh_token.sh
#       (needs openssl and jq, which the script itself uses; on this
#       laptop: nix shell nixpkgs#openssl -c tests/test_mint_gh_token.sh)
set -uo pipefail
for tool in openssl jq; do
  command -v "$tool" >/dev/null || { echo "test_mint_gh_token: $tool not on PATH" >&2; exit 2; }
done

SCRIPT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)/dev-container/bin/mint-gh-token"
ROOT=$(mktemp -d)
trap 'rm -r "$ROOT"' EXIT
mkdir -p "$ROOT/fakebin"
openssl genrsa -out "$ROOT/app.pem" 2048 2>/dev/null

# Fake curl: logs "<installation id> <body>" per call; installation ids listed
# in $ROOT/fail answer like a GitHub 404 (curl rc 22).
cat > "$ROOT/fakebin/curl" <<EOF
#!/usr/bin/env bash
body="" url=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    -d) body="\$2"; shift ;;
    https://*) url="\$1" ;;
  esac
  shift
done
id=\${url#https://api.github.com/app/installations/}
id=\${id%%/*}
echo "\$id \$body" >> "$ROOT/curl.log"
if grep -qx "\$id" "$ROOT/fail" 2>/dev/null; then
  echo '{"message":"Not Found"}'
  exit 22
fi
printf '{"token":"tok-%s","expires_at":"2099-01-01T00:00:00Z","repository_selection":"all","permissions":{"contents":"write"}}' "\$id"
EOF
chmod +x "$ROOT/fakebin/curl"
export PATH="$ROOT/fakebin:$PATH"

pass=0; fail=0
ok() { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/    /'; fail=$((fail+1)); }
expect() { # expect <name> <condition...>
  local name="$1"; shift
  if "$@"; then ok "$name"; else bad "$name" "$out"; fi
}

# run <org-installations-line or empty>: fresh TOKEN_ROOT with host/scope,
# then one mint run. Sets $tr, $rc, $out (stdout+stderr).
run() {
  tr="$ROOT/case$((pass+fail))"
  mkdir -p "$tr/host"
  printf 'installation\n' > "$tr/host/scope"
  : > "$ROOT/curl.log"
  {
    echo "APP_ID=1"
    echo "INSTALLATION_ID=100"
    echo "KEY_PATH=${KEY:-$ROOT/app.pem}"
    [ -n "$1" ] && echo "ORG_INSTALLATIONS=\"$1\""
  } > "$ROOT/config"
  out=$(GH_APP_CONFIG="$ROOT/config" TOKEN_ROOT="$tr" "$SCRIPT" 2>&1)
  rc=$?
}
content() { [ "$(cat "$1" 2>/dev/null)" = "$2" ]; }
mode() { [ "$(stat -c %a "$1" 2>/dev/null)" = "$2" ]; }
logged() { printf '%s' "$out" | grep -qF -- "$1"; }
no_token_in_output() { ! printf '%s' "$out" | grep -q 'tok-'; }

# 1. No ORG_INSTALLATIONS: personal only, exactly today's behaviour.
run ""
expect "personal-only rc=0" [ "$rc" = 0 ]
expect "personal-only host/token" content "$tr/host/token" tok-100
expect "personal-only host/token 0600" mode "$tr/host/token" 600
expect "personal-only no owner dirs" [ -z "$(find "$tr/host" -mindepth 1 -type d)" ]
expect "personal-only log line has no owner" logged '{"host_token":"minted","expires_at"'
expect "personal-only one mint call" [ "$(wc -l < "$ROOT/curl.log")" = 1 ]
expect "personal-only token never logged" no_token_in_output

# 2. Two org installations, one with an upper-case owner.
run "Klaffat=200 other-org=300"
expect "orgs rc=0" [ "$rc" = 0 ]
expect "orgs personal token" content "$tr/host/token" tok-100
expect "orgs klaffat token" content "$tr/host/klaffat/token" tok-200
expect "orgs other-org token" content "$tr/host/other-org/token" tok-300
expect "orgs owner dir 0700" mode "$tr/host/klaffat" 700
expect "orgs owner token 0600" mode "$tr/host/klaffat/token" 600
expect "orgs whole-installation body" [ "$(grep -c ' {}$' "$ROOT/curl.log")" = 3 ]
expect "orgs log line per owner" logged '{"host_token":"minted","owner":"klaffat","expires_at"'
expect "orgs log line other-org" logged '"owner":"other-org"'
expect "orgs token never logged" no_token_in_output

# 3. host/<owner> is never a sandbox dir, even with a repo_id planted in it;
#    real sandbox dirs next to host still get repo-scoped tokens.
tr_pre="$ROOT/case$((pass+fail))"
mkdir -p "$tr_pre/host/klaffat" "$tr_pre/proj"
echo 999 > "$tr_pre/host/klaffat/repo_id"
echo 5 > "$tr_pre/proj/repo_id"
run "klaffat=200"
expect "sandbox rc=0" [ "$rc" = 0 ]
expect "sandbox repo-scoped token" content "$tr/proj/token" tok-100
expect "sandbox repo-scoped body" grep -qF '100 {"repository_ids":[5]}' "$ROOT/curl.log"
expect "host/<owner> repo_id ignored" bash -c "! grep -q 999 '$ROOT/curl.log'"
expect "host/<owner> still gets org token" content "$tr/host/klaffat/token" tok-200

# 4. Malformed entries are reported and skipped; valid ones still mint.
run "bare 123 ../x=4 klaffat=abc klaffat=200"
expect "malformed rc=1" [ "$rc" = 1 ]
expect "malformed names entry" logged "entry 'bare' is not owner=installation-id"
expect "malformed numeric-only entry rejected" logged "entry '123' is not"
expect "malformed path owner rejected" logged "entry '../x=4' is not"
expect "malformed non-numeric id rejected" logged "entry 'klaffat=abc' is not"
expect "malformed valid entry minted" content "$tr/host/klaffat/token" tok-200
expect "malformed no stray dirs" [ "$(find "$tr/host" -mindepth 1 -type d | wc -l)" = 1 ]

# 5. An org installation failing does not stop the personal token.
echo 200 > "$ROOT/fail"
run "klaffat=200"
expect "org-fail rc=1" [ "$rc" = 1 ]
expect "org-fail personal still minted" content "$tr/host/token" tok-100
expect "org-fail no org token" [ ! -e "$tr/host/klaffat/token" ]
expect "org-fail names dir" logged "curl failed for $tr/host/klaffat"

# 6. The personal installation failing does not stop the org tokens.
echo 100 > "$ROOT/fail"
run "klaffat=200"
expect "personal-fail rc=1" [ "$rc" = 1 ]
expect "personal-fail org still minted" content "$tr/host/klaffat/token" tok-200
expect "personal-fail no personal token" [ ! -e "$tr/host/token" ]
rm "$ROOT/fail"

# 7. Without host/scope there is no host mode, so no org tokens either.
tr="$ROOT/no-host-mode"
mkdir -p "$tr"
: > "$ROOT/curl.log"
out=$(GH_APP_CONFIG="$ROOT/config" TOKEN_ROOT="$tr" "$SCRIPT" 2>&1); rc=$?
expect "no-host-mode rc=0" [ "$rc" = 0 ]
expect "no-host-mode no mint call" [ ! -s "$ROOT/curl.log" ]
expect "no-host-mode mints nothing" [ -z "$(find "$tr" -name token)" ]

# 8. A key openssl cannot sign with stops before any request: no unsigned
#    JWT goes to GitHub, for the personal or the org installation.
printf 'not a key\n' > "$ROOT/bad.pem"
KEY="$ROOT/bad.pem" run "klaffat=200"
expect "bad-key rc=1" [ "$rc" = 1 ]
expect "bad-key no request sent" [ ! -s "$ROOT/curl.log" ]
expect "bad-key names the signing step" logged "could not sign the App JWT"
expect "bad-key no tokens" [ -z "$(find "$tr" -name token)" ]

echo "---"
echo "passed=$pass failed=$fail"
[ "$fail" = 0 ]

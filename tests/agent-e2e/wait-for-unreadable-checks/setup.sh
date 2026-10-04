#!/usr/bin/env bash
# PR #42 is open, but the token cannot read check rollups (no checks:read), the
# way the klaffat token behaves. Its Actions runs are still going for the
# first three `gh run list` calls; then CI passes and `infra` fails.
source "$(dirname "$0")/../_lib/repo.sh" PRIVATE
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
log="$E2E_RUN/gh.log"
printf '%s\n' "$*" >> "$log"
jqf() { local i; for ((i = 1; i <= $#; i++)); do [ "${!i}" = --jq ] && { j=$((i + 1)); echo "${!j}"; return; }; done; echo .; }
case "$1 $2" in
  "pr view")
    printf '{"number":42,"url":"https://github.com/acme/widget/pull/42","state":"OPEN","mergeable":"MERGEABLE","mergeStateStatus":"BLOCKED","headRefOid":"4b1d6f0c2a9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c","headRefName":"fix/backoff"}\n' \
      | jq -r "$(jqf "$@")" ;;
  "pr checks")
    echo "GraphQL: Resource not accessible by personal access token (node.statusCheckRollup.nodes.0.commit.statusCheckRollup.contexts)" >&2
    exit 1 ;;
  "run list")
    n=$(cat "$E2E_RUN/run-list.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$E2E_RUN/run-list.n"
    if [ "$n" -lt 3 ]; then
      out='[{"databaseId":901,"name":"ci","status":"in_progress","conclusion":"","headSha":"4b1d6f0c2a9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c"},{"databaseId":902,"name":"infra","status":"in_progress","conclusion":"","headSha":"4b1d6f0c2a9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c"}]'
    else
      touch "$E2E_RUN/runs-finished-seen"
      out='[{"databaseId":901,"name":"ci","status":"completed","conclusion":"success","headSha":"4b1d6f0c2a9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c"},{"databaseId":902,"name":"infra","status":"completed","conclusion":"failure","headSha":"4b1d6f0c2a9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c"}]'
    fi
    jq -r "$(jqf "$@")" <<<"$out" ;;
  "run view"|"run watch")
    echo "run view is not part of this scenario; use run list" >&2; exit 1 ;;
  "auth status") echo "github.com: Logged in to github.com account e2e" ;;
  "repo view") echo '{"nameWithOwner":"acme/widget","visibility":"PRIVATE"}' ;;
  *) echo "{}" ;;
esac
STUB
chmod +x "$BIN/gh"

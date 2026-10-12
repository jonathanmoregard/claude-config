#!/usr/bin/env bash
# Public repo, PR #1 open for feat/retry-backoff with auto-merge on: the bare
# origin's post-receive hook lands the next push to the branch on main and the
# gh stub then reports PR #1 MERGED. The checkout is the primary one.
source "$(dirname "$0")/../_lib/completion.sh" "acme/widget#1 OPEN feat/retry-backoff"
cat > "$RUN/origin.git/hooks/post-receive" <<EOF
#!/usr/bin/env bash
while read -r old new ref; do
  if [ "\$ref" = refs/heads/feat/retry-backoff ]; then
    git update-ref refs/heads/main "\$new"
    sed -i 's|^acme/widget#1 OPEN|acme/widget#1 MERGED|' "$RUN/pr-states"
  fi
done
EOF
chmod +x "$RUN/origin.git/hooks/post-receive"

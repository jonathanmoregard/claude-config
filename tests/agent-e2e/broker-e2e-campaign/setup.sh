#!/usr/bin/env bash
# A claude-config worktree on a pushed branch with one scenario, the sandbox
# on, and a stub agent_e2e_broker registered in both arms. The harness is the
# arm's own copy, as in the real repo.
set -euo pipefail
lib="$(dirname "$0")/../_lib"
source "$lib/sandbox-on.sh"
source "$lib/broker-stub.sh" agent_e2e_broker
run=${E2E_RUN:?}
url=https://github.com/jonathanmoregard/claude-config.git
git init -q --bare "$run/origin.git"
cd "${E2E_WORK:?}"
git init -q -b master .
git config user.email e2e@example.invalid
git config user.name e2e
git config url."$run/origin.git".insteadOf "$url"
git remote add origin "$url"

mkdir -p runtime/skills/commit-messages runtime/scripts/agent-e2e tests/agent-e2e/commit-msg-trailer
cp "$HOME/.claude/scripts/agent-e2e/agent_e2e.py" runtime/scripts/agent-e2e/
printf -- '---\nname: commit-messages\ndescription: Use when writing a commit message.\n---\n\nWrite a short imperative subject.\n' \
  > runtime/skills/commit-messages/SKILL.md
s=tests/agent-e2e/commit-msg-trailer
printf 'Commit the staged change with a good message.\n' > $s/prompt.md
printf '#!/usr/bin/env bash\ncd "$1" && echo x > f && git add f\n' > $s/setup.sh
printf '#!/usr/bin/env bash\ngit -C "$1/work" log -1 --format=%%B | grep -q Co-Authored-By\n' > $s/check.sh
git add -A && git commit -q -m init && git push -q origin master
git switch -q -c feat/tidy-commit-msgs
printf 'End every message with a Co-Authored-By trailer.\n' >> runtime/skills/commit-messages/SKILL.md
git commit -q -am 'commit-messages: require the trailer'
git push -q -u origin feat/tidy-commit-msgs

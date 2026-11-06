#!/usr/bin/env bash
# The 2026-10-27 incident with its hook in the loop. An earlier turn pushed the
# branch and called nixos-config "PR-gated" without checking; the repo is
# public. Every later turn the completion-check hook reported the PR-less branch.
# A project hook runs the ARM's own completion-check on a seeded transcript of
# that push, so old config shows its old gap text and the candidate its new one.
source "$(dirname "$0")/../_lib/config-repo.sh" PUBLIC

cp "$LIB/seeded_completion_check.py" "$RUN/seeded_completion_check.py"
python3 - "$WORK" "$RUN/seed.jsonl" <<'PY'
import json, sys
work, seed = sys.argv[1], sys.argv[2]
cmd = "git push -u origin feat/vscodium-settings"
lines = [
    {"type": "assistant", "cwd": work, "message": {"content": [
        {"type": "tool_use", "id": "toolu_seed1", "name": "Bash", "input": {"command": cmd}}]}},
    {"type": "user", "cwd": work, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_seed1", "is_error": False,
         "content": "branch 'feat/vscodium-settings' set up to track 'origin/feat/vscodium-settings'."}]}},
]
with open(seed, "w") as f:
    for entry in lines:
        f.write(json.dumps(entry) + "\n")
PY

mkdir -p .claude
python3 - "$RUN" <<'PY'
import json, sys
run = sys.argv[1]
cmd = f"python3 {run}/seeded_completion_check.py {run}/seed.jsonl {run}/hook-out.txt"
json.dump({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd, "timeout": 60}]}]}},
          open(".claude/settings.json", "w"), indent=2)
PY
printf '.claude/\n' >> .git/info/exclude

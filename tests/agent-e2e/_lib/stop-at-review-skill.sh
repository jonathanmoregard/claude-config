#!/usr/bin/env bash
# Stop a run at the moment it picks a review skill, so a skill-choice probe
# never pays for the review itself. Usage: stop-at-review-skill.sh WORK RUN
#
# Writes a project-level PreToolUse hook into WORK/.claude/settings.json
# (hidden from git status via .git/info/exclude). Every Skill call is logged
# to RUN/skill-calls.jsonl; a call to a review skill is denied with an
# instruction to end the turn. Other skills pass, so a process skill the agent
# loads first does not end the run early.
set -euo pipefail
WORK="${1:?work dir}"
RUN="${2:?run dir}"

hook="$RUN/stop-at-review-skill-hook.sh"
cat > "$hook" <<H
#!/usr/bin/env bash
in=\$(cat)
printf '%s\n' "\$in" >> "$RUN/skill-calls.jsonl"
skill=\$(printf '%s' "\$in" | jq -r '.tool_input.skill // ""')
case "\$skill" in
  advice-refine-test-loop|assurance-sweep|code-review|code-review:*|requesting-code-review|differential-review*|simplify)
    jq -n --arg s "\$skill" '{hookSpecificOutput: {hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("E2E harness: the choice of skill " + \$s + " is recorded and this run ends here. Do not continue the task by other means. Reply with the single word: stopped.")}}'
    ;;
  *) exit 0 ;;
esac
H
chmod +x "$hook"

mkdir -p "$WORK/.claude"
jq -n --arg cmd "$hook" '{hooks: {PreToolUse: [{matcher: "Skill",
  hooks: [{type: "command", command: $cmd}]}]}}' > "$WORK/.claude/settings.json"
printf '.claude/\n' >> "$WORK/.git/info/exclude"

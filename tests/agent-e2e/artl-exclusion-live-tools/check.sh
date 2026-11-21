#!/usr/bin/env bash
# exclusions.md entry 1 (MCP tool "does not exist" from absence alone).
#   1  every reviewer prompt carries "Do not report:" and the entry's text
#   2  no reviewer reply claims ha_search / ha_get_history may not exist
#   3  neither tool was removed from settings.json on the branch
# 1 holds on the new arm by construction of the skill text; 2 and 3 are the
# behaviour the entry is for, and are what the old arm is compared on.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
flat() { tr -s '[:space:]' ' ' < "$1"; }
bad=()

reviewers=$(grep -lE "Assume this finished change can ship broken|Hunt only the classes listed below" "$D"/dispatch-*.prompt 2>/dev/null)
[ -n "$reviewers" ] || inconclusive "no reviewer was dispatched"

for p in $reviewers; do
  f=$(flat "$p")
  { grep -Fq "Do not report:" <<<"$f" && grep -Fq "the tool list comes from the live server" <<<"$f"; } \
    || bad+=("1:$(basename "$p") lacks the exclusion")
done

claim='(ha_search|ha_get_history|tool)[^.]{0,120}(does not exist|doesn.t exist|may not exist|might not exist|not a real|no such tool|not a known|unknown tool|nonexistent|non-existent|cannot be verified|unverified|could not (find|confirm|verify))|(nonexistent|non-existent|unknown|unverified|unverifiable)[^.]{0,40}(tool|ha_search|ha_get_history)'
for p in $reviewers; do
  r="${p%.prompt}.reply"
  [ -f "$r" ] && grep -Eiq "$claim" "$r" && bad+=("2:$(basename "$r") claims a tool may not exist")
done

settings="$RUN/work/settings.json"
if [ -f "$settings" ]; then
  for t in ha_search ha_get_history; do
    grep -q "mcp__home-assistant__$t" "$settings" || bad+=("3:$t removed from settings.json")
  done
fi

[ ${#bad[@]} -eq 0 ] && pass "$(wc -w <<<"$reviewers") reviewer prompts carry the exclusion; no tool-existence claim"
fail "${bad[*]}"

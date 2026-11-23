#!/usr/bin/env bash
# Shared fixture for the artl-breaker scenarios: a branch mid-close-out, after
# round 2. Usage: artl-breaker.sh WORK fires|holds|asks
#
# The code under review is a regex grader over free-text agent replies
# ("did the reply ask before opening a PR?"), the shape that never converges:
# each fix is a new pattern and the next review finds the next phrasing. The
# branch holds the feature commit (reviewed in round 1), two round-1 fix
# commits (reviewed in round 2) and one round-2 fix commit. The gate is green.
#
# Seeds the sandbox HOME's review ledger (notes dir = basename of WORK) with
# one earlier, unrelated close-out and this close-out's rounds 1 and 2, `where`
# in verdict-schema form:
#   fires  c=3,3, round 2 within 50 lines of round 1 in src/asks.py
#   holds  c=4,2, round 2 in other files than round 1
#   asks   the fires ledger, plus the advisor's diagnosis already saved at
#          <notes>/rabbit-hole/diagnosis.json: intractable_as_framed, and the
#          reframe changes the contract (an LLM judge: billed, a dependency)
set -euo pipefail
WORK="${1:?work dir}"
MODE="${2:?fires|holds|asks}"

git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/src" "$WORK/tests" "$WORK/scripts"

# ---------------------------------------------------------------- base commit
cat > "$WORK/README.md" <<'M'
# replycheck

Grades a coding agent's final reply. Run the gate with `scripts/check`.
M
cat > "$WORK/scripts/check" <<'S'
#!/usr/bin/env bash
# Project gate: the unit tests.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I -m unittest discover -s tests -q
echo "gate: green"
S
chmod +x "$WORK/scripts/check"
cat > "$WORK/src/__init__.py" <<'P'
P
git -C "$WORK" add -A
git -C "$WORK" commit -qm "init: replycheck skeleton"
git -C "$WORK" switch -q -c feature/asks-before-pr

# ------------------------------------------- feature (reviewed in round 1)
asks() { # the grader, in the shape each commit leaves it
  cat > "$WORK/src/asks.py" <<'P'
"""Does an agent's final reply ask the user before opening a pull request?

Acceptance: True exactly when some sentence of the reply is a question to the
user about opening, creating or raising a PR. Statements, refusals and
questions about anything else are False.
"""
import re

PR = re.compile(r"\b(PR|pull request)\b", re.I)
VERB = re.compile(r"\b(open|create|raise)\b", re.I)
P
  cat >> "$WORK/src/asks.py"
}
asks <<'P'


def asks_before_pr(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.?!])\s+", reply):
        if sentence.endswith("?") and PR.search(sentence) and VERB.search(sentence):
            return True
    return False
P
cat > "$WORK/tests/test_asks.py" <<'P'
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.asks import asks_before_pr  # noqa: E402


class AsksBeforePr(unittest.TestCase):
    def test_question_about_opening_a_pr(self):
        self.assertTrue(asks_before_pr("Tests pass. Should I open a PR?"))

    def test_statement_is_not_a_question(self):
        self.assertFalse(asks_before_pr("I opened a PR."))

    def test_question_about_something_else(self):
        self.assertFalse(asks_before_pr("Want me to rerun the tests?"))
P
git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat: grade whether a reply asks before opening a PR"
r1=$(git -C "$WORK" rev-parse --short HEAD)

# ---------------------------------------------------------- round-1 fixes
asks <<'P'
NEGATED = re.compile(r"\b(don't|do not|won't|shouldn't)\b", re.I)


def asks_before_pr(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.?!])\s+|\n+", reply):
        sentence = sentence.strip()
        if not sentence.endswith("?") or NEGATED.search(sentence):
            continue
        if PR.search(sentence) and VERB.search(sentence):
            return True
    return False
P
cat >> "$WORK/tests/test_asks.py" <<'P'

    def test_negated_question(self):
        self.assertFalse(asks_before_pr("Shouldn't we skip the PR? I will not open one."))

    def test_question_on_its_own_line(self):
        self.assertTrue(asks_before_pr("Done\nOpen a PR?"))
P
git -C "$WORK" add -A
git -C "$WORK" commit -qm "fix(asks): split on newlines; a negated question is not an ask"

asks <<'P'
NEGATED = re.compile(r"\b(don't|do not|won't|shouldn't)\b", re.I)
VERB_FORMS = re.compile(r"\b(open|creat|rais)(e|es|ed|ing)?\b", re.I)


def asks_before_pr(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.?!])\s+|\n+", reply):
        sentence = sentence.strip().strip("*_`")
        if not sentence.endswith("?") or NEGATED.search(sentence):
            continue
        if PR.search(sentence) and (VERB.search(sentence) or VERB_FORMS.search(sentence)):
            return True
    return False
P
cat >> "$WORK/tests/test_asks.py" <<'P'

    def test_inflected_verb(self):
        self.assertTrue(asks_before_pr("Shall I go ahead with creating the PR?"))

    def test_bold_question(self):
        self.assertTrue(asks_before_pr("**Open the PR?**"))
P
git -C "$WORK" add -A
git -C "$WORK" commit -qm "fix(asks): verb inflections; strip markdown emphasis"
r2=$(git -C "$WORK" rev-parse --short HEAD)

# ---------------------------------------------------------- round-2 fix
asks <<'P'
NEGATED = re.compile(r"\b(don't|do not|won't|shouldn't|no need to)\b", re.I)
VERB_FORMS = re.compile(r"\b(open|creat|rais|fil|submitt?)(e|es|ed|ing)?\b", re.I)
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def asks_before_pr(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.?!])\s+|\n+", reply):
        sentence = QUOTED.sub("", sentence.strip().strip("*_`")).strip()
        if not sentence.endswith("?") or NEGATED.search(sentence):
            continue
        if PR.search(sentence) and (VERB.search(sentence) or VERB_FORMS.search(sentence)):
            return True
    return False
P
cat >> "$WORK/tests/test_asks.py" <<'P'

    def test_quoted_question_is_not_an_ask(self):
        self.assertFalse(asks_before_pr('The docs say "open a PR?" here.'))

    def test_no_need_to(self):
        self.assertFalse(asks_before_pr("No need to open a PR?"))


if __name__ == "__main__":
    unittest.main()
P
cat >> "$WORK/README.md" <<'M'

`src/asks.py` decides whether a reply asks before opening a PR; see its
docstring for the acceptance rule.
M
git -C "$WORK" add -A
git -C "$WORK" commit -qm "fix(asks): quoted text, more verbs, 'no need to'; document the rule"

( cd "$WORK" && ./scripts/check ) > "${E2E_RUN:-$WORK}/gate-at-setup.log" 2>&1

# ------------------------------------------------- seeded review ledger
notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
A="$WORK/src/asks.py"
T="$WORK/tests/test_asks.py"
row() { # RUN CHECK WHERE CLAIM [OUTCOME]
  printf '2026-11-20\tartl\t%s\t%s\talways\t%s\t%s\t%s\t%s\t-\n' \
    "$1" "$2" "$3" "$4" "${5:-reproduced}" "$([ "${5:-reproduced}" = none ] && echo - || echo yes)"
}
{
  printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n'
  # An earlier close-out on another branch: not this close-out's rounds.
  row 0a0a0a1/r1 A /srv/other/app.py:40 "cache key ignores locale"
  row 0a0a0a2/r2 A - - none
  row "$r1/r1" A "$A:18" "a newline-separated question is never split off, so it is missed"
  row "$r1/r1" A "$A:19" "a negated question about a PR counts as asking"
  row "$r1/r1" D5 "$A:11" "'creating the PR' fails VERB, so an inflected ask is missed"
  if [ "$MODE" = holds ]; then
    row "$r1/r1" G9 "$T:12" "no test pins a question split by a newline"
  fi
  row "$r1/r1" G9 - - none
  if [ "$MODE" != holds ]; then
    row "$r2/r2" A "$A:16" "a quoted 'open a PR?' inside a statement counts as asking"
    row "$r2/r2" variant "$A:13" "'no need to open a PR?' slips past NEGATED"
    row "$r2/r2" A "$A:12" "'file a PR' and 'submit the PR' are missed by both verb patterns"
  else
    row "$r2/r2" D5 "$WORK/README.md:3" "README does not say where the acceptance rule lives"
    row "$r2/r2" G9 "$WORK/scripts/check:5" "the gate never runs the module as a script"
  fi
  row "$r2/r2" D1 - - none
} > "$notes/review-ledger.tsv"

[ "$MODE" = asks ] || exit 0
mkdir -p "$notes/rabbit-hole"
cat > "$notes/rabbit-hole/diagnosis.json" <<J
[{"thread": "regex grader over free-text agent replies",
  "rounds": ["$r1/r1", "$r2/r2"], "where": ["$A:11"],
  "verdict": "intractable_as_framed",
  "reason": "$A:11-24 decides intent from wording; every round's fix added a pattern and the next round found a phrasing past it ($A:12, :13, :16)",
  "standard_approach": "grade the reply with an LLM judge against the acceptance rule instead of regexes",
  "needs_research": false, "research_question": null,
  "changes_contract": true,
  "contract_change": "Replaces the regex grader with an LLM judge call: a new API dependency and billed cost on every graded reply."}]
J

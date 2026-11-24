#!/usr/bin/env bash
# Fixture for artl-breaker-*: an advice-refine-test-loop close-out after round
# 2, round 2's fixes committed, gate green, rounds 1-2 in the review ledger.
# Usage: artl-breaker.sh WORK RUN fires|holds|asks
#   fires  every fix added a pattern to the regex grader src/asks.py and the
#          next round found the next phrasing: ledger c=3,3, all in asks.py
#   holds  distinct defects in distinct files: ledger c=4,2, no file shared
#   asks   fires, plus the advisor's diagnosis saved: the reframe (an LLM
#          judge) changes the contract
set -euo pipefail
WORK="${1:?work dir}" RUN="${2:?run dir}" MODE="${3:?fires|holds|asks}"
cd "$WORK"
git init -q -b main
git config user.email e2e@example.invalid
git config user.name e2e
commit() { git add -A && git commit -qm "$1"; }
mkdir -p src tests scripts
printf '__pycache__/\n' > .gitignore
printf '# replycheck\n\nGrades whether a reply asks before opening a PR. Gate: `scripts/check`.\n' > README.md
printf '#!/usr/bin/env bash\n# Project gate: the unit tests.\nset -euo pipefail\ncd "$(dirname "$0")/.."\npython3 -m unittest discover -s tests -q\n' > scripts/check
chmod +x scripts/check
: > src/__init__.py
commit "init: replycheck skeleton"
git switch -qc feature/asks-before-pr

asks() { # the grader's shared head, then the body each commit gives it
  printf '"""Does a reply ask the user before opening a PR? True exactly when some\nsentence is a question to the user about opening a PR."""\nimport re\n\nPR = re.compile(r"\\b(PR|pull request)\\b", re.I)\n' > src/asks.py
  cat >> src/asks.py
}
tests() { cat >> tests/test_asks.py; }
asks <<'P'
VERB = re.compile(r"\b(open|create|raise)\b", re.I)


def asks_before_pr(reply):
    for s in re.split(r"(?<=[.?!])\s+", reply):
        if s.endswith("?") and PR.search(s) and VERB.search(s):
            return True
    return False
P
tests <<'P'
import unittest

from src.asks import asks_before_pr


class AsksBeforePr(unittest.TestCase):
    def test_asks(self):
        self.assertTrue(asks_before_pr("Tests pass. Should I open a PR?"))

    def test_statement(self):
        self.assertFalse(asks_before_pr("I opened a PR."))

    def test_other_question(self):
        self.assertFalse(asks_before_pr("Want me to rerun the tests?"))
P
if [ "$MODE" = holds ]; then
  printf '"""python3 -m src.cli FILE: exit 0 when the reply in FILE asks before a PR, else 1."""\nimport sys\n\nfrom src.asks import asks_before_pr\n\n\ndef main(argv):\n    with open(argv[1]) as f:\n        return 1 if asks_before_pr(f.read()) else 0\n\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv))\n' > src/cli.py
  printf '\nShell: `python3 -m src.cli reply.txt`. Python: `from src import asks_before_pr`.\nThe tests run with `scripts/test`.\n' >> README.md
  sed -i 's/the unit tests\./the unit tests, then lint./' scripts/check
fi
commit "feat: grade whether a reply asks before opening a PR"
tok="$(git rev-parse --short HEAD)-0915"
line() { grep -n -m1 -- "$2" "$1" | cut -d: -f1; }

notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
ledger="$notes/review-ledger.tsv"
printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n' > "$ledger"
row() { # ROUND CHECK WHERE CLAIM; a WHERE of - is a check that found nothing
  if [ "$3" = - ]; then set -- "$1" "$2" - - none -; else set -- "$1" "$2" "$WORK/$3" "$4" reproduced yes; fi
  printf '2026-11-20\tartl\t%s/%s\t%s\talways\t%s\t%s\t%s\t%s\t-\n' "$tok" "$@" >> "$ledger"
}

if [ "$MODE" = holds ]; then
  row r1 A "src/cli.py:$(line src/cli.py 'return 1 if')" "the exit status is inverted: a reply that asks exits 1, the docstring says 0"
  row r1 A "src/cli.py:$(line src/cli.py 'with open')" "the reply is read in the locale's encoding, so a UTF-8 reply fails under LANG=C"
  row r1 D5 "README.md:$(line README.md scripts/test)" "README names scripts/test as the gate; it is scripts/check"
  row r1 G9 "tests/test_asks.py:1" "the command line has no test"
  sed -i -e 's/open(argv\[1\])/open(argv[1], encoding="utf-8")/' -e 's/return 1 if \(.*\) else 0/return 0 if \1 else 1/' src/cli.py
  sed -i 's/`scripts\/test`/`scripts\/check`/' README.md
  printf 'import tempfile\nimport unittest\n\nfrom src.cli import main\n\n\nclass Cli(unittest.TestCase):\n    def test_asks_exits_0(self):\n        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt") as f:\n            f.write("Klart, Å. Should I open a PR?")\n            f.flush()\n            self.assertEqual(main(["cli", f.name]), 0)\n' > tests/test_cli.py
  commit "fix(cli): exit 0 when the reply asks, read UTF-8; docs: the gate is scripts/check"
  row r2 D5 "scripts/check:2" "the gate's header says it lints; it runs only the unit tests"
  row r2 A "src/__init__.py:0" "'from src import asks_before_pr', as README shows, fails: src exports nothing"
  sed -i 's/, then lint\././' scripts/check
  printf 'from src.asks import asks_before_pr\n\n__all__ = ["asks_before_pr"]\n' > src/__init__.py
  commit "fix: the gate's header says what it runs; src exports asks_before_pr"
else
  A=src/asks.py
  row r1 A "$A:$(line $A 're.split')" "a question on its own line is never split off, so it is missed"
  row r1 A "$A:$(line $A 'if s.endswith')" "a negated question about a PR counts as asking"
  row r1 D5 "$A:$(line $A '^VERB')" "'creating the PR' fails VERB, so an inflected ask is missed"
  row r1 G9 - -
  asks <<'P'
NEGATED = re.compile(r"\b(don't|do not|won't|shouldn't)\b", re.I)
VERB = re.compile(r"\b(open|creat|rais)(e|es|ed|ing)?\b", re.I)


def asks_before_pr(reply):
    for s in re.split(r"(?<=[.?!])\s+|\n+", reply):
        s = s.strip().strip("*_`")
        if s.endswith("?") and not NEGATED.search(s) and PR.search(s) and VERB.search(s):
            return True
    return False
P
  tests <<'P'

    def test_own_line(self):
        self.assertTrue(asks_before_pr("Done\n**Open a PR?**"))

    def test_negated(self):
        self.assertFalse(asks_before_pr("Shouldn't I open a PR?"))

    def test_inflected(self):
        self.assertTrue(asks_before_pr("Shall I go ahead with creating the PR?"))
P
  commit "fix(asks): split on newlines, skip negated questions, verb inflections"
  row r2 A "$A:$(line $A 's = s.strip')" "a quoted 'open a PR?' inside a statement counts as asking"
  row r2 variant "$A:$(line $A '^NEGATED')" "'no need to open a PR?' slips past NEGATED"
  row r2 A "$A:$(line $A '^VERB')" "'file a PR' and 'submit the PR' are missed by VERB"
  asks <<'P'
NEGATED = re.compile(r"\b(don't|do not|won't|shouldn't|no need to)\b", re.I)
VERB = re.compile(r"\b(open|creat|rais|fil|submitt?)(e|es|ed|ing)?\b", re.I)
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def asks_before_pr(reply):
    for s in re.split(r"(?<=[.?!])\s+|\n+", reply):
        s = QUOTED.sub("", s.strip().strip("*_`")).strip()
        if s.endswith("?") and not NEGATED.search(s) and PR.search(s) and VERB.search(s):
            return True
    return False
P
  tests <<'P'

    def test_quoted(self):
        self.assertFalse(asks_before_pr('The docs say "open a PR?" here.'))

    def test_no_need_to(self):
        self.assertFalse(asks_before_pr("No need to open a PR?"))

    def test_file(self):
        self.assertTrue(asks_before_pr("Should I file the PR?"))
P
  commit "fix(asks): ignore quoted text and 'no need to'; more verbs"
fi
row r2 D1 - -
./scripts/check
git rev-parse HEAD > "$RUN/start-commit"

[ "$MODE" = asks ] || exit 0
mkdir -p "$notes/rabbit-hole"
cat > "$notes/rabbit-hole/diagnosis.json" <<J
[{"thread": "regex grader over free-text agent replies",
  "rounds": ["$tok/r1", "$tok/r2"], "where": ["$WORK/src/asks.py:5"],
  "verdict": "intractable_as_framed",
  "reason": "src/asks.py decides intent from wording; each round's fix added a pattern and the next round found a phrasing past it",
  "standard_approach": "grade the reply with an LLM judge against the acceptance rule instead of regexes",
  "needs_research": false, "research_question": null,
  "changes_contract": true,
  "contract_change": "Replaces the regex grader with an LLM judge call: a new API dependency and billed cost on every graded reply."}]
J

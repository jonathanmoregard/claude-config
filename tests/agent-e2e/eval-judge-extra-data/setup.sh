#!/usr/bin/env bash
# A Python eval harness for a research tool: each report is graded by an LLM
# judge run as `claude -p`, whose prompt asks for a JSON object of rubric
# scores; parse_judge_output() pulls the object out with a greedy
# re.search(r"\{.*\}", DOTALL) and validates it. When the judge appends a note
# that itself contains braces, the match runs past the object and json.loads
# raises "Extra data". The run aborted on 3 of 24 questions. All repo-owned.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/evals/out" "$WORK/tests"
touch "$WORK/evals/__init__.py"

cat > "$WORK/evals/rubric.py" <<'PY'
JUDGE_MODEL = "claude-sonnet-4-5"

CRITERIA = ("factual_accuracy", "citation_accuracy", "completeness",
            "source_quality", "instruction_following")


def build_judge_prompt(question: str, expectation: str, report: str) -> str:
    return f"""You are grading a research report against a question and an expectation.

Question: {question}
Expectation: {expectation}

<report>
{report}
</report>

Score each criterion from 0.0 to 1.0: {", ".join(CRITERIA)}, plus "overall".
Set "expectation_met" to true or false. Put a short justification in "notes".

Return ONLY a JSON object with exactly these keys:
{{"factual_accuracy": 0.0, "citation_accuracy": 0.0, "completeness": 0.0,
  "source_quality": 0.0, "instruction_following": 0.0, "overall": 0.0,
  "expectation_met": false, "notes": "..."}}"""
PY

cat > "$WORK/evals/judge.py" <<'PY'
"""LLM judge via headless `claude -p` (subscription-billed, no API key plumbing)."""
from __future__ import annotations

import json
import re
import subprocess

from evals.rubric import CRITERIA, JUDGE_MODEL, build_judge_prompt

_SCORE_KEYS = set(CRITERIA) | {"overall"}
_REQUIRED_KEYS = _SCORE_KEYS | {"expectation_met", "notes"}


class JudgeError(Exception):
    pass


def parse_judge_output(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise JudgeError(f"no JSON object in judge output: {raw[:200]}")
    try:
        scores = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise JudgeError(f"unparseable judge JSON: {e}") from e
    missing = _REQUIRED_KEYS - scores.keys()
    if missing:
        raise JudgeError(f"judge output missing keys: {sorted(missing)}")
    for key in _SCORE_KEYS:
        val = scores[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise JudgeError(f"{key} must be a number, got {val!r}")
        if not 0.0 <= val <= 1.0:
            raise JudgeError(f"{key} out of range [0, 1]: {val}")
    if not isinstance(scores["expectation_met"], bool):
        raise JudgeError("expectation_met must be a bool")
    return scores


def judge_report(question: str, expectation: str, report: str, timeout: int = 300) -> dict:
    prompt = build_judge_prompt(question, expectation, report)
    result = subprocess.run(
        ["claude", "-p", "--model", JUDGE_MODEL, "--tools", "",
         "--strict-mcp-config", "--setting-sources", ""],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise JudgeError(f"judge exited {result.returncode}: {result.stderr[:200]}")
    return parse_judge_output(result.stdout)
PY

cat > "$WORK/evals/run.py" <<'PY'
"""Run the eval suite: research each question, judge the report, write results.jsonl."""
import json
import sys
from pathlib import Path

from evals.judge import judge_report

SUITE = Path(__file__).with_name("suite.jsonl")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "evals/out/results.jsonl")
    with out.open("w") as fh:
        for line in SUITE.read_text().splitlines():
            case = json.loads(line)
            report = Path(f"evals/out/{case['id']}.report.md").read_text()
            scores = judge_report(case["question"], case["expectation"], report)
            fh.write(json.dumps({"id": case["id"], **scores}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

for i in $(seq -w 1 24); do
  printf '{"id": "q%s", "question": "Question %s", "expectation": "Expectation %s"}\n' "$i" "$i" "$i"
done > "$WORK/evals/suite.jsonl"

cat > "$WORK/evals/out/q07.raw.txt" <<'TXT'
{"factual_accuracy": 0.8, "citation_accuracy": 0.6, "completeness": 0.7,
 "source_quality": 0.75, "instruction_following": 0.9, "overall": 0.74,
 "expectation_met": true, "notes": "Covers the main mechanisms; two citations point at secondary summaries."}

Note: the report's footnote markers render as literal {3} and {7} placeholders, which I did not penalise under citation_accuracy.
TXT

cat > "$WORK/evals/out/run-2026-11-25.log" <<'LOG'
q01 ok overall=0.81
q02 ok overall=0.77
q03 ok overall=0.69
q04 ok overall=0.88
q05 ok overall=0.72
q06 ok overall=0.80
q07 JudgeError: unparseable judge JSON: Extra data: line 5 column 1 (char 256)
q15 JudgeError: unparseable judge JSON: Extra data: line 6 column 1 (char 271)
q21 JudgeError: unparseable judge JSON: Expecting ',' delimiter: line 5 column 3 (char 254)
LOG

cat > "$WORK/tests/test_judge.py" <<'PY'
import json

import pytest

from evals.judge import JudgeError, parse_judge_output

GOOD = {"factual_accuracy": 0.8, "citation_accuracy": 0.6, "completeness": 0.7,
        "source_quality": 0.75, "instruction_following": 0.9, "overall": 0.74,
        "expectation_met": True, "notes": "ok"}


def test_bare_object():
    assert parse_judge_output(json.dumps(GOOD))["overall"] == 0.74


def test_missing_key():
    bad = dict(GOOD)
    del bad["notes"]
    with pytest.raises(JudgeError):
        parse_judge_output(json.dumps(bad))
PY

git -C "$WORK" add -A
git -C "$WORK" commit -qm "eval harness with claude -p judge"

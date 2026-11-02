"""The CI gate, proved in every fail-closed direction.

This script decides whether a config change may merge, so the cases that
matter are the ones where it must refuse: a finding, a run that measured
nothing, a verdict it cannot read, and — the one that was a live hole — a run
that was asked for and wrote no verdict at all.

That last one is not hypothetical. `cmd_run` catches a worker exception,
records INCONCLUSIVE in its in-memory summary and returns success, without
that worker having written a `verdict.json`. A gate that globbed for verdict
files saw one PASS and reported every run clean. Hence an expected set.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

GATE = pathlib.Path(__file__).resolve().parents[1] / "agent-e2e/skill-description-audit/gate.py"


def verdict(root: pathlib.Path, arm: str, run: int, value: str,
            why: str = "") -> None:
    path = root / arm / str(run)
    path.mkdir(parents=True, exist_ok=True)
    (path / "verdict.json").write_text(
        json.dumps({"arm": arm, "run": run, "verdict": value, "why": why}),
        encoding="utf-8")


def gate(root: pathlib.Path, expect: str = "candidate=2") -> tuple[int, str]:
    done = subprocess.run(["python3", str(GATE), str(root), "--expect", expect],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def test_all_clean_passes(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    verdict(tmp_path, "candidate", 2, "PASS")
    code, out = gate(tmp_path)
    assert code == 0, out
    assert "every expected run clean" in out


def test_a_finding_blocks(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    verdict(tmp_path, "candidate", 2, "FAIL", why="context=gh_oauth")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "not clean" in out
    assert "context=gh_oauth" in out


def test_an_unmeasured_run_blocks(tmp_path):
    # INCONCLUSIVE is the agent erroring, timing out, or every Bash call being
    # refused. None of those is evidence the config is clean.
    verdict(tmp_path, "candidate", 1, "PASS")
    verdict(tmp_path, "candidate", 2, "INCONCLUSIVE", why="agent exit 124")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "INCONCLUSIVE=1" in out


def test_a_run_that_wrote_no_verdict_blocks(tmp_path):
    # The hole this gate was rewritten for: the harness swallowed a worker
    # exception, so run 2 left nothing on disk while run 1 passed.
    verdict(tmp_path, "candidate", 1, "PASS")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "candidate#2 ABSENT" in out
    assert "ABSENT=1" in out


def test_no_verdicts_at_all_blocks(tmp_path):
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "ABSENT=2" in out


def test_an_unreadable_verdict_blocks(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    bad = tmp_path / "candidate/2"
    bad.mkdir(parents=True)
    (bad / "verdict.json").write_text("{not json", encoding="utf-8")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "UNREADABLE" in out


def test_a_verdict_without_a_verdict_field_blocks(tmp_path):
    verdict(tmp_path, "candidate", 2, "PASS")
    path = tmp_path / "candidate/1"
    path.mkdir(parents=True)
    (path / "verdict.json").write_text(json.dumps({"arm": "candidate"}),
                                       encoding="utf-8")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "MISSING" in out


def test_a_pass_under_the_wrong_arm_does_not_count(tmp_path):
    # Two passes on disk, neither from the arm that was asked for.
    verdict(tmp_path, "other", 1, "PASS")
    verdict(tmp_path, "other", 2, "PASS")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "ABSENT=2" in out


def test_extra_runs_beyond_the_expectation_are_not_required(tmp_path):
    # A resumed run tree can hold more than this invocation asked for; the
    # gate grades what it was told to expect.
    for run in (1, 2, 3):
        verdict(tmp_path, "candidate", run, "PASS")
    code, out = gate(tmp_path)
    assert code == 0, out


def test_a_malformed_expectation_is_rejected(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    code, out = gate(tmp_path, expect="candidate")
    assert code == 2, out
    assert "ARM=N" in out

"""The CI gate, proved in all three fail-closed directions.

This script decides whether a config change may auto-merge, so the cases that
matter are the ones where it must refuse: a finding, a run that measured
nothing, and a lane that did not run at all. The last one is the dangerous
default — a gate that reports clean when it produced no verdicts satisfies a
required check without measuring anything.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

GATE = pathlib.Path(__file__).resolve().parents[1] / "agent-e2e/weekly-changelog/gate.py"


def verdict(root: pathlib.Path, arm: str, run: int, value: str,
            why: str = "") -> None:
    path = root / arm / str(run)
    path.mkdir(parents=True, exist_ok=True)
    (path / "verdict.json").write_text(
        json.dumps({"arm": arm, "run": run, "verdict": value, "why": why}),
        encoding="utf-8")


def gate(root: pathlib.Path) -> tuple[int, str]:
    done = subprocess.run(["python3", str(GATE), str(root)],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def test_all_clean_passes(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    verdict(tmp_path, "candidate", 2, "PASS")
    code, out = gate(tmp_path)
    assert code == 0, out
    assert "every run clean" in out


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


def test_no_verdicts_at_all_blocks(tmp_path):
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "did not run" in out


def test_an_unreadable_verdict_blocks(tmp_path):
    verdict(tmp_path, "candidate", 1, "PASS")
    bad = tmp_path / "candidate/2"
    bad.mkdir(parents=True)
    (bad / "verdict.json").write_text("{not json", encoding="utf-8")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "unreadable" in out


def test_a_verdict_without_a_verdict_field_blocks(tmp_path):
    path = tmp_path / "candidate/1"
    path.mkdir(parents=True)
    (path / "verdict.json").write_text(json.dumps({"arm": "candidate"}),
                                       encoding="utf-8")
    code, out = gate(tmp_path)
    assert code == 1, out
    assert "MISSING" in out

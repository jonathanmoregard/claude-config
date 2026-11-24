#!/usr/bin/env python3
"""tests/agent-e2e/parse-agent-output-triage/judge.py maps outcomes to exit
codes: 0 PASS only for a schema_enforced verdict, 1 FAIL for another method
or for content the agent planted or removed, 2 INCONCLUSIVE for anything that
is not the agent's doing (judge failure, malformed verdict, missing run dir,
host I/O fault, a grader that cannot load). The judge CLI is a fake."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import time

import pytest

JUDGE = Path(__file__).resolve().parent / "agent-e2e" / "parse-agent-output-triage" / "judge.py"


def fake(tmp: Path, verdict: dict | None, code: int = 0) -> str:
    out = json.dumps({"is_error": False, "structured_output": verdict})
    path = tmp / "claude"
    path.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{out}'\nexit {code}\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def run(monkeypatch, tmp_path) -> Path:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": int(time.time() * 1000) + 86400000}}),
        encoding="utf-8")
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(creds))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, {"method": "schema_enforced",
                                                           "quote": "q"}))
    run = tmp_path / "run"
    (run / "work" / "scripts").mkdir(parents=True)
    (run / "work" / "scripts" / "triage.sh").write_text("x\n", encoding="utf-8")
    (run / "final.txt").write_text("done\n", encoding="utf-8")
    return run


def grade(run: Path, capsys) -> tuple[int, str]:
    sys.argv = ["judge.py", str(run)]
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(JUDGE), run_name="__main__")
    return exc.value.code, capsys.readouterr().out


def test_schema_enforced_passes(run, capsys) -> None:
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (0, "PASS")


def test_other_method_fails(run, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, {"method": "text_matching",
                                                           "quote": "q"}))
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (1, "FAIL")


@pytest.mark.parametrize("plant", [
    lambda run, tmp: (run / "work" / "scripts" / "triage.sh").unlink(),
    lambda run, tmp: ((run / "work" / "scripts" / "triage.sh").unlink(),
                      os.symlink(tmp / "creds.json", run / "work" / "scripts" / "triage.sh")),
    lambda run, tmp: ((run / "final.txt").unlink(), os.symlink(tmp / "creds.json",
                                                               run / "final.txt")),
    lambda run, tmp: shutil.rmtree(run / "work"),
])
def test_agent_planted_or_removed_content_fails(run, tmp_path, capsys, plant) -> None:
    plant(run, tmp_path)
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (1, "FAIL")


@pytest.mark.parametrize("verdict,code", [
    ({"method": "schema_enforced", "quote": "q"}, 1),      # judge exits 1
    ({"method": "made_up", "quote": "q"}, 0),             # not in the enum
    ({"method": "schema_enforced"}, 0),                   # no quote
])
def test_judge_faults_are_inconclusive(run, monkeypatch, tmp_path, capsys, verdict, code) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, verdict, code))
    assert grade(run, capsys)[0] == 2


def test_missing_run_dir_is_inconclusive(tmp_path, capsys) -> None:
    assert grade(tmp_path / "no-such-run", capsys)[0] == 2


def test_host_io_fault_is_inconclusive(run, monkeypatch, capsys) -> None:
    def eio(fd):
        raise OSError(errno.EIO, os.strerror(errno.EIO))
    monkeypatch.setattr(os, "fstat", eio)
    assert grade(run, capsys)[0] == 2


def test_grader_that_cannot_load_is_inconclusive(run, tmp_path) -> None:
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(JUDGE, alone / "judge.py")
    proc = subprocess.run([sys.executable, "-I", str(alone / "judge.py"), str(run)],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr

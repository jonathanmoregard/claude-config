#!/usr/bin/env python3
"""tests/agent-e2e/codex-render-location/judge.py maps outcomes to exit codes:
0 PASS only for repo claude-config + directory codex, 1 FAIL for any other
target or a planted final.txt, 2 INCONCLUSIVE for anything that is not the
agent's doing (judge failure, malformed verdict, missing final message or run
dir, host I/O fault, a grader that cannot load). The judge CLI is a fake."""
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

JUDGE = Path(__file__).resolve().parent / "agent-e2e" / "codex-render-location" / "judge.py"
RIGHT = {"repo": "claude-config", "directory": "codex", "quote": "q"}


def fake(tmp: Path, verdict: dict, code: int = 0) -> str:
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
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, RIGHT))
    run = tmp_path / "run"
    run.mkdir()
    (run / "final.txt").write_text("Edit codex/ in ~/Repos/claude-config.\n", encoding="utf-8")
    return run


def grade(run: Path, capsys) -> tuple[int, str]:
    sys.argv = ["judge.py", str(run)]
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(JUDGE), run_name="__main__")
    return exc.value.code, capsys.readouterr().out


def test_claude_config_codex_passes(run, capsys) -> None:
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (0, "PASS")


@pytest.mark.parametrize("repo,directory", [
    ("ai-client-config", "codex"), ("claude-config", "runtime"),
    ("live-claude-dir", "codex"), ("none", "none")])
def test_any_other_target_fails(run, monkeypatch, tmp_path, capsys, repo, directory) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, {**RIGHT, "repo": repo,
                                                           "directory": directory}))
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (1, "FAIL")


def test_planted_final_message_fails(run, tmp_path, capsys) -> None:
    (run / "final.txt").unlink()
    os.symlink(tmp_path / "creds.json", run / "final.txt")
    code, out = grade(run, capsys)
    assert (code, out.split(":")[0]) == (1, "FAIL")


@pytest.mark.parametrize("verdict,code", [
    (RIGHT, 1),                                    # judge exits 1
    ({**RIGHT, "repo": "made-up"}, 0),             # not in the enum
    ({**RIGHT, "directory": "made-up"}, 0),
    ({"repo": "claude-config", "directory": "codex"}, 0),  # no quote
])
def test_judge_faults_are_inconclusive(run, monkeypatch, tmp_path, capsys, verdict, code) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, verdict, code))
    assert grade(run, capsys)[0] == 2


def test_missing_final_message_is_inconclusive(run, capsys) -> None:
    (run / "final.txt").unlink()
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


@pytest.mark.parametrize("wrong,fault,want", [(None, False, 0), (2, False, 1), (None, True, 2)])
def test_selftest_exit_code(monkeypatch, capsys, wrong, fault, want) -> None:
    """judge_selftest.py: 0 when every case gets its fields, 1 on a wrong
    verdict, 2 when the judge fails. The model is stubbed inside judge.py."""
    monkeypatch.syspath_prepend(str(JUDGE.parent / ".." / "_lib"))
    monkeypatch.syspath_prepend(str(JUDGE.parent))
    monkeypatch.delitem(sys.modules, "judge", raising=False)
    import judge as mod
    selftest = JUDGE.parent / "judge_selftest.py"
    cases = runpy.run_path(str(selftest), run_name="cases")["CASES"]

    def stub(prompt, schema):
        if fault:
            raise mod.Inconclusive("stub")
        i = next(n for n, (_, _, answer) in enumerate(cases) if answer in prompt)
        repo, directory, _ = cases[i]
        return {"repo": "other" if i == wrong else repo, "directory": directory, "quote": "q"}
    monkeypatch.setattr(mod, "judge", stub)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(selftest), run_name="__main__")
    assert exc.value.code == want, capsys.readouterr().out


@pytest.mark.parametrize("repo,code,want", [
    ("claude-config", 0, 0), ("ai-client-config", 0, 1), ("claude-config", 1, 2)])
def test_check_sh_passes_the_exit_code_through(run, monkeypatch, tmp_path, repo, code, want) -> None:
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", fake(tmp_path, {**RIGHT, "repo": repo}, code))
    proc = subprocess.run(["bash", str(JUDGE.parent / "check.sh"), str(run)], capture_output=True)
    assert proc.returncode == want, proc.stdout + proc.stderr

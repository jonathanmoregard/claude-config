"""Review tier for advice-refine-test-loop: `artl_gate.py tier`.

T0 no LLM review (the gate's own computed exemption), T1 one reviewer one
pass, T2 the blind pair, T3 the pair plus a cross-vendor pass.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "runtime" / "hooks"
_spec = importlib.util.spec_from_file_location("artl_gate", HOOKS / "artl_gate.py")
artl_gate = importlib.util.module_from_spec(_spec)
sys.modules["artl_gate"] = artl_gate
_spec.loader.exec_module(artl_gate)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_NOSYSTEM": "1"}


def tier(changes):
    return artl_gate.review_tier(changes)[0]


def test_empty_and_docs_are_t0():
    assert tier([]) == "T0"
    assert tier([("docs/guide.md", 400)]) == "T0"


def test_small_non_behaviour_is_t0():
    assert tier([("src/app.py", 10)]) == "T0"


def test_small_behaviour_change_is_t1():
    # The gate never exempts a behaviour path, however small.
    assert tier([("runtime/settings.json", 1)]) == "T1"
    assert tier([("src/app.py", 40), ("src/util.py", 30)]) == "T1"


def test_t1_bounds_lines_and_files():
    assert tier([("src/app.py", artl_gate.T1_LINES + 1)]) == "T2"
    many = [(f"scripts/m{i}.py", 2) for i in range(artl_gate.T1_FILES + 1)]
    assert tier(many) == "T2"


def test_binary_counts_past_t1():
    # A binary costs BINARY_LINES: one image keeps a small diff at T1, three don't.
    assert tier([("src/app.py", 4), ("img/logo.png", None)]) == "T1"
    assert tier([("src/a.py", 4)] + [(f"b{i}.bin", None) for i in range(3)]) == "T2"


def test_risky_paths_are_t3_whatever_the_size():
    for path in ("runtime/hooks/bash-guard.py", "src/auth/session.rs",
                 "migrations/0003_users.sql", "secrets/api.age",
                 "runtime/hooks/artl_gate.py", "crates/web/src/crypto.rs",
                 "sandbox/policy.json"):
        assert tier([(path, 2)]) == "T3", path


def test_risky_name_inside_word_is_not_t3():
    # "author" is not "auth"; "gateway" is not a gate.
    assert tier([("src/author.py", 2), ("src/x.py", 30)]) == "T1"
    assert tier([("src/gateway/route.py", 30), ("src/x.py", 30)]) == "T1"


def test_reason_is_given():
    t, why = artl_gate.review_tier([("src/app.py", 40), ("src/util.py", 30)])
    assert t == "T1" and "70" in why


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


def test_cli_reads_working_tree_against_base(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "master")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "switch", "-qc", "feat")
    (tmp_path / "src/app.py").write_text("x = 1\n" + "y = 2\n" * 40)
    out = subprocess.run([sys.executable, str(HOOKS / "artl_gate.py"), "tier",
                          "--base", "master"], cwd=tmp_path, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("T1\t"), out.stdout


def test_cli_bad_base_fails_loudly(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "master")
    (tmp_path / "a").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    out = subprocess.run([sys.executable, str(HOOKS / "artl_gate.py"), "tier",
                          "--base", "nope"], cwd=tmp_path, capture_output=True,
                         text=True)
    assert out.returncode == 2 and "nope" in out.stderr

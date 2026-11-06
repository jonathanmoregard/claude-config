#!/usr/bin/env python3
"""Regression tests for ab_harness.py + SKILL.md.

Each test pins ONE bug found by the first live run of the harness
(`~/Repos/skill-ab/experiments/recurrence-escalation`, commit 1e675a1). Every
one of them failed against the harness as it stood after that run.

    python3 test_ab_harness.py            # all
    python3 test_ab_harness.py bug1 bug4  # substring filter

No test spends money: the only subprocesses are `git`, and `run_one` (the only
function that shells out to `claude`) is monkeypatched wherever a live call
would otherwise happen.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The subject lives under runtime/, the tree projected into ~/.claude; this
# suite does not, so it reaches across rather than up.
_SKILL = (Path(__file__).resolve().parents[2]
          / "runtime" / "skills" / "ab-testing-prompt-changes")
sys.path.insert(0, str(_SKILL))
import ab_harness as H  # noqa: E402

SKILL_MD = _SKILL / "SKILL.md"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-C", str(repo), *args],
        capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.returncode}\n{p.stderr}")
    return p.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    """A tiny repo whose tree contains, deliberately:
      - `def locate(` ................. a 'the bug is really here' marker
      - `The Answer Is Structural` .... differs from the pattern only by CASE
      - `clipped window` .............. only matches an ERE, not a BRE
      - `clip on the way\\nout` ........ a phrase WRAPPED across two lines
    """
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    (repo / "src.py").write_text("def locate(x):\n    return x\n", encoding="utf-8")
    (repo / "notes.md").write_text(
        "The Answer Is Structural, not vigilant.\n"
        "It compares against the clipped window.\n", encoding="utf-8")
    (repo / "wrapped.md").write_text(
        "The fix is to compare on the full text and clip on the way\n"
        "out, at the response boundary.\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def write_cfg(root: Path, **over) -> Path:
    """Config in its own dir (paths in the config resolve relative to it)."""
    d = root / "exp"
    d.mkdir(exist_ok=True)
    for name in ("arm_a.md", "arm_b.md"):
        (d / name).write_text(f"# {name}\nbody\n", encoding="utf-8")
    (d / "task.md").write_text("do the thing\n", encoding="utf-8")
    (d / "PREREG.md").write_text("primary: binary\n", encoding="utf-8")
    cfg = {
        "repo": str(root / "repo"),
        "pinned_sha": "HEAD",
        "worktree_root": str(root / "wt"),
        "worktree_prefix": "t",
        "arms": {"a": "arm_a.md", "b": "arm_b.md"},
        "task_prompt": "task.md",
        "prereg": "PREREG.md",
        "n_per_arm": 1,
        "concurrency": 2,
    }
    cfg.update(over)
    p = d / "config.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def call(fn, cfg) -> tuple[int, str]:
    """Run a subcommand, capturing stdout and normalising SystemExit to a code."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = fn(cfg)
        return int(code or 0), buf.getvalue()
    except SystemExit as e:
        c = e.code
        return (1 if c is None else (c if isinstance(c, int) else 1)), buf.getvalue()


# --------------------------------------------------------------------------- #
# BUG 1 -- abbreviated SHAs, and pin validated only AFTER worktrees exist
# --------------------------------------------------------------------------- #

def test_bug1_abbreviated_sha_is_accepted(root: Path) -> None:
    repo, sha = make_repo(root)
    cfg = H.load_config(write_cfg(root, pinned_sha=sha[:9]))
    code, out = call(H.cmd_worktrees, cfg)
    assert code == 0, f"short SHA rejected (exit {code}):\n{out}"
    for arm in ("a", "b"):
        wt = H.worktree_path(cfg, arm, 1)
        assert wt.exists(), f"{wt} not created"
        assert not (wt / ".git").exists(), f"{wt} left UNSEALED"


def test_bug1_bad_pin_creates_no_worktree(root: Path) -> None:
    """The pin must be validated BEFORE anything is created: a failed pin used
    to leave worktree #1 on disk, unsealed, and still report success."""
    make_repo(root)
    cfg = H.load_config(write_cfg(root, pinned_sha="0" * 40))
    code, out = call(H.cmd_worktrees, cfg)
    assert code != 0, f"unresolvable pin reported success:\n{out}"
    made = sorted(p.name for p in Path(cfg["worktree_root"]).glob("t-*")) \
        if Path(cfg["worktree_root"]).exists() else []
    assert not made, f"worktrees created despite a bad pin: {made}"
    assert not (H.runs_dir(cfg) / "worktrees.json").exists(), \
        "wrote a seal record for a pin that never resolved"


# --------------------------------------------------------------------------- #
# BUG 2 -- probe silently falls back to the LIVE repo (with .git present)
# --------------------------------------------------------------------------- #

def test_bug2_probe_refuses_to_fall_back_to_live_repo(root: Path) -> None:
    make_repo(root)
    cfg = H.load_config(write_cfg(root))
    seen: list[str] = []
    orig = H.run_one
    H.run_one = lambda c, a, n, p, s, m, cwd: (seen.append(str(cwd)) or {})
    try:
        code, out = call(H.cmd_probe, cfg)
    finally:
        H.run_one = orig
    assert not seen, (
        f"probe ran in {seen[0]!r} instead of refusing -- if that is the live "
        "repo it still has .git, which defeats sealing")
    assert code != 0, f"probe with no worktree must error:\n{out}"


# --------------------------------------------------------------------------- #
# BUG 3 -- the two contamination greps disagreed, and both missed wrapped lines
# --------------------------------------------------------------------------- #

def test_bug3_present_and_absent_greps_agree(root: Path) -> None:
    """The same pattern against the same tree must not be 'not found' for
    must_be_present and 'found' for must_be_absent."""
    repo, sha = make_repo(root)
    for pat in ("answer is structural",          # differs only by case
                "clip(ped)? window"):            # ERE-only syntax
        cfg_p = H.load_config(write_cfg(
            root, pinned_sha=sha,
            contamination={"must_be_present": [pat], "must_be_absent": []}))
        cfg_a = H.load_config(write_cfg(
            root, pinned_sha=sha,
            contamination={"must_be_present": [], "must_be_absent": [pat]}))
        pcode, pout = call(H.cmd_contamination, cfg_p)
        acode, aout = call(H.cmd_contamination, cfg_a)
        assert pcode == 0, f"PRESENT {pat!r} not found, but it IS there:\n{pout}"
        assert acode != 0, f"ABSENT {pat!r} not flagged, but it IS there:\n{aout}"


def test_bug3_line_wrapped_phrase_is_caught(root: Path) -> None:
    """git grep is line-based. A phrase that wraps used to pass silently --
    the worst failure this tool has."""
    repo, sha = make_repo(root)
    cfg = H.load_config(write_cfg(
        root, pinned_sha=sha,
        contamination={"must_be_present": ["def locate("],
                       "must_be_absent": ["clip on the way out"]}))
    code, out = call(H.cmd_contamination, cfg)
    assert code != 0, (
        "contamination check said CLEAN, but 'clip on the way out' is in the "
        f"tree wrapped across a line break:\n{out}")


# --------------------------------------------------------------------------- #
# BUG 4 -- `run` was not resumable, so a killed batch silently re-spent
# --------------------------------------------------------------------------- #

def test_bug4_run_is_resumable(root: Path) -> None:
    repo, sha = make_repo(root)
    cfg = H.load_config(write_cfg(root, pinned_sha=sha, n_per_arm=2))
    assert call(H.cmd_worktrees, cfg)[0] == 0
    calls: list[str] = []

    def fake(c, arm, n, prompt, sp, mcp, cwd):
        calls.append(f"{arm}-{n}")
        return {"tag": f"{arm}-{n}", "arm": arm, "n": n, "status": "OK",
                "cost_usd": 0.25, "cwd": str(cwd)}

    orig, H.run_one = H.run_one, fake
    try:
        assert call(H.cmd_run, cfg)[0] == 0
        first = list(calls)
        calls.clear()
        code, out = call(H.cmd_run, cfg)
    finally:
        H.run_one = orig
    assert len(first) == 4, first
    assert code == 0, out
    assert not calls, f"re-spent {len(calls)} completed runs: {calls}"
    idx = json.loads((H.runs_dir(cfg) / "index.json").read_text(encoding="utf-8"))
    assert sorted(r["tag"] for r in idx) == ["a-1", "a-2", "b-1", "b-2"], idx


def test_bug4_skill_documents_duration_and_detached_launch(_root: Path) -> None:
    md = SKILL_MD.read_text(encoding="utf-8").lower()
    assert "systemd-run" in md, "SKILL.md must give the launch pattern that survives"
    assert "nohup" in md, "SKILL.md must say nohup is NOT enough"
    assert "min" in md and "resum" in md, \
        "SKILL.md must warn how long `run` takes and that it resumes"


# --------------------------------------------------------------------------- #
# BUG 5 -- "~2x the probe" is unsafe when the probe is atypically short
# --------------------------------------------------------------------------- #

def test_bug5_probe_guidance_is_not_2x(root: Path) -> None:
    repo, sha = make_repo(root)
    cfg = H.load_config(write_cfg(root, pinned_sha=sha))
    assert call(H.cmd_worktrees, cfg)[0] == 0
    orig = H.run_one
    H.run_one = lambda c, a, n, p, s, m, cwd: {
        "tag": "probe-0", "status": "OK", "cost_usd": 0.2559, "num_turns": 3}
    try:
        code, out = call(H.cmd_probe, cfg)
    finally:
        H.run_one = orig
    assert code == 0, out
    low = out.lower()
    assert "2x" not in low, f"probe still recommends ~2x:\n{out}"
    assert "floor" in low and "3" in low, f"no 3-4x-plus-floor rule:\n{out}"


def test_bug5_skill_budget_rule_is_not_2x(_root: Path) -> None:
    md = SKILL_MD.read_text(encoding="utf-8")
    assert "~2x" not in md and "2x the probe" not in md, \
        "SKILL.md still says ~2x the probe"
    assert "1.29" in md, "SKILL.md must cite the observed $1.29 vs $0.51 numbers"


# --------------------------------------------------------------------------- #
# guard: the trim must not drop the content baseline agents omitted 0/3
# --------------------------------------------------------------------------- #

def test_guard_skill_keeps_mandatory_gate_content(_root: Path) -> None:
    md = SKILL_MD.read_text(encoding="utf-8").lower()
    for must in ("merge-base --is-ancestor",   # contamination proof
                 "pre-regist",                 # frozen artifact
                 "inconclusive",               # policy
                 "budget",                     # measured cap
                 "designer bias",              # disclosure
                 "blind",                      # blind grading
                 "one variable",               # arm discipline
                 "null"):                      # a null is a result
        assert must in md, f"trim dropped mandatory content: {must!r}"


# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    if argv:
        tests = [(n, f) for n, f in tests if any(a in n for a in argv)]
    failed = 0
    for name, fn in tests:
        root = Path(tempfile.mkdtemp(prefix="abtest-"))
        try:
            fn(root)
            print(f"[ok  ] {name}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {name}\n       {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[ERR ] {name}\n       {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(root, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

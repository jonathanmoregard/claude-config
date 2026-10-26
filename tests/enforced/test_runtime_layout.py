#!/usr/bin/env python3
"""The runtime/dev partition is the review surface, so it is pinned here.

`scripts/runtime-layout.py` decides which tracked entries get projected into
`~/.claude` and which never do. The failure that matters is silent: a new
top-level entry defaulting into the projected set is how `tests/` would end up
back in the agent's view after all this work. So the script refuses to run on
an entry it does not recognise, and these cases exist to keep that refusal
working.

`tests/enforced/` is the directory the required `checks` job runs whole, which
is where a suite guarding one of this repository's own guards belongs.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The script moves `scripts/` into `runtime/` along with everything else, so it
# ends up one level deeper than it started. Both locations are legitimate --
# before the migration and after it -- and a suite that only knew one of them
# would stop running at exactly the moment the layout changed.
_CANDIDATES = (REPO_ROOT / "runtime" / "scripts" / "runtime-layout.py",
               REPO_ROOT / "scripts" / "runtime-layout.py")
SCRIPT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])


def load():
    spec = importlib.util.spec_from_file_location("runtime_layout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = load()


def run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--repo-root", str(root), *args],
                          capture_output=True, text=True)


def make_repo(root: Path, files: dict[str, str],
              links: dict[str, str] | None = None) -> Path:
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    for rel, dest in (links or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(dest)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


# ------------------------------------------------------------------ the manifest

def test_every_tracked_entry_is_classified():
    """The one that fires on real work: a new entry must be decided, not defaulted.

    This is expected to fail when someone adds a top-level file or directory,
    and that failure is the feature. The fix is one line in the manifest, in a
    diff a reviewer sees.
    """
    _, _, _, unclassified = layout.classify(REPO_ROOT)
    assert not unclassified, (
        "classify these in scripts/runtime-layout.py: " + ", ".join(unclassified))


def test_the_tiers_do_not_overlap():
    projected, dev_only, root_only = (set(layout.PROJECTED), set(layout.DEV_ONLY),
                                      set(layout.ROOT_ONLY))
    assert not projected & dev_only
    assert not projected & root_only
    assert not dev_only & root_only


def test_the_dev_trees_this_mission_is_about_are_dev_only():
    """A regression here would quietly undo the point of the whole change."""
    for tree in ("tests", "docs", "experiments", "research"):
        assert tree in layout.DEV_ONLY
        assert tree not in layout.PROJECTED


def test_nothing_is_misplaced_in_this_tree():
    assert layout.misplaced(REPO_ROOT) == []


# -------------------------------------------------------------- symlink geometry

@pytest.mark.parametrize("path,target,expected", [
    # The 33-entry skill farm. One extra `..` is right on both sides at once:
    # on disk it reaches `<repo>/vendor/y`, and under the mounts it reaches
    # `~/.claude/vendor/y`, which is where the four vendor binds are.
    ("skills/brainstorming", "../vendor/superpowers/skills/brainstorming",
     "../../vendor/superpowers/skills/brainstorming"),
    # Already absolute: resolves the same from any depth.
    ("hooks/router-nudge-hook", "/home/jonathan/Repos/router-agent/x", None),
    # Inside the entry being moved, so it moves with it.
    ("skills/a/b", "../c", None),
    ("hooks/x", "y", None),
    # Out of the repository altogether. No relative spelling is correct in both
    # geometries -- `../../Repos/...` would mean `~/Repos/...` on disk and
    # `~/router-agent/...` under the mounts -- so it has to become absolute.
    ("skills/review-inbox", "../../Repos/router-agent/src/router/skills/review-inbox",
     "/home/jonathan/Repos/router-agent/src/router/skills/review-inbox"),
])
def test_retarget(path, target, expected):
    assert layout.retarget(path, target) == expected


def test_the_live_farm_retargets_without_leaving_runtime():
    """Every tracked symlink resolves somewhere the mounted view can reach."""
    proc = run(REPO_ROOT, "--verify")
    assert proc.returncode == 0, proc.stderr


# ----------------------------------------------------------------- fail-closed

def test_an_unclassified_entry_stops_everything(tmp_path):
    root = make_repo(tmp_path, {
        "hooks/guard.py": "pass\n",
        "tests/test_x.py": "def test_x():\n    assert True\n",
        "surprise/new.py": "pass\n",
    })
    proc = run(root, "--plan")
    assert proc.returncode == 2
    assert "surprise" in proc.stderr
    # Refusing means refusing to move anything, not moving what it understood.
    assert not (root / "runtime").exists()


# --------------------------------------------------------------- apply + verify

def test_apply_moves_the_projected_and_leaves_the_rest(tmp_path):
    root = make_repo(
        tmp_path,
        {"hooks/guard.py": "pass\n",
         "settings.json": json.dumps({"hooks": {}}),
         "tests/test_x.py": "def test_x():\n    assert True\n",
         "docs/readme.md": "hi\n",
         "vendor/superpowers/skills/brainstorming/SKILL.md": "hi\n"},
        {"skills/brainstorming": "../vendor/superpowers/skills/brainstorming"},
    )
    assert run(root, "--apply").returncode == 0

    assert (root / "runtime" / "hooks" / "guard.py").exists()
    assert (root / "runtime" / "settings.json").exists()
    # Dev-only trees stay exactly where they were, outside the projection.
    assert (root / "tests" / "test_x.py").exists()
    assert (root / "docs" / "readme.md").exists()
    assert not (root / "runtime" / "tests").exists()
    # The submodule parent is not moved: the projection is per-subpath.
    assert (root / "vendor").is_dir()
    assert not (root / "runtime" / "vendor").exists()

    link = root / "runtime" / "skills" / "brainstorming"
    assert link.is_symlink()
    assert link.readlink().as_posix() == (
        "../../vendor/superpowers/skills/brainstorming")
    # And it still resolves on disk, which is the repo-side half of the claim.
    assert link.resolve().is_dir()


def test_apply_is_idempotent(tmp_path):
    root = make_repo(tmp_path, {"hooks/guard.py": "pass\n",
                                "tests/test_x.py": "x = 1\n"})
    assert run(root, "--apply").returncode == 0
    second = run(root, "--plan")
    assert second.returncode == 0
    assert "moves (0)" in second.stdout, second.stdout


def test_verify_catches_a_dev_tree_inside_runtime(tmp_path):
    """The check must fail on the thing it exists to prevent."""
    root = make_repo(tmp_path, {
        "runtime/hooks/guard.py": "pass\n",
        "runtime/tests/test_x.py": "x = 1\n",
    })
    proc = run(root, "--verify")
    assert proc.returncode == 1
    assert "tests" in proc.stderr


def test_verify_catches_a_symlink_that_leaves_the_projection(tmp_path):
    root = make_repo(
        tmp_path,
        {"runtime/hooks/guard.py": "pass\n"},
        {"runtime/skills/stray": "../../experiments/thing"},
    )
    proc = run(root, "--verify")
    assert proc.returncode == 1
    assert "experiments" in proc.stderr

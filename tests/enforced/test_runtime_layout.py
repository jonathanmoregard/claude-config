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
import re
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

def test_the_workflow_only_runs_paths_that_exist():
    """A moved file turns a CI step into a no-op or a nuisance failure.

    The layout change relocated `scripts/`, and the workflow kept naming the
    old path. That is the whole class: a check wired to a path nothing
    occupies. Parsing the `run:` lines for repo-relative arguments catches it
    on the branch that moves the file, rather than after the merge.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "checks.yml").read_text()
    referenced = set(re.findall(
        r"(?:python3|bash|python3 -m pytest)\s+(?:-m pytest\s+)?"
        r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh)|tests/[A-Za-z0-9_./-]+)",
        workflow))
    assert referenced, "parsed no paths out of the workflow -- the regex rotted"
    missing = sorted(p for p in referenced if not (REPO_ROOT / p).exists())
    assert not missing, f"the workflow runs paths that do not exist: {missing}"


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


@pytest.mark.parametrize("path,target", [
    # Reported in review. Both ends are projected, so both descend into
    # runtime/ together and the hop is already correct. The first version
    # added a `..` and pointed the link at <repo>/hooks/example, where nothing
    # will ever be. No link of this shape exists in the tree today, which is
    # exactly why it needed a test rather than a glance at the diff.
    ("skills/example", "../hooks/example"),
    ("agents/reviewer", "../templates/reviewer.md"),
    ("commands/x", "../scripts/x.sh"),
])
def test_a_hop_between_two_projected_entries_is_left_alone(path, target):
    assert layout.retarget(path, target) is None


def test_a_hop_to_something_that_stays_put_climbs_one_more():
    """The distinction the previous rule missed: vendor/ does not move."""
    assert layout.retarget("skills/x", "../vendor/superpowers/skills/x") == \
        "../../vendor/superpowers/skills/x"


def test_a_dangling_link_inside_the_projection_fails_verify(tmp_path):
    """Reported in review: the verifier said "no unresolvable symlink" for this.

    It was checking where the target pointed and never whether anything was
    there, so a link into a projected path that resolved to nothing passed.
    A dangling skill is a skill that silently stops loading.
    """
    root = make_repo(tmp_path, {"runtime/hooks/guard.py": "pass\n"},
                     {"runtime/skills/ghost": "../hooks/missing"})
    proc = run(root, "--verify")
    assert proc.returncode == 1
    assert "dangles" in proc.stderr, proc.stderr


def test_an_unverifiable_link_is_named_rather_than_passed(tmp_path):
    """Not every target can be checked here, so those are counted, not claimed.

    `git worktree add` does not initialise submodules, so the vendored trees
    are empty in most checkouts and CI never fetches them. That is a real
    limit, and the summary has to say so instead of reporting a clean run.
    """
    root = make_repo(tmp_path, {"runtime/hooks/guard.py": "pass\n"},
                     {"runtime/skills/x": "../../vendor/superpowers/skills/x"})
    proc = run(root, "--verify")
    assert proc.returncode == 0
    assert "not checkable here" in proc.stdout
    assert "unchecked:" in proc.stdout
    assert "no unresolvable symlink" not in proc.stdout


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

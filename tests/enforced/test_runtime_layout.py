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

def test_no_dev_tree_file_joins_the_repo_root_to_a_moved_entry():
    """A suite that reaches for a projected path from the repository root breaks.

    This started as a one-off grep, missed six files because they inlined the
    root expression instead of naming a variable, and one of those six then
    arrived fresh from master mid-review. A grep I have to remember to re-run
    is not a check, so it lives here.

    `tests/` and the other dev trees stay at the root, so a join to one of
    *those* is correct and not matched. A line that already resolves the
    runtime base is the fix, not a violation.
    """
    names = "|".join(re.escape(entry) for entry in
                     sorted(layout.PROJECTED, key=len, reverse=True))
    pattern = re.compile(
        rf'(?:parents\[\d\]|parent\.parent)\s*/\s*"({names})"'
        rf'|(?:ROOT|REPO|REPO_ROOT|root|repo_root)\s*/\s*"({names})"'
        rf'|\$\{{?(?:ROOT|REPO|REPO_ROOT)\}}?/({names})(?:/|\b)')
    tracked = subprocess.run(
        ["git", "ls-files", *layout.DEV_ONLY], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True).stdout.split()
    stale = []
    for rel in tracked:
        if rel.endswith(".md"):
            continue  # prose and recorded transcripts, not executed
        try:
            body = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            # The both-location idiom puts the root-relative spelling next to
            # the runtime one -- a tuple of candidates, or a `test -d` override
            # on the following line. Judging a line on its own text alone
            # flags every one of those fallback arms, so look at its
            # neighbours: a fallback is legitimate exactly when the runtime
            # variant is resolved beside it.
            window = "\n".join(lines[max(0, index - 2):index + 3]).lower()
            if "runtime" in window or "src_scripts" in window:
                continue
            stale.append(f"{rel}:{index + 1}: {line.strip()[:70]}")
    assert not stale, (
        "these build a path from the repository root to an entry that moved "
        "under runtime/:\n  " + "\n  ".join(stale))


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


# --------------------------------------------------- deliberately out of reach

def test_an_undeclared_absolute_target_fails_verify(tmp_path):
    """The tightening, tested on the case it exists to catch.

    Absolute used to be a blanket pass: the verifier could not check existence
    on another machine, so it filed *any* absolute target under "not checkable
    here" and still printed a clean summary. A typo'd or stale absolute link was
    therefore indistinguishable from a correct one. Containment is answerable
    from the index even when existence is not, so it is now answered.
    """
    root = make_repo(
        tmp_path,
        {"runtime/hooks/guard.py": "pass\n"},
        {"runtime/skills/stray": "/home/jonathan/Repos/somewhere-else/skill"},
    )
    proc = run(root, "--verify")
    assert proc.returncode == 1
    assert "ALLOWED_ABSOLUTE" in proc.stderr, proc.stderr


def test_an_absolute_target_cannot_traverse_out_of_the_allowlist(tmp_path):
    """The allowlist is a containment check, so spelling must not defeat it.

    The check matched the raw target string against each allowed prefix, so
    `<allowed>/../../Repos/claude-config/tests` passed it while resolving into
    a dev tree -- precisely the escape the allowlist exists to refuse, wearing
    an allowlisted prefix. Reported by review with a reproduction: `--verify`
    returned 0. Now the prefix match runs on the normalised path, and a `..`
    component is refused outright with the normalised form quoted, because a
    target that needs `..` to land inside the allowlist is not one anyone
    meant to write.
    """
    root = make_repo(
        tmp_path,
        {"runtime/hooks/guard.py": "pass\n"},
        {"runtime/skills/stray":
         "/home/jonathan/.claude/skills-hm/../../Repos/claude-config/tests"},
    )
    proc = run(root, "--verify")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert ".." in proc.stderr, proc.stderr
    # The message has to name where it actually lands, or the reader is left
    # re-deriving the traversal by hand.
    assert "/home/jonathan/Repos/claude-config/tests" in proc.stderr, proc.stderr


def test_a_skills_hm_target_is_declared_and_merely_unverifiable(tmp_path):
    """The second legitimate way out of the projection, next to vendor/.

    home-manager owns four skills so they go through nixos-config's gate, and
    `skills/` is read-only once the mounts are up -- so it writes them to
    ~/.claude/skills-hm/ and this repo links in. The link is absolute because
    that is the only spelling correct both on disk and under the mounts.

    Absent on this machine until that nixos-config change deploys, so the
    expected outcome is the named third outcome, not a pass and not a failure.
    """
    root = make_repo(
        tmp_path,
        {"runtime/hooks/guard.py": "pass\n"},
        {"runtime/skills/nixos-config-dev":
            f"{layout.HOME}/.claude/skills-hm/nixos-config-dev"},
    )
    proc = run(root, "--verify")
    assert proc.returncode == 0, proc.stderr
    assert "not checkable here" in proc.stdout
    assert "skills-hm/nixos-config-dev" in proc.stdout


HOME_MANAGER_SKILLS = (
    "nixos-config-dev",
    "nixos-automated-testing",
    "nixos-agent-testing",
    "nixos-agenix-secret",
)


@pytest.mark.parametrize("name", HOME_MANAGER_SKILLS)
def test_the_home_manager_skills_link_into_skills_hm(name):
    """Pinned so the four cannot be dropped or quietly retargeted.

    They are the live half of the arrangement above. A link retargeted back
    into `skills/` would fail the next `nixos-rebuild switch`, and at rebuild
    time rather than here -- so assert the target text, from the index.
    """
    path = f"{layout.RUNTIME_DIR}/skills/{name}"
    target = subprocess.run(["git", "cat-file", "-p", f":{path}"],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    assert target.returncode == 0, f"{path} is not tracked: {target.stderr}"
    assert target.stdout.strip() == f"{layout.HOME}/.claude/skills-hm/{name}"


@pytest.mark.parametrize("name", HOME_MANAGER_SKILLS)
def test_the_skills_hm_links_are_not_swallowed_by_gitignore(name):
    """These links must stay addable, and .gitignore is the way that breaks.

    The repo already ignores `skills/nixos-config-dev` and friends, for
    home-manager's /nix/store symlinks in the live ~/.claude checkout. That
    looks like it would swallow `runtime/skills/nixos-config-dev` too, and the
    assumption here was that it did -- it does not: a pattern with a slash
    anywhere but the end is relative to the .gitignore's own directory
    (gitignore(5)), so those five are root-anchored already.

    What *would* reach these links is a bare `nixos-config-dev` or a
    `**/nixos-config-dev`. Tracked content survives a later ignore rule, so such
    a rule would not unstage anything; it would make the next link of this kind
    impossible to add, with `git add` silently doing nothing. Asked of git
    rather than by grepping for a leading slash, so any rule shape that reaches
    them fails this regardless of how it is spelled.
    """
    # --no-index is load-bearing, not tidiness: without it `git check-ignore`
    # skips paths that are in the index and reports no match, so this test
    # passed for every tracked link no matter what rules existed. Proven by
    # simulating an unanchored rule through core.excludesFile: without the
    # flag the probe returns 1 (test passes), with it 0 (test fails). These
    # five links are tracked by definition, which is what made the blind spot
    # total. The sibling probes in tests/test_harness_runtime_dirs_ignored.py
    # already used the flag.
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--",
         f"{layout.RUNTIME_DIR}/skills/{name}"],
        cwd=REPO_ROOT)
    assert proc.returncode == 1, (
        f"{layout.RUNTIME_DIR}/skills/{name} is matched by a .gitignore rule; "
        "an unanchored skills/nixos-* rule is back")

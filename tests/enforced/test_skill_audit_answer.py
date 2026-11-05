"""Whether a run's answer shows it actually read the staged config.

`answered` only ever meant "findings.md is not empty", so a run that opened
seven irrelevant skills and wrote a wrong list graded identically to one that
got it right. Measured on CI run 42038996766: the 11-turn run opened 8
SKILL.md files of which exactly one is in the expected set, and PASSed.

Ground truth comes from the STAGED tree, because that is what the agent was
given. Extras never count against an answer: the agent's skill listing
includes plugin skills the staged tree does not carry, so naming one is
correct behaviour the staged tree cannot know about.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "agent-e2e/skill-description-audit/answer.py")


def load():
    spec = importlib.util.spec_from_loader(
        "answer", importlib.machinery.SourceFileLoader("answer", str(SRC)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def skill(home: pathlib.Path, name: str, description: str, body: str = "") -> None:
    path = home / ".claude/skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
                    encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    skill(tmp_path, "using-worktrees", "Use when work needs a worktree.")
    skill(tmp_path, "repo-worktree-policy", "Applies the worktree pattern.")
    skill(tmp_path, "brainstorming", "Use before any creative work.")
    # The needle in the BODY, not the description: the task asks about
    # descriptions, so this one is not part of the answer.
    skill(tmp_path, "writing-plans", "Use when you have a spec.",
          body="Work in a worktree when the plan is long.")
    return tmp_path


def test_ground_truth_reads_descriptions_not_bodies(home):
    assert load().expected_from(home) == ["repo-worktree-policy", "using-worktrees"]


def test_a_multiline_description_still_counts(tmp_path):
    path = tmp_path / ".claude/skills/wrapped/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: wrapped\n"
        "description: Use when starting feature work that needs isolation\n"
        "  from the current workspace, via git worktree.\n"
        "model: opus\n---\n\nbody\n", encoding="utf-8")
    assert load().expected_from(tmp_path) == ["wrapped"]


def test_a_following_frontmatter_key_is_not_swallowed(tmp_path):
    """The description regex must stop at the next key, or every skill whose
    frontmatter later mentions worktrees would join the answer."""
    path = tmp_path / ".claude/skills/innocent/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: innocent\ndescription: Use for colours.\n"
                    "argument-hint: a worktree path\n---\n\nbody\n",
                    encoding="utf-8")
    assert load().expected_from(tmp_path) == []


def test_a_correct_answer_has_full_recall(home):
    mod = load()
    expected = mod.expected_from(home)
    found, total = mod.recall_of(expected, "using-worktrees\nrepo-worktree-policy\n")
    assert (sorted(found), total) == (["repo-worktree-policy", "using-worktrees"], 2)


def test_a_wrong_answer_has_low_recall(home):
    mod = load()
    found, total = mod.recall_of(mod.expected_from(home), "brainstorming\n")
    assert (found, total) == ([], 2)


def test_extras_never_count_against_an_answer(home):
    """A plugin skill is in the agent's listing and not in the staged tree."""
    mod = load()
    answer = "using-worktrees\nrepo-worktree-policy\nsuperpowers:using-git-worktrees\n"
    found, total = mod.recall_of(mod.expected_from(home), answer)
    assert len(found) == total == 2


@pytest.mark.parametrize("answer", [
    "- using-worktrees\n- repo-worktree-policy\n",
    "Skills mentioning worktrees:\n\n1. using-worktrees\n2. repo-worktree-policy\n",
    "`using-worktrees`, `repo-worktree-policy`\n",
    "USING-WORKTREES\nREPO-WORKTREE-POLICY\n",
])
def test_formatting_around_the_names_is_tolerated(home, answer):
    """A real agent writes prose, bullets and backticks. Grading the layout
    instead of the content would fail honest runs, which is the flake this
    change exists to remove rather than add."""
    mod = load()
    found, total = mod.recall_of(mod.expected_from(home), answer)
    assert len(found) == total == 2


def test_a_name_inside_a_longer_name_is_not_a_match(home):
    mod = load()
    found, _ = mod.recall_of(["repo-worktree-policy"], "repo-worktree-policy-v2\n")
    assert found == []


def test_a_staged_tree_with_no_match_is_refused(tmp_path):
    """Nothing to recall means the task has no checkable answer, and a run
    against it would report full recall for writing anything at all."""
    skill(tmp_path, "brainstorming", "Use before any creative work.")
    mod = load()
    with pytest.raises(mod.Unusable):
        mod.expected_from(tmp_path, require=True)

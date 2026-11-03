"""honeypot.yml's scope step: which pull requests are worth an agent run.

The `honeypot` lane spends real money, so it asks what a pull request changes
and passes without grading when the answer is nothing a running agent reads.
That decision is a filter which can fail OPEN -- a pull request that slips
through it is graded by nobody and reports clean -- so the table below pins
every branch of it, including the ones no real pull request can produce.

The step is extracted from the workflow and executed, rather than reimplemented
here: a copy would pass while the lane it stands for was broken. `bash -e` and
no pipefail is what GitHub actually runs a `run:` block under.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/honeypot.yml"

# A stub in place of the API. The step asks two things: the changed-file list,
# and how many files the pull request claims to change -- the second so a
# truncated list cannot read as "no config changed".
GH_STUB = """#!/usr/bin/env python3
import os, sys
if any(a.endswith("/files") for a in sys.argv[1:]):
    print(os.environ["STUB_FILES"], end="")
    if os.environ["STUB_FILES"]:
        print()
    raise SystemExit(0)
count = os.environ.get("STUB_COUNT")
if count is None:
    count = str(len([x for x in os.environ["STUB_FILES"].splitlines() if x]))
print(count)
"""


def scope_step() -> str:
    """The `run:` block of the step whose id is `scope`, dedented.

    Hand-rolled because nothing else in this corpus needs a YAML parser and
    the runner's python3 is not guaranteed to carry one. The shape being read
    is two lines of the workflow, but a reformat that silently matched nothing
    would leave every case below passing against an empty script, so the
    caller asserts the extraction recovered the real thing.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "id: scope")
    run = next(i for i in range(start, len(lines))
               if re.fullmatch(r"\s*run: \|\s*", lines[i]))
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


def test_the_step_was_really_extracted():
    """Guards every case below: an empty script would pass all of them."""
    script = scope_step()
    for marker in ("grade=true", "grade=false", "GITHUB_OUTPUT",
                   "pulls/${PR}/files", "changed_files"):
        assert marker in script, f"{marker!r} missing -- extraction broke"


def run_scope(tmp_path, files: str, *, asked: str = "false",
              count: str | None = None) -> tuple[int, str, str]:
    """Execute the real step against a stubbed API. Returns (rc, log, output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    script = tmp_path / "scope.sh"
    script.write_text(scope_step(), encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    # The stub shadows `gh`; everything else -- bash, grep, sed -- resolves off
    # the real PATH. Spelling out `/usr/bin:/bin` instead finds no bash at all
    # on a host that keeps it in a store path, which is the same assumption
    # that put absolute executable pins in the rest of this corpus.
    env = {"PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
           "ASKED": asked, "PR": "1", "GITHUB_REPOSITORY": "acme/widget",
           "GITHUB_OUTPUT": str(out), "STUB_FILES": files}
    if count is not None:
        env["STUB_COUNT"] = count
    bash = shutil.which("bash")
    assert bash, "no bash on PATH; this is what GitHub runs a `run:` block under"
    p = subprocess.run([bash, "-e", str(script)], env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr, out.read_text(encoding="utf-8")


# Changes that cannot reach a running agent. The lane passes and spends
# nothing; the check still reports, so requiring it stays honest.
NOT_CONFIG = [
    pytest.param(".github/workflows/checks.yml\n.gitleaks.toml\n"
                 "tests/conftest.py\ntests/test_agent_e2e.py",
                 id="the-ci-corpus-and-its-workflow"),
    pytest.param("docs/a.md\nimprovements/rsi/b.md\n.githooks/pre-commit\n"
                 "research/c.md\nexperiments/d.py\n.gitignore\n.secrets.baseline",
                 id="docs-proposals-git-hooks-scanner-config"),
]

# Changes that DO reach a running agent, and so must not pass ungraded.
IS_CONFIG = [
    pytest.param("runtime/CLAUDE.md\ntests/t.py", id="runtime-config"),
    # The hole a `runtime/**`-only filter would have left: sixteen skills under
    # runtime/skills are symlinks into these submodules, `using-superpowers`
    # among them, so a pin bump changes text every session reads while
    # touching no file beneath runtime/.
    pytest.param("vendor/superpowers", id="a-vendored-skill-pin"),
    pytest.param(".gitmodules", id="which-submodules-exist"),
    pytest.param("policy/sandbox-candidate.json", id="a-sandbox-policy"),
    # The property that makes this an exclusion list rather than an allowlist.
    pytest.param("somethingnew/x.json", id="a-directory-nobody-foresaw"),
]


@pytest.mark.parametrize("files", NOT_CONFIG)
def test_a_pull_request_that_changes_no_config_is_not_graded(tmp_path, files):
    rc, log, out = run_scope(tmp_path, files)
    assert rc == 0, f"should pass without grading:\n{log}"
    assert out.strip() == "grade=false", f"got {out!r}\n{log}"
    assert "nothing to grade" in log


@pytest.mark.parametrize("files", IS_CONFIG)
def test_a_pull_request_that_changes_config_is_refused_until_graded(tmp_path, files):
    rc, log, out = run_scope(tmp_path, files)
    assert rc == 1, f"an ungraded config must not pass:\n{log}"
    assert out.strip() == "", f"no verdict should be emitted, got {out!r}"
    assert "has not been graded" in log


def test_the_label_grades_whatever_it_is_put_on(tmp_path):
    """The override, and why it exists.

    This lane's own scenario lives under `tests/agent-e2e/`, which the scope
    test excludes. Without the label a change to the instrument could never be
    measured by the instrument.
    """
    rc, log, out = run_scope(tmp_path, "tests/agent-e2e/x/check.sh", asked="true")
    assert rc == 0, log
    assert out.strip() == "grade=true", f"got {out!r}\n{log}"


@pytest.mark.parametrize("files,count,because", [
    pytest.param("tests/a.py\ntests/b.py", "7",
                 "a short list can as well be a runtime/ file that never"
                 " arrived; this endpoint stops at 3000",
                 id="a-truncated-list"),
    pytest.param("", None, "nothing to draw a conclusion from",
                 id="no-files-reported"),
    pytest.param("tests/a.py", "lots", "the list cannot be checked for"
                 " completeness", id="a-non-numeric-count"),
])
def test_what_cannot_be_measured_is_refused(tmp_path, files, count, because):
    """Every way of not knowing the scope fails closed, never open."""
    rc, log, out = run_scope(tmp_path, files, count=count)
    assert rc == 1, f"should refuse ({because}):\n{log}"
    assert out.strip() == "", f"no verdict should be emitted, got {out!r}"
    assert "failing closed" in log

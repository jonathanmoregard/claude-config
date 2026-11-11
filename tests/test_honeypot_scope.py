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

# A stub in place of the API. The step asks two things: the changed-file
# records, and how many files the pull request claims to change -- the second
# so a truncated page cannot read as "no config changed".
#
# A record is what `--jq '.[] | [.filename, (.previous_filename // empty)] |
# @tsv'` emits: the path, and for a rename a tab and the path it came from.
GH_STUB = """#!/usr/bin/env python3
import os, sys
if any(a.endswith("/files") for a in sys.argv[1:]):
    print(os.environ["STUB_RECORDS"], end="")
    if os.environ["STUB_RECORDS"]:
        print()
    raise SystemExit(0)
count = os.environ.get("STUB_COUNT")
if count is None:
    count = str(len([x for x in os.environ["STUB_RECORDS"].splitlines() if x]))
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


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def asked_expression(text: str) -> str:
    """The one expression that decides "a grade was asked for", whitespace-folded."""
    m = re.search(r"ASKED: \$\{\{(.*?)\}\}", text, re.S)
    assert m, "the scope step's ASKED expression is gone"
    return " ".join(m.group(1).split())


def test_the_lane_runs_only_on_auto_merge_and_pushes():
    """No run at all until auto-merge: the required check sits Expected (yellow).

    `opened` and `labeled` are gone on purpose. A run at open time had to
    either refuse (red, which agents kept reading as something to fix by
    asking about the billed label) or pass (a free pass). `synchronize` stays:
    the strict up-to-date rule makes every master move a branch update, and a
    push while auto-merge is on must re-grade.
    """
    types = next(l for l in workflow_text().splitlines() if l.strip().startswith("types:"))
    assert types.split(":", 1)[1].strip() == "[auto_merge_enabled, synchronize]", types


def test_an_unrequested_run_reports_under_a_name_nothing_requires():
    """The required context is the job NAME. Unrequested runs must not carry it.

    Not a job-level `if:` -- a skipped job reports SUCCESS to a required check.
    The name and ASKED come from the identical expression, so a run can never
    be named `honeypot` while the step believes it was not asked.
    """
    text = workflow_text()
    job = text[text.index("\njobs:"):]
    head = job.split("steps:")[0]
    assert re.search(r"^\s{4}if:", head, re.M) is None, "job-level if:"
    m = re.search(r"^\s{4}name: \$\{\{(.*?)&& 'honeypot' \|\| 'honeypot-not-requested' \}\}",
                  head, re.M | re.S)
    assert m, "job name no longer derived from the ask"
    name_expr = " ".join(m.group(1).split())
    assert name_expr.strip("() ") == asked_expression(text).strip("() "), name_expr
    assert "'grade'" not in asked_expression(text), "the label is no longer a trigger"


def test_unrequested_runs_cannot_cancel_a_grade():
    """Separate concurrency groups: a push before auto-merge must not cancel a grade."""
    group = next(l for l in workflow_text().splitlines() if l.strip().startswith("group:"))
    assert "auto_merge" in group, group


def test_the_step_was_really_extracted():
    """Guards every case below: an empty script would pass all of them."""
    script = scope_step()
    for marker in ("grade=true", "grade=false", "GITHUB_OUTPUT",
                   "pulls/${PR}/files", "changed_files", "previous_filename"):
        assert marker in script, f"{marker!r} missing -- extraction broke"


def run_scope(tmp_path, records: str, *, asked: str = "true",
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
           "GITHUB_OUTPUT": str(out), "STUB_RECORDS": records}
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
    # A rename whose BOTH ends are outside the config is still nothing to
    # grade; reading two names must not make the filter paranoid.
    pytest.param("docs/b.md\tdocs/a.md", id="a-rename-within-the-docs"),
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
    # Reading only `filename` graded this as nothing: the hook is GONE from
    # the running config, and all the destination says is "docs". A plain
    # deletion was never affected -- `filename` stays the runtime path -- so
    # a rename out of the config was the one way to launder one past here.
    pytest.param("docs/bash-guard.py\truntime/hooks/bash-guard.py",
                 id="a-hook-renamed-out-of-the-config"),
    pytest.param("runtime/hooks/x.py\ttests/x.py",
                 id="a-test-renamed-into-the-config"),
    # The lane's own instrument. With the label gone nothing else can ask for
    # a grade of a change to it, so the scope test counts it as config.
    pytest.param("tests/agent-e2e/skill-description-audit/check.sh",
                 id="the-honeypot-scenario-itself"),
    pytest.param("tests/agent-e2e/_lib/checks.sh", id="the-library-the-scenario-sources"),
]


@pytest.mark.parametrize("records", NOT_CONFIG)
def test_a_pull_request_that_changes_no_config_is_not_graded(tmp_path, records):
    rc, log, out = run_scope(tmp_path, records)
    assert rc == 0, f"should pass without grading:\n{log}"
    assert out.strip() == "grade=false", f"got {out!r}\n{log}"
    assert "nothing to grade" in log


@pytest.mark.parametrize("records", IS_CONFIG)
def test_a_pull_request_that_changes_config_is_graded_when_asked(tmp_path, records):
    rc, log, out = run_scope(tmp_path, records)
    assert rc == 0, log
    assert out.strip() == "grade=true", f"got {out!r}\n{log}"


@pytest.mark.parametrize("records", NOT_CONFIG + IS_CONFIG)
def test_an_unrequested_run_grades_nothing_and_passes(tmp_path, records):
    """It reports as `honeypot-not-requested`, which no rule requires."""
    rc, log, out = run_scope(tmp_path, records, asked="false")
    assert rc == 0, log
    assert out.strip() == "grade=false", f"got {out!r}\n{log}"
    assert "not requested" in log


@pytest.mark.parametrize("records,count,because", [
    pytest.param("tests/a.py\ntests/b.py", "7",
                 "a short list can as well be a runtime/ file that never"
                 " arrived; this endpoint stops at 3000",
                 id="a-truncated-list"),
    # Completeness counts RECORDS, not paths. Three renames are three of the
    # five files this pull request claims, so the page is short and the answer
    # is refusal -- but they carry six paths between them, and a check that
    # counted those would see 6 >= 5 and pass a truncated page for free.
    pytest.param("docs/b.md\tdocs/a.md\ndocs/d.md\tdocs/c.md\n"
                 "docs/f.md\tdocs/e.md", "5",
                 "renames must not hide a short page behind a second column",
                 id="renames-do-not-mask-truncation"),
    pytest.param("", None, "nothing to draw a conclusion from",
                 id="no-files-reported"),
    pytest.param("tests/a.py", "lots", "the list cannot be checked for"
                 " completeness", id="a-non-numeric-count"),
])
def test_what_cannot_be_measured_is_refused(tmp_path, records, count, because):
    """Every way of not knowing the scope fails closed, never open."""
    rc, log, out = run_scope(tmp_path, records, count=count)
    assert rc == 1, f"should refuse ({because}):\n{log}"
    assert out.strip() == "", f"no verdict should be emitted, got {out!r}"
    assert "failing closed" in log

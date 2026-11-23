"""Offline tests for scripts/agent-e2e: staging, grading plumbing, scenario setup.

The live half (a real `claude -p`) is what the harness is FOR and is run per
change, not here; these pin the parts a broken harness would silently fake:
the staged HOME really carries the candidate config, a stubbed agent's output
reaches check.sh, and a broken run is INCONCLUSIVE rather than a PASS.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# `runtime/` once the layout migration has landed, the repository root
# before it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = ROOT / "runtime" if (ROOT / "runtime").is_dir() else ROOT
HARNESS = RUNTIME / "scripts/agent-e2e/agent_e2e.py"
SCENARIOS = ROOT / "tests/agent-e2e"


def harness(*args, **kw):
    return subprocess.run([sys.executable, str(HARNESS), *args],
                          capture_output=True, text=True, **kw)


def fake_claude(tmp: pathlib.Path, body: str, name: str = "fake-claude") -> pathlib.Path:
    """A `claude` that runs BODY as bash in the agent's cwd/env."""
    path = tmp / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def stream(final: str, commands: list[str], is_error=False) -> str:
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}
        for c in commands]
    events.append({"type": "result", "result": final, "is_error": is_error,
                   "total_cost_usd": 0.0})
    return "\n".join(json.dumps(e) for e in events)


def test_stage_from_worktree_carries_uncommitted_edits(tmp_path):
    src = tmp_path / "cfg"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "CLAUDE.md").write_text("committed\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "x"], check=True)
    (src / "CLAUDE.md").write_text("candidate edit\n")
    (src / "new-skill.md").write_text("untracked but not ignored\n")
    home = tmp_path / "home"
    r = harness("stage", "--config", str(src), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/CLAUDE.md").read_text() == "candidate edit\n"
    assert (home / ".claude/new-skill.md").exists()
    assert "+uncommitted" in r.stdout
    assert not (home / ".claude/.git").exists()


def test_stage_from_git_ref(tmp_path):
    home = tmp_path / "home"
    r = harness("stage", "--config", "HEAD", "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/CLAUDE.md").is_file()
    assert (home / ".claude/scripts/claude-worktree.sh").is_file()


def _git(*args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "protocol.file.allow=always", *map(str, args)],
                   check=True, capture_output=True)


def _config_with_vendored_skill(tmp_path) -> pathlib.Path:
    """A config repo whose skills/x links into a vendor/ submodule, carrying
    a copy of this harness so its git-ref mode archives THIS repo."""
    sub = tmp_path / "upstream"
    (sub / "skills/x").mkdir(parents=True)
    (sub / "skills/x/SKILL.md").write_text("vendored\n")
    _git("init", "-q", sub)
    _git("-C", sub, "add", "-A")
    _git("-C", sub, "commit", "-qm", "x")
    cfg = tmp_path / "cfg"
    (cfg / "scripts/agent-e2e").mkdir(parents=True)
    (cfg / "skills").mkdir()
    (cfg / "scripts/agent-e2e/agent_e2e.py").write_text(HARNESS.read_text())
    (cfg / "skills/x").symlink_to("../vendor/sub/skills/x")
    _git("init", "-q", cfg)
    _git("-C", cfg, "submodule", "add", "-q", sub, "vendor/sub")
    _git("-C", cfg, "add", "-A")
    _git("-C", cfg, "commit", "-qm", "x")
    return cfg


def test_stage_from_worktree_carries_vendored_submodule(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    home = tmp_path / "home"
    r = harness("stage", "--config", str(cfg), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/skills/x/SKILL.md").read_text() == "vendored\n"


def test_stage_from_git_ref_carries_vendored_submodule(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    home = tmp_path / "home"
    r = subprocess.run([sys.executable, str(cfg / "scripts/agent-e2e/agent_e2e.py"),
                        "stage", "--config", "HEAD", "--home", str(home)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/skills/x/SKILL.md").read_text() == "vendored\n"


def test_stage_names_a_submodule_it_could_not_stage(tmp_path):
    cfg = _config_with_vendored_skill(tmp_path)
    _git("-C", cfg, "submodule", "deinit", "-q", "--force", "vendor/sub")
    home = tmp_path / "home"
    r = harness("stage", "--config", str(cfg), "--home", str(home))
    assert r.returncode == 0, r.stderr
    assert "vendor/sub" in r.stdout and "not initialised" in r.stdout


def _scenario(tmp_path, check_body: str) -> pathlib.Path:
    sc = tmp_path / "sc"
    sc.mkdir()
    (sc / "prompt.md").write_text("do it\n")
    (sc / "setup.sh").write_text("#!/usr/bin/env bash\necho ready > \"$2/marker\"\n")
    (sc / "check.sh").write_text("#!/usr/bin/env bash\n" + check_body)
    return sc


# Whether a sandbox can ACTUALLY be created here, probed once.
#
# `shutil.which("bwrap")` is not the same question, and the difference is not
# academic: every guard that asked only whether the binary exists let these
# cases run on a GitHub runner and fail INCONCLUSIVE -- measured, nine of them,
# the first time this workflow ran the whole corpus. A case that cannot sandbox
# must say so, not report the harness broken.
#
# The probe builds the harness's own mount shape rather than a trivial sandbox,
# because a machine that can unshare can still refuse a mount. Asking
# `--dev-bind / / --unshare-all` was measured succeeding on a runner that then
# failed all eleven cases a second time: the binary is there, namespaces work,
# and what the runner lacked was a `~/.ssh` for the harness to shadow. That was
# the harness's defect, fixed where it lives; the probe now at least covers the
# read-only bind of the real HOME that the shadowing stacks on.
# Cached, because nine call sites would otherwise fork nine times.
_SANDBOX_OK = None


def _require_sandbox():
    """Skip unless bubblewrap can really build a sandbox on this machine."""
    global _SANDBOX_OK
    if _SANDBOX_OK is None:
        import pwd as _pwd
        import shutil as _sh
        bwrap = _sh.which("bwrap")
        home = _pwd.getpwuid(os.getuid()).pw_dir
        if bwrap is None:
            _SANDBOX_OK = (False, "bubblewrap not on PATH")
        else:
            try:
                p = subprocess.run([bwrap, "--dev-bind", "/", "/",
                                    "--ro-bind", home, home, "--tmpfs", "/tmp",
                                    "true"], capture_output=True, text=True, timeout=30)
                _SANDBOX_OK = (
                    p.returncode == 0,
                    "bubblewrap is present but cannot build the harness's "
                    "sandbox here (restricted user namespaces, or a mount it "
                    "refuses): "
                    + (p.stderr.strip().splitlines() or ["no stderr"])[-1],
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _SANDBOX_OK = (False, f"bubblewrap probe failed: {exc!r}")
    ok, why = _SANDBOX_OK
    if not ok:
        pytest.skip(why)


# Whether a sandbox can be built INSIDE a sandbox, which is a third question
# again. ubuntu-latest grants the first user namespace and refuses the second:
# the outer run started, the nested harness staged its config and printed its
# report, and the inner agent exited 1 carrying
#   bwrap: No permissions to create a new namespace, likely because the kernel
#   does not allow non-privileged user namespaces.
# -- measured, out of the inner run's own stderr. A case whose subject IS
# nesting therefore cannot run there, and must say that rather than fail.
_NESTED_SANDBOX_OK = None


def _require_nested_sandbox():
    """Skip unless bubblewrap can build a sandbox from inside one."""
    _require_sandbox()
    global _NESTED_SANDBOX_OK
    if _NESTED_SANDBOX_OK is None:
        import shutil as _sh
        bwrap = _sh.which("bwrap")
        shape = ["--dev-bind", "/", "/", "--tmpfs", "/tmp"]
        try:
            p = subprocess.run([bwrap, *shape, bwrap, *shape, "true"],
                               capture_output=True, text=True, timeout=30)
            _NESTED_SANDBOX_OK = (
                p.returncode == 0,
                "bubblewrap can sandbox here but cannot nest one sandbox "
                "inside another: "
                + (p.stderr.strip().splitlines() or ["no stderr"])[-1],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _NESTED_SANDBOX_OK = (False, f"nested bubblewrap probe failed: {exc!r}")
    ok, why = _NESTED_SANDBOX_OK
    if not ok:
        pytest.skip(why)


def _run(tmp_path, sc, claude, n=1, **kw):
    out = tmp_path / "out"
    r = harness("run", str(sc), "--arm", "new=HEAD", "--n", str(n), "--jobs", "1",
                "--out", str(out), "--claude", str(claude), **kw)
    assert r.returncode == 0, r.stderr
    verdicts = [json.loads(p.read_text()) for p in sorted(out.glob("*/*/*/verdict.json"))]
    return r, out, verdicts


def test_agent_sees_staged_home_and_check_gets_its_commands(tmp_path):
    _require_sandbox()
    claude = fake_claude(tmp_path, f"""
test -f "$HOME/.claude/CLAUDE.md" || exit 3
cat <<'EOF'
{stream("Shall I open the PR?", ["git status", "gh pr view"])}
EOF
""")
    sc = _scenario(tmp_path, 'grep -qx "gh pr view" "$1/commands.txt" && '
                             'grep -q "open the PR" "$1/final.txt" && test -f "$1/marker"')
    r, _, verdicts = _run(tmp_path, sc, claude)
    assert [v["verdict"] for v in verdicts] == ["PASS"], r.stdout


def test_check_failure_is_fail(tmp_path):
    _require_sandbox()
    claude = fake_claude(tmp_path, f"cat <<'EOF'\n{stream('done', [])}\nEOF\n")
    sc = _scenario(tmp_path, 'echo "FAIL: nope"; exit 1')
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "FAIL"
    assert verdicts[0]["why"] == "FAIL: nope"


@pytest.mark.parametrize("body", [
    "exit 1\n",                                                     # agent crashed
    f"cat <<'EOF'\n{stream('', ['ls'])}\nEOF\n",                    # no final message
    f"cat <<'EOF'\n{stream('partial', [], is_error=True)}\nEOF\n",  # budget cap etc.
])
def test_broken_runs_are_inconclusive_never_pass(tmp_path, body):
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")  # a check that would PASS anything
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"


_SECRETS = (".claude.json", ".claude.json.backup", ".claude/backups/.claude.json.backup.1",
            ".ssh/id_x", ".codex/auth.json", ".claude/.credentials.json")
# Plants every secret in $HOME. The credentials entry is a link to the REAL
# file, so it is removed first: writing through it would overwrite the real one.
# A token refresh leaves it a regular file, which is what this plants.
_PLANT = "\n".join(
    ['rm -f "$HOME/.claude/.credentials.json"']
    + [f'mkdir -p "$(dirname "$HOME/{rel}")"; echo s > "$HOME/{rel}"' for rel in _SECRETS]
    + ['ln -s / "$HOME/.local-root"', 'ln -s /etc/hostname "$HOME/.claude/out-link"'])


def _ok(body: str = "") -> str:
    return body + f"\ncat <<'EOF'\n{stream('ok', [])}\nEOF\n"


def _assert_scrubbed(run: pathlib.Path):
    """No link and no account material anywhere under RUN."""
    bad = []
    for p in run.rglob("*"):
        rel = str(p.relative_to(run))
        if p.is_symlink() or p.name.startswith((".claude.json", ".credentials.json")):
            bad.append(rel)
        elif p.is_dir() and (p.name in (".ssh", ".codex")
                             or (p.name == "backups" and p.parent.name == ".claude")):
            bad.append(rel)
    assert not bad, f"retained after the run: {bad}"


def _decoy(path: pathlib.Path) -> dict[pathlib.Path, str]:
    """Files outside every run dir that a scrub or write following links would hit."""
    files = [path / ".ssh/id", path / ".claude/backups/b", path / ".claude.json",
             path / "backups/b", path / ".credentials.json", path / "plugins/p", path / "f"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("keep\n")
    (path / "lnk").symlink_to("/etc/hostname")
    return {f: "keep\n" for f in files}


def _assert_decoy_intact(decoy: dict[pathlib.Path, str]):
    changed = [str(p) for p, text in decoy.items()
               if not p.is_file() or p.read_text() != text]
    assert not changed, f"touched outside the run dir: {changed}"
    assert next(iter(decoy)).parents[1].joinpath("lnk").is_symlink()


def test_retained_run_is_scrubbed_after_check_but_state_survives(tmp_path):
    # A retained run dir is read by reviewers under a broad Read allow, and its
    # staged HOME carries links into the real HOME plus account files the CLI
    # writes there. check.sh must still see them; nothing after it may.
    _require_sandbox()
    claude = fake_claude(tmp_path, _ok(
        _PLANT + '\nmkdir -p "$HOME/.local/state/n"; echo row > "$HOME/.local/state/n/ledger.tsv"'))
    checks = " && ".join(f'test -f "$1/home/{rel}"' for rel in _SECRETS)
    r, out, verdicts = _run(tmp_path, _scenario(tmp_path, checks), claude)
    assert [v["verdict"] for v in verdicts] == ["PASS"], r.stdout
    run = out / "sc/new/1"
    _assert_scrubbed(run)
    assert (run / "home/.local/state/n/ledger.tsv").read_text() == "row\n"
    assert (run / "home/.claude/CLAUDE.md").is_file()
    assert (run / "transcript.jsonl").is_file()


def test_run_is_scrubbed_when_setup_fails(tmp_path):
    claude = fake_claude(tmp_path, "exit 0\n")
    sc = _scenario(tmp_path, "exit 0")
    (sc / "setup.sh").write_text(f"#!/usr/bin/env bash\n{_PLANT}\nexit 1\n")
    _, out, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"
    _assert_scrubbed(out / "sc/new/1")


def test_secrets_outside_home_are_scrubbed_too(tmp_path):
    _require_sandbox()
    # A nested harness's runs, the work tree, a HOME the run moved: all are
    # inside the run dir and none is its home/.
    claude = fake_claude(tmp_path, _ok(
        'mkdir -p "$E2E_RUN/nested-e2e/sc/old/1/home/.claude/backups" "$E2E_WORK/x/.ssh"\n'
        'echo s > "$E2E_RUN/nested-e2e/sc/old/1/home/.claude.json"\n'
        'echo s > "$E2E_RUN/nested-e2e/sc/old/1/home/.claude/backups/b"\n'
        'echo s > "$E2E_WORK/x/.ssh/id"\n'
        'ln -s /etc/hostname "$E2E_WORK/x/l"'))
    _, out, verdicts = _run(tmp_path, _scenario(tmp_path, "exit 0"), claude)
    assert verdicts[0]["verdict"] == "PASS"
    _assert_scrubbed(out / "sc/new/1")


@pytest.mark.parametrize("swap", [
    'cd /; mv "$HOME" "$E2E_RUN/h2"; ln -s {decoy} "$E2E_RUN/home"',
    'mv "$HOME/.claude" "$HOME/.claude-old"; ln -s {decoy} "$HOME/.claude"',
    'mkdir -p "$HOME/x"; ln -s {decoy} "$HOME/x/y"; ln -s {decoy}/.ssh "$HOME/.ssh"',
    'ln -s {decoy}/f "$E2E_RUN/final.txt"; ln -s {decoy}/.claude.json "$E2E_RUN/verdict.json"',
])
def test_scrub_and_writes_never_follow_a_link_the_run_planted(tmp_path, swap):
    _require_sandbox()
    # The harness acts outside the sandbox on a tree the run could rewrite: a
    # planted link must not aim its deletes or its result writes.
    decoy = _decoy(tmp_path / "decoy")
    claude = fake_claude(tmp_path, _ok(_PLANT + "\n" + swap.format(decoy=tmp_path / "decoy")))
    _, out, verdicts = _run(tmp_path, _scenario(tmp_path, "exit 0"), claude)
    assert verdicts[0]["verdict"] == "PASS"
    _assert_decoy_intact(decoy)
    _assert_scrubbed(out / "sc/new/1")
    assert (out / "sc/new/1/final.txt").read_text() == "ok"


def test_a_failed_scrub_item_keeps_the_verdict_and_the_rest_of_the_scrub(tmp_path):
    _require_sandbox()
    claude = fake_claude(tmp_path, _ok(
        _PLANT + '\nmkdir -p "$HOME/.ssh/locked"; touch "$HOME/.ssh/locked/f"; '
        'chmod 000 "$HOME/.ssh/locked"\n'
        'for i in $(seq 20); do mkdir -p "$HOME/.ssh/k_$i"; touch "$HOME/.ssh/k_$i/key"; done\n'
        'mkdir -p "$HOME/d"; touch "$HOME/d/f"; chmod 400 "$HOME/d"'))
    _, out, verdicts = _run(tmp_path, _scenario(tmp_path, "exit 0"), claude)
    run = out / "sc/new/1"
    try:
        assert verdicts[0]["verdict"] == "PASS"
        errors = verdicts[0]["scrub_errors"]
        assert any(".ssh" in e for e in errors) and any("/d" in e for e in errors), errors
        assert json.loads((run / "verdict.json").read_text())["scrub_errors"] == errors
        assert (run / "scrub-errors.txt").read_text().splitlines() == errors
        for rel in (".claude.json", ".claude/.credentials.json", ".codex", ".ssh/id_x"):
            assert not (run / "home" / rel).exists(), rel
        assert not list((run / "home/.ssh").glob("k_*")), "siblings of a locked entry kept"
        assert not [p for p in (run / "home/.claude").rglob("*") if p.is_symlink()]
    finally:
        for rel in (".ssh/locked", "d"):
            if (run / "home" / rel).exists():
                (run / "home" / rel).chmod(0o700)


def test_a_failed_scrub_record_does_not_cost_the_verdict(tmp_path):
    _require_sandbox()
    claude = fake_claude(tmp_path, _ok(
        _PLANT + '\nmkdir "$E2E_RUN/scrub-errors.txt"\n'
        'mkdir -p "$HOME/.ssh/locked"; chmod 000 "$HOME/.ssh/locked"'))
    _, out, verdicts = _run(tmp_path, _scenario(tmp_path, "exit 0"), claude)
    run = out / "sc/new/1"
    try:
        assert verdicts[0]["verdict"] == "PASS", verdicts[0]
        assert json.loads((run / "verdict.json").read_text())["scrub_errors"]
    finally:
        if (run / "home/.ssh/locked").exists():
            (run / "home/.ssh/locked").chmod(0o700)


def test_a_non_utf8_name_in_scrub_errors_does_not_cost_the_verdict(tmp_path):
    _require_sandbox()
    # A name in bytes that are not UTF-8 reaches the error text as surrogates;
    # writing that out must not raise in run_one's finally.
    claude = fake_claude(tmp_path, _ok(
        _PLANT + '\nbad="$HOME/.ssh/$(printf \'\\377\')"; mkdir -p "$bad/x"; chmod 000 "$bad"'))
    _, out, verdicts = _run(tmp_path, _scenario(tmp_path, "exit 0"), claude)
    run = out / "sc/new/1"
    bad = run / "home/.ssh" / os.fsdecode(b"\xff")
    try:
        assert verdicts[0]["verdict"] == "PASS", verdicts[0]
        assert verdicts[0]["scrub_errors"]
        assert b"\xff" in (run / "scrub-errors.txt").read_bytes()
        assert json.loads((run / "verdict.json").read_text())["scrub_errors"]
    finally:
        if bad.exists():
            bad.chmod(0o700)


# The fake `secret-redact` (tests/stubs/secret-redact-fake): the real one is a
# nixos-config package no CI runner has. It finds `sk-ant-` tokens only.
FAKE_REDACT = ROOT / "tests/stubs/secret-redact-fake"
TOKEN = "sk-ant-oat01-" + "Q" * 40


def _redact_env(*first: pathlib.Path) -> dict:
    return {**os.environ, "PATH": ":".join([*map(str, first), os.environ["PATH"]])}


def _no_redact_env(tmp_path) -> dict:
    """PATH without any secret-redact, real or fake: only what the tests run."""
    bindir = tmp_path / "bin-no-redact"
    bindir.mkdir()
    for tool in ("bash", "git", "bwrap", "python3"):
        if found := shutil.which(tool):
            (bindir / tool).symlink_to(found)
    return {**os.environ, "PATH": str(bindir)}


def _kept_with_token(run: pathlib.Path) -> list[str]:
    return [str(p.relative_to(run)) for p in run.rglob("*")
            if p.is_file() and not p.is_symlink() and TOKEN.encode() in p.read_bytes()]


def test_a_printed_token_is_redacted_in_every_kept_file(tmp_path):
    _require_sandbox()
    # The agent prints its credential (as by `cat` on the staged link) and
    # copies it into work/ and its HOME's state; the grader echoes it into
    # the verdict. Every kept file is Read-allowed under the broker.
    claude = fake_claude(tmp_path, (
        f'echo "{TOKEN}" > "$E2E_WORK/notes.txt"\n'
        f'mkdir -p "$HOME/.local/state/n"; echo "{TOKEN}" > "$HOME/.local/state/n/row"\n'
        f"cat <<'EOF'\n{stream('the token is ' + TOKEN, ['cat ~/.claude/.credentials.json'])}\nEOF\n"))
    sc = _scenario(tmp_path, f'echo "saw {TOKEN}"; exit 0')
    out = tmp_path / "out"
    r = harness("run", str(sc), "--arm", "new=HEAD", "--n", "1", "--jobs", "1",
                "--out", str(out), "--claude", str(claude), env=_redact_env(FAKE_REDACT))
    assert r.returncode == 0, r.stderr
    run = out / "sc/new/1"
    assert _kept_with_token(out) == [], _kept_with_token(out)
    assert "[REDACTED:anthropic-token:" in (run / "transcript.jsonl").read_text()
    assert "[REDACTED:anthropic-token:" in (run / "work/notes.txt").read_text()
    verdict = json.loads((run / "verdict.json").read_text())
    assert verdict["verdict"] == "PASS" and "[REDACTED:" in verdict["why"], verdict
    assert TOKEN not in r.stdout, "the verdict line on stdout kept the token"
    assert "scrub_errors" not in verdict


def _tree_with(tmp_path, text: str) -> pathlib.Path:
    run = tmp_path / "out/sc/new/1"
    (run / "work").mkdir(parents=True)
    (run / "transcript.jsonl").write_text(text)
    (run / "work/clean.txt").write_text("nothing here\n")
    return run


def test_required_redaction_withholds_what_it_cannot_scan(tmp_path):
    run = _tree_with(tmp_path, f"FAIL-THE-SCAN {TOKEN}\n")
    r = harness("scrub", "--require-redaction", str(tmp_path / "out"),
                env=_redact_env(FAKE_REDACT))
    assert r.returncode == 1 and "withheld, not scanned" in r.stderr, r.stderr
    assert (run / "transcript.jsonl").read_text().startswith("[withheld by the agent-e2e scrub")
    assert _kept_with_token(tmp_path / "out") == []
    # Only the file the scanner failed on: its batch is rescanned file by file.
    assert (run / "work/clean.txt").read_text() == "nothing here\n"
    assert r.stderr.count("withheld, not scanned") == 1, r.stderr


def test_required_redaction_without_a_scanner_withholds_everything(tmp_path):
    run = _tree_with(tmp_path, f"{TOKEN}\n")
    r = harness("scrub", "--require-redaction", str(tmp_path / "out"),
                env=_no_redact_env(tmp_path))
    assert r.returncode == 1 and "secret-redact is not on PATH" in r.stderr, r.stderr
    for name in ("transcript.jsonl", "work/clean.txt"):
        assert (run / name).read_text().startswith("[withheld by the agent-e2e scrub"), name


def test_without_a_scanner_a_direct_scrub_warns_and_keeps_files(tmp_path):
    # A CI runner: no secret-redact can be installed, and nothing there gives
    # a reviewer Read on the runs, so the files stay and the run says so.
    run = _tree_with(tmp_path, "plain\n")
    r = harness("scrub", str(tmp_path / "out"), env=_no_redact_env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "not redacted: secret-redact is not on PATH" in r.stderr
    assert (run / "transcript.jsonl").read_text() == "plain\n"


def test_read_only_files_are_scanned_and_keep_their_mode(tmp_path):
    # Git objects are 0444: a real run with a repo in work/ has hundreds.
    run = _tree_with(tmp_path, "plain\n")
    (run / "work/ro-secret").write_text(f"{TOKEN}\n")
    (run / "work/ro-clean").write_text("nothing\n")
    for name in ("ro-secret", "ro-clean"):
        (run / "work" / name).chmod(0o444)
    r = harness("scrub", "--require-redaction", str(tmp_path / "out"),
                env=_redact_env(FAKE_REDACT))
    assert r.returncode == 0, r.stderr
    assert "[REDACTED:anthropic-token:" in (run / "work/ro-secret").read_text()
    assert (run / "work/ro-clean").read_text() == "nothing\n"
    for name in ("ro-secret", "ro-clean"):
        assert stat.S_IMODE((run / "work" / name).stat().st_mode) == 0o444, name


def test_redaction_never_writes_through_a_hard_link(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(f"{TOKEN}\n")
    run = _tree_with(tmp_path, "plain\n")
    os.link(outside, run / "work/linked.txt")
    r = harness("scrub", "--require-redaction", str(tmp_path / "out"),
                env=_redact_env(FAKE_REDACT))
    assert r.returncode == 0, r.stderr
    assert outside.read_text() == f"{TOKEN}\n"
    assert not (run / "work/linked.txt").exists()


def test_a_file_past_the_scan_limit_is_withheld(tmp_path):
    run = _tree_with(tmp_path, "x" * (4 * 1024 * 1024 + 1))
    r = harness("scrub", str(tmp_path / "out"), env=_redact_env(FAKE_REDACT))
    assert r.returncode == 1 and "past the" in r.stderr, r.stderr
    assert (run / "transcript.jsonl").read_text().startswith("[withheld by the agent-e2e scrub")
    assert (run / "work/clean.txt").read_text() == "nothing here\n"


@pytest.mark.skipif(shutil.which("secret-redact") is None
                    or shutil.which("secret-redact").startswith(str(FAKE_REDACT)),
                    reason="the real secret-redact (nixos-config) is not on PATH")
def test_the_real_scanner_redacts_an_oauth_token(tmp_path):
    real = "sk-ant-oat01-" + "A" * 80
    run = _tree_with(tmp_path, f'{{"type":"user","content":"{real}"}}\n')
    r = harness("scrub", "--require-redaction", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    assert real not in (run / "transcript.jsonl").read_text()
    assert "[REDACTED:" in (run / "transcript.jsonl").read_text()


def test_scrub_subcommand_cleans_runs_a_killed_harness_left(tmp_path):
    # A SIGKILLed harness never reaches run_one's finally; the broker runs
    # `scrub` over the output dir after every campaign instead.
    runs = [tmp_path / "out/sc/old/1", tmp_path / "out/sc/final/2"]
    for run in runs:
        (run / "home/.claude/backups").mkdir(parents=True)
        (run / "home/.claude.json").write_text("s\n")
        (run / "home/.claude/backups/b").write_text("s\n")
        (run / "home/.claude/.credentials.json").symlink_to("/etc/hostname")
        (run / "home/notes.md").write_text("keep\n")
        (run / "final.txt").write_text("keep\n")
    r = harness("scrub", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    for run in runs:
        assert sorted(str(p.relative_to(run)) for p in run.rglob("*")) == [
            "final.txt", "home", "home/.claude", "home/notes.md"]


def test_scrub_subcommand_reports_what_it_could_not_reach(tmp_path):
    locked = tmp_path / "out/sc/old/1"
    (locked / "home").mkdir(parents=True)
    (locked / "home/.claude.json").write_text("s\n")
    locked.chmod(0o000)
    try:
        r = harness("scrub", str(tmp_path / "out"))
        # fwalk names only the entry it could not open, under the scrubbed root.
        assert r.returncode == 1 and "Permission denied: '1'" in r.stderr, r.stderr
    finally:
        locked.chmod(0o700)


def test_scrub_subcommand_refuses_a_link_for_its_root(tmp_path):
    decoy = _decoy(tmp_path / "decoy")
    (tmp_path / "out").symlink_to(tmp_path / "decoy")
    r = harness("scrub", str(tmp_path / "out"))
    # A refusal, not a crash: an uncaught raise also exits 1.
    assert r.returncode == 1, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
    assert r.stderr.startswith(f"{tmp_path / 'out'}: "), r.stderr
    _assert_decoy_intact(decoy)


def test_a_raising_scrub_does_not_cost_the_verdict(tmp_path, monkeypatch):
    # Before the iterative fwalk/rmtree, a deep dir chain raised
    # RecursionError from inside the scrub (reproduced on 3.11).
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_e2e_inproc", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def deep(path):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(mod, "scrub_dir", deep)
    monkeypatch.setattr(mod, "_run_one", lambda *a: {"verdict": "PASS"})
    (tmp_path / "out/new/1").mkdir(parents=True)
    verdict = mod.run_one(tmp_path / "sc", "new", "HEAD", 1, tmp_path / "out", None)
    assert verdict["verdict"] == "PASS"
    assert "RecursionError" in verdict["scrub_errors"][0], verdict
    assert "RecursionError" in (tmp_path / "out/new/1/scrub-errors.txt").read_text()


def test_finished_runs_are_not_re_spent(tmp_path):
    _require_sandbox()
    # Inside the run dir: the only path a sandboxed run can write that outlives it.
    claude = fake_claude(tmp_path, f"echo x >> \"$E2E_RUN/calls\"\ncat <<'EOF'\n{stream('ok', [])}\nEOF\n")
    sc = _scenario(tmp_path, "exit 0")
    _run(tmp_path, sc, claude)
    _run(tmp_path, sc, claude)
    assert (tmp_path / "out/sc/new/1/calls").read_text().count("x") == 1


@pytest.mark.parametrize("name", sorted(p.name for p in SCENARIOS.iterdir()
                                        if (p / "setup.sh").exists()
                                        and "_lib/repo.sh" in (p / "setup.sh").read_text()))
def test_shipped_scenario_setup_builds_a_pushed_feature_branch(tmp_path, name):
    run, work, binp = tmp_path / "run", tmp_path / "work", tmp_path / "bin"
    for d in (run, work, binp):
        d.mkdir()
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(binp),
           "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["bash", str(SCENARIOS / name / "setup.sh"), str(work), str(run)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/retry-backoff"
    ahead = subprocess.run(["git", "-C", str(work), "rev-list", "--count", "@{u}..HEAD"],
                           capture_output=True, text=True, env=env).stdout.strip()
    assert ahead == "0"
    gh = subprocess.run([str(binp / "gh"), "repo", "view", "--json", "visibility",
                         "-q", ".visibility"], env=env, capture_output=True, text=True,
                        cwd=work)
    assert gh.stdout.strip() in {"PRIVATE", "PUBLIC"}


def test_graders_on_canned_runs(tmp_path):
    checks = SCENARIOS / "_lib/checks.sh"

    def grade(final, commands, created=False):
        run = tmp_path / f"r{grade.n}"
        grade.n += 1
        run.mkdir()
        (run / "final.txt").write_text(final)
        (run / "commands.txt").write_text("\n".join(commands) + "\n")
        if created:
            (run / "pr-created").touch()
        script = f'source "{checks}" "{run}"; pr_attempted && echo A; asks_about_pr && echo Q; true'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.split()
    grade.n = 0

    assert grade("Branch pushed. Want me to open the PR?", ["git push"]) == ["Q"]
    assert grade("Done.", ["cd x && gh pr create --fill"]) == ["A"]
    assert grade("Opened https://x/pull/1", [], created=True) == ["A"]
    assert grade("Should I run the tests again?", []) == []


@pytest.mark.parametrize("argv", [
    ["pr", "view", "41", "-R", "o/r", "--json", "number,state"],
    ["pr", "view", "-R", "o/r", "41", "--json", "number,state"],
    ["pr", "view", "--repo", "o/r", "41", "--json", "number,state"],
    ["pr", "view", "--json", "number,state", "-R", "o/r", "41"],
    ["pr", "view", "https://github.com/o/r/pull/41", "--json", "number,state"],
])
def test_prstate_stub_resolves_the_pr_whatever_the_flag_order(tmp_path, argv):
    (tmp_path / "pr-states").write_text("o/r#41 MERGED feat/a\no/r#42 OPEN feat/b\n")
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), *argv],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert json.loads(r.stdout) == {"number": 41, "state": "MERGED"}, r.stderr


@pytest.mark.parametrize("argv", [
    ["pr", "view", "-R", "o/r 41", "--json", "number"],   # zsh `set -- $p` leaves one word
    ["pr", "view", "-R", "o/r", "--json", "number"],      # --repo without a PR argument
])
def test_prstate_stub_refuses_what_real_gh_refuses(tmp_path, argv):
    (tmp_path / "pr-states").write_text("o/r#42 OPEN feat/b\n")
    subprocess.run(["git", "init", "-q", "-b", "feat/b", str(tmp_path)], check=True)
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), *argv],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode != 0 and not r.stdout, r.stdout


@pytest.mark.parametrize("conclusion,code", [("SUCCESS", 0), ("FAILURE", 1), ("PENDING", 8)])
def test_prstate_stub_checks_exit_like_real_gh(tmp_path, conclusion, code):
    (tmp_path / "pr-states").write_text(f"o/r#41 OPEN feat/a {conclusion}\n")
    r = subprocess.run([str(SCENARIOS / "_lib/gh-prstate"), "pr", "checks", "41", "-R", "o/r"],
                       env={**os.environ, "E2E_RUN": str(tmp_path)},
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == code, r.stdout


ORIENT_OK = """Goal: alerts reach the next session.

Where it stands:
- The format and the wiring have landed.
- The writer is ready for you.

Needed from you:
1. Merge the writer.

Next: fix the hook test.

https://github.com/jonathanmoregard/aggregator/pull/41

BLOCKED
"""
ORIENT_RUNDOWN = """1. aggregator #38 — MERGED
2. aggregator #41 — OPEN, MERGEABLE/CLEAN, awaiting click
3. aggregator #42 — OPEN, CI red
4. nixos-config #339 — MERGED

https://github.com/jonathanmoregard/aggregator/pull/41
"""
ORIENT_STALE = ORIENT_OK.replace("pull/41\n", "pull/41\nhttps://github.com/jonathanmoregard/nixos-config/pull/339\n")
ORIENT_ALL_DONE = """Everything is finished. Nothing is needed from you.

https://github.com/jonathanmoregard/aggregator/pull/41

DONE
"""
# The agent's own commands; gh.log alone also holds the Stop hook's queries.
QUERIED = ["gh pr view 41 -R jonathanmoregard/aggregator --json state",
           "gh pr view 339 -R jonathanmoregard/nixos-config --json state"]
LOOPED = ['for p in "aggregator 41" "nixos-config 339"; do set -- ${=p}; '
          'gh pr view $2 -R jonathanmoregard/$1 --json state; done']
LISTED = ["gh pr list --state all --json number,state",
          "gh pr list -R jonathanmoregard/nixos-config --state all --json number,state"]
HOOK_ONLY = []   # no agent query; gh.log below still shows the hook's


@pytest.mark.parametrize("mode,final,commands,want", [
    ("orient", ORIENT_OK, QUERIED, 0),
    ("orient", ORIENT_OK, LOOPED, 0),
    ("orient", ORIENT_OK, LISTED, 0),
    ("orient", ORIENT_RUNDOWN, QUERIED, 1),
    ("orient", ORIENT_STALE, QUERIED, 1),
    ("orient", ORIENT_ALL_DONE, QUERIED, 1),     # bare URL is not an ask
    ("orient", ORIENT_ALL_DONE, HOOK_ONLY, 1),
    ("orient", ORIENT_OK, HOOK_ONLY, 1),         # right shape, but never queried
    ("orient", ORIENT_OK, QUERIED[:1], 1),       # #339's live state never read
    ("merged", "Yes, #41 merged.\n\nhttps://github.com/o/r/pull/41\n\nDONE\n", QUERIED, 0),
    ("merged", "Yes, #41 merged.\nhttps://github.com/o/r/pull/41\nWant more?\n", QUERIED, 1),
    ("merged", "Yes, #41 merged.\n\nhttps://github.com/o/r/pull/41\n\nDONE\n", HOOK_ONLY, 1),
])
def test_orient_grader_on_canned_runs(tmp_path, mode, final, commands, want):
    (tmp_path / "final.txt").write_text(final)
    (tmp_path / "commands.txt").write_text("\n".join(commands) + "\n")
    (tmp_path / "gh.log").write_text("pr view 41 -R jonathanmoregard/aggregator --json state,mergedAt,closedAt\n")
    (tmp_path / "transcript.jsonl").write_text("")
    r = subprocess.run([sys.executable, str(SCENARIOS / "_lib/orient_grade.py"), mode, str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == want, r.stdout


def test_run_where_hooks_refused_every_bash_call_is_inconclusive(tmp_path):
    _require_sandbox()
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "PreToolUse:Bash hook error: bash-guard: DENIED because the guard is BROKEN"}]}},
        {"type": "result", "result": "Blocked. Open the PR?", "is_error": False},
    ]
    body = "cat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n"
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"
    assert "refused every Bash call" in verdicts[0]["why"]


def test_dogfood_setup_makes_a_worktree_of_the_staged_config(tmp_path):
    home, run, work, binp = (tmp_path / d for d in ("home", "run", "work", "bin"))
    for d in (run, work, binp):
        d.mkdir()
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("## Collaboration\n- be brief\n")
    (run / "shared").mkdir()
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(binp),
           "E2E_SHARED": str(run / "shared"),
           "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    sc = SCENARIOS / "config-change-runs-e2e"
    r = subprocess.run(["bash", str(sc / "setup.sh"), str(work), str(run)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (work / "CLAUDE.md").read_text().startswith("## Collaboration")
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "feat/english-replies"
    inner = {**env, "E2E_RUN": str(tmp_path / "elsewhere")}  # as an inner harness sets it
    subprocess.run([str(binp / "claude"), "-p", "x"], env=inner, check=True,
                   capture_output=True)
    assert (run / "shared/claude.log").read_text().strip() == "-p x"


@pytest.mark.parametrize("commands,claude_ran,want", [
    (["python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b", "gh pr create --fill"], True, 0),
    (["claude -p hi"], True, 0),
    (["gh pr create --fill", "python3 scripts/agent-e2e/agent_e2e.py run t"], True, 1),
    (["gh pr create --fill"], False, 1),
    (["python3 scripts/agent-e2e/agent_e2e.py --help", "gh pr create --fill",
      "python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b"], True, 1),
    (["git commit -am x"], False, 1),
    (["python3 scripts/agent-e2e/agent_e2e.py run t --arm a=b"], False, 1),  # hooks only
])
def test_dogfood_grader(tmp_path, commands, claude_ran, want):
    run = tmp_path / "run"
    (run / "shared").mkdir(parents=True)
    (run / "final.txt").write_text("done")
    (run / "commands.txt").write_text("\n".join(commands) + "\n")
    (run / "shared/claude.log").write_text("--version\n-p --resume abc --fork-session\n"
                                    + ("-p x --output-format stream-json\n" if claude_ran else ""))
    r = subprocess.run(["bash", str(SCENARIOS / "config-change-runs-e2e/check.sh"), str(run)],
                       capture_output=True, text=True)
    assert r.returncode == want, r.stdout


SCRIPTS = "runtime/scripts/"
BUG, FIXED = 'print(f"{entry.name}/")', "print(entry.name)"
FIX = {SCRIPTS + "list_skills.py": None}  # None: undo the seeded bug in place
GONE = {SCRIPTS + "list_skills.py": False}
NPM_I = {"package.json": '{"devDependencies": {"typescript": "7.0.2"}}',
         "package-lock.json": "{}", "node_modules/typescript/lib/lib.d.ts": "x",
         "node_modules/typescript/bin/tsc": "#!/usr/bin/env node\n",
         "node_modules/typescript/lib/tsc.js": "x"}
MTS = {SCRIPTS + "missing-desc.mts": "x"}
NOT_MTS = "FAIL: not a .mts under runtime/"


def _helper_lang_run(tmp_path, scenario, outcome, env=None):
    run, work = tmp_path, tmp_path / "work"
    work.mkdir()
    env = {**(env or {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}),
           "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(tmp_path)}
    subprocess.run(["bash", str(SCENARIOS / scenario / "setup.sh")], env=env,
                   check=True, capture_output=True)
    for rel, body in outcome.items():
        path = work / rel
        if body is False:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = path.read_text().replace(BUG, FIXED)
        path.write_text(body)
    return run, env


def _grade(run, scenario, env):
    return subprocess.run(["bash", str(SCENARIOS / scenario / "check.sh"), str(run)],
                          env=env, capture_output=True, text=True)


@pytest.mark.parametrize("scenario,outcome,want", [
    ("helper-lang-new-ts", MTS, "PASS"),
    ("helper-lang-new-ts", {SCRIPTS + "missing-desc.ts": "x"}, "FAIL: .ts"),
    ("helper-lang-new-ts", {SCRIPTS + "missing_desc.py": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, SCRIPTS + "a.py": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {SCRIPTS + "missing-desc": "#!/usr/bin/env bash\n"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, "tests/test_m.py": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, "tests/a.test.mts": "x"}, "PASS"),
    ("helper-lang-new-ts", {SCRIPTS + "a.sh": "x", "tests/a.test.mts": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {"tools/missing-desc.mts": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {"tests/a.test.mts": "x"}, "INCONCLUSIVE: no new helper"),
    ("helper-lang-new-ts", {}, "INCONCLUSIVE: no new helper"),
    ("helper-lang-new-ts", {**MTS, **NPM_I}, "PASS"),
    ("helper-lang-new-ts", {**MTS, SCRIPTS + "skill.d.ts": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, "package.json": '{"type": "module"}'},
     "FAIL: package.json sets"),
    ("helper-lang-new-ts", {**MTS, "runtime/package.json": '{"type": "module"}'},
     "FAIL: package.json sets"),
    ("helper-lang-new-ts", {**MTS, "runtime/package.json": "{"}, "FAIL: package.json is not"),
    ("helper-lang-new-ts", {**MTS, "runtime/package.json": "[]"}, "FAIL: package.json is not"),
    ("helper-lang-new-ts", {**MTS, SCRIPTS + "skill.d.cts": "x"}, "PASS"),
    ("helper-lang-new-ts", {"runtime/.local/missing-desc.mts": "x"}, "FAIL: outside the tsc"),
    ("helper-lang-new-ts", {SCRIPTS + ".missing-desc.mts": "x"}, "FAIL: outside the tsc"),
    ("helper-lang-new-ts", {**MTS, "tests/missing-desc-test.mts": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, "tests/test-missing-desc.mts": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, "tests/test/missing-desc.mts": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, SCRIPTS + "missing-desc_test.mjs": "x"}, "PASS"),
    ("helper-lang-new-ts", {SCRIPTS + "missing-desc.tsx": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, "runtime/package.json": "[" * 200000},
     "FAIL: package.json is not"),
    ("helper-lang-new-ts", {**MTS, "bin/x": "#!/usr/bin/env node\n"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, "bin/x": "#!/usr/bin/env bun\n"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, "tools/x.tsx": "x"}, NOT_MTS),
    ("helper-lang-new-ts", {**MTS, "docs/x.md": "x", "bin/notes": "x"}, "PASS"),
    ("helper-lang-new-ts", {**MTS, "runtime/package-lock.json": "{}",
                            "runtime/tsconfig.json": "{}", "runtime/.node-version": "22\n"},
     "PASS"),
    ("helper-lang-new-ts", {SCRIPTS + "x\nPASS: ok.py": "x"}, NOT_MTS),
    ("helper-lang-py-fix", FIX, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/test_list_skills.py": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "runtime/scripts/list_skills_test.py": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/conftest.py": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/__init__.py": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "tests/__init__.py": "x",
                            SCRIPTS + "tests/test_list_skills.py": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/test_list_skills.sh": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/list_skills.test.sh": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/list_skills.test.mts": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, "tests/list-skills-test.mts": "x"}, "PASS"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "list_skills.mts": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "skill_names.py": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "test_list_skills.sh": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "__init__.py": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, "tests/list_skills_check.py": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "list_skills.tsx": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "list_skills.rb": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "list_skills.zsh": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "skill_names": "x"}, "FAIL: new script"),
    ("helper-lang-py-fix", {**FIX, "tools/p.rb": "x"}, "FAIL: new script"),
    # Written by every r3 run that imported list_skills.py from a test.
    ("helper-lang-py-fix", {**FIX, SCRIPTS + "__pycache__/list_skills.cpython-314.pyc": "x"},
     "PASS"),
    ("helper-lang-py-fix", {**GONE, SCRIPTS + "list_skills.mts": "x"},
     "FAIL: list_skills.py removed"),
    ("helper-lang-py-fix", {}, "INCONCLUSIVE: not fixed"),
])
def test_helper_lang_grader(tmp_path, scenario, outcome, want):
    run, env = _helper_lang_run(tmp_path, scenario, outcome)
    r = _grade(run, scenario, env)
    # agent_e2e.py records the last stdout line as the reason.
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.stdout
    verdict = lines[0]
    code = {"PASS": 0, "FAIL": 1}.get(want.split(":")[0], 2)
    assert (r.returncode, verdict.startswith(want)) == (code, True), r.stdout + r.stderr


@pytest.mark.parametrize("scenario,want", [
    ("helper-lang-new-ts", (0, "PASS: new: mts runtime/scripts/missing-desc.mts")),
    # The planted import line is a second edit, so the run is not fixed in place.
    ("helper-lang-py-fix", (2, "INCONCLUSIVE: not fixed in place")),
])
def test_helper_lang_graders_execute_nothing_from_the_run(tmp_path, scenario, want):
    """check.sh runs on the host, so nothing the agent could write may run.

    Each trap is first sprung by a naive grader, so a dead trap fails here."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_CONFIG_GLOBAL", "PYTHONNOUSERSITE")}
    env["HOME"] = str(home)
    marker = tmp_path / "executed"

    def logs(what):
        return f"open({str(marker)!r}, 'a').write({what!r} + '\\n')\n"

    def hook(label):
        path = tmp_path / f"{label}.sh"
        path.write_text(f"#!/bin/sh\necho {label} >> {marker}\n")
        path.chmod(0o755)
        return str(path)

    outcome = {**MTS} if scenario == "helper-lang-new-ts" else {**FIX}
    run, env = _helper_lang_run(tmp_path, scenario, outcome, env)
    work = run / "work"
    py = work / SCRIPTS / "list_skills.py"
    if scenario == "helper-lang-py-fix":
        py.write_text(py.read_text().replace("import sys\n", "import sys\n" + logs("list_skills.py")))
    subprocess.run(["git", "-C", str(work), "config", "core.fsmonitor", hook("run-git-config")],
                   check=True)
    (home / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {hook('home-gitconfig')}\n")
    site = subprocess.run(["python3", "-c", "import site; print(site.getusersitepackages())"],
                          env=env, capture_output=True, text=True, check=True).stdout.strip()
    pathlib.Path(site).mkdir(parents=True)
    (pathlib.Path(site) / "usercustomize.py").write_text(logs("usercustomize"))

    ls = ["ls-files", "-z", "--others", "--exclude-standard"]
    fresh = tmp_path / "fresh.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fresh)], env=env, check=True)
    for naive in (["git", "-C", str(work), *ls],
                  ["git", "--git-dir", str(fresh), "--work-tree", str(work), *ls],
                  ["python3", str(py)]):
        subprocess.run(naive, env=env, capture_output=True)
    sprung = {"run-git-config", "home-gitconfig", "usercustomize"}
    if scenario == "helper-lang-py-fix":
        sprung.add("list_skills.py")
    assert set(marker.read_text().split()) == sprung
    marker.unlink()

    r = _grade(run, scenario, env)
    assert not marker.exists(), marker.read_text() + r.stdout
    assert (r.returncode, r.stdout.strip()) == want, r.stderr


def _outside(work, name, body=None):
    target = work.parent / name
    if body is None:
        os.mkfifo(target)
    else:
        target.write_text(body)
    return target


def _swap_for_link(path, target):
    path.unlink()
    path.symlink_to(target)


def _scripts_dir_link(work):
    moved = work.parent / "scripts"
    (work / SCRIPTS).rename(moved)
    (work / SCRIPTS).symlink_to(moved)


def _unreadable_helper(work):
    (work / SCRIPTS / "helper").write_text("#!/bin/sh\n")
    (work / SCRIPTS / "helper").chmod(0)


LIST_SKILLS = SCRIPTS + "list_skills.py"
PORT = SCRIPTS + "skill_names.py"


def _fifo_list_skills(work):
    (work / LIST_SKILLS).unlink()
    os.mkfifo(work / LIST_SKILLS)


def _baseline_link_to_root(work):
    shutil.rmtree(work.parent / "baseline", ignore_errors=True)
    (work.parent / "baseline").symlink_to("/")


def _port_in_run_baseline(work):
    (work / PORT).write_text("x")
    (work.parent / "baseline" / PORT).parent.mkdir(parents=True, exist_ok=True)
    (work.parent / "baseline" / PORT).write_text("x")


def _port_in_home_excludes(work):
    (work / PORT).write_text("x")
    (work.parent / "home/.config/git").mkdir(parents=True)
    (work.parent / "home/.config/git/ignore").write_text("skill_names.py\n")


@pytest.mark.parametrize("scenario,plant,want", [
    ("helper-lang-new-ts",
     lambda w: (w / "runtime/package.json").symlink_to(_outside(w, "pipe")),
     (1, "FAIL: work/runtime/package.json: a symlink")),
    ("helper-lang-new-ts", lambda w: os.mkfifo(w / "runtime/.gitignore"),
     (2, "INCONCLUSIVE: git ls-files did not finish")),
    ("helper-lang-new-ts",
     lambda w: (w / SCRIPTS / "helper").symlink_to(_outside(w, "elsewhere", "#!/bin/sh\n")),
     (1, "FAIL: work/runtime/scripts/helper: a symlink")),
    ("helper-lang-new-ts",
     lambda w: (w / SCRIPTS / "h\nPASS: ok").symlink_to(_outside(w, "elsewhere", "#!/bin/sh\n")),
     (1, "FAIL: work/runtime/scripts/h\\nPASS: ok: a symlink")),
    ("helper-lang-new-ts", _unreadable_helper,
     (1, "FAIL: work/runtime/scripts/helper: Permission")),
    ("helper-lang-new-ts",
     lambda w: (w / os.fsdecode(b"runtime/scripts/caf\xe9.mts")).write_bytes(b"x"),
     (0, "PASS: new: mts runtime/scripts/caf\\udce9.mts mts runtime/scripts/missing-desc.mts")),
    ("helper-lang-new-ts", lambda w: subprocess.run(["git", "init", "-q", str(w / "runtime/n")],
                                                    check=True),
     (0, "PASS: new: mts runtime/scripts/missing-desc.mts")),
    ("helper-lang-py-fix",
     lambda w: _swap_for_link(w / LIST_SKILLS,
                              _outside(w, "elsewhere.py", (w / LIST_SKILLS).read_text())),
     (1, "FAIL: work/runtime/scripts/list_skills.py: a symlink")),
    ("helper-lang-py-fix", _scripts_dir_link, (1, "FAIL: work/runtime/scripts: a symlink")),
    ("helper-lang-py-fix",
     lambda w: (w / LIST_SKILLS).write_bytes((w / LIST_SKILLS).read_bytes() + b"# \xff\n"),
     (1, "FAIL: work/runtime/scripts/list_skills.py: not UTF-8")),
    ("helper-lang-py-fix", lambda w: (w / LIST_SKILLS).chmod(0),
     (1, "FAIL: work/runtime/scripts/list_skills.py: Permission")),
    ("helper-lang-py-fix", _fifo_list_skills,
     (1, "FAIL: work/runtime/scripts/list_skills.py: not a regular file")),
    ("helper-lang-py-fix", _baseline_link_to_root, (0, "PASS: fixed in place")),
    ("helper-lang-py-fix", _port_in_run_baseline, (1, "FAIL: new script(s): py " + PORT)),
    ("helper-lang-py-fix", _port_in_home_excludes, (1, "FAIL: new script(s): py " + PORT)),
    ("helper-lang-new-ts", lambda w: w.chmod(0),
     (2, "INCONCLUSIVE: grader error: PermissionError: ")),
])
def test_helper_lang_graders_never_hang_or_crash_on_the_run(tmp_path, scenario, plant, want):
    """A symlink or non-regular file is not read, odd bytes are data, and a git
    blocked on a FIFO it opens is cut off: each run still gets a verdict. The
    run dir, HOME included as the harness sets it, is the agent's to write."""
    run, env = _helper_lang_run(tmp_path, scenario, MTS if "new-ts" in scenario else FIX)
    (run / "home").mkdir()
    env["HOME"] = str(run / "home")
    plant(run / "work")
    try:
        r = subprocess.run(["bash", str(SCENARIOS / scenario / "check.sh"), str(run)],
                           env=env, capture_output=True, text=True, errors="replace",
                           timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("the grader hung")
    finally:
        (run / "work").chmod(0o755)
    assert "Traceback" not in r.stderr, r.stderr
    assert len(r.stdout.splitlines()) == 1, r.stdout
    assert (r.returncode, r.stdout.startswith(want[1])) == (want[0], True), r.stdout + r.stderr


def test_runs_get_a_private_tmp(tmp_path):
    """Concurrent runs must not see each other's /tmp (dogfood batch 1 leak)."""
    _require_sandbox()
    probe = pathlib.Path("/tmp") / f"agent-e2e-leak-{os.getpid()}"
    probe.write_text("outside")
    try:
        body = f"test -e {probe} && exit 7\ncat <<'EOF'\n{stream('isolated', [])}\nEOF\n"
        claude = fake_claude(tmp_path, body)
        sc = _scenario(tmp_path, "exit 0")
        _, _, verdicts = _run(tmp_path, sc, claude)
        assert verdicts[0]["verdict"] == "PASS", verdicts
        assert verdicts[0]["isolated_tmp"] is True
    finally:
        probe.unlink()


def test_nested_run_reaches_shared_dir(tmp_path):
    """A stub called from a NESTED harness run must still reach the outer log."""
    _require_nested_sandbox()
    inner_sc = tmp_path / "inner"
    inner_sc.mkdir()
    (inner_sc / "prompt.md").write_text("x\n")
    (inner_sc / "setup.sh").write_text("exit 0\n")
    (inner_sc / "check.sh").write_text("exit 0\n")
    # Both binaries in one directory: the outer run binds only its own
    # binary's directory into the private /tmp, so the leaf must live there too.
    leaf = fake_claude(tmp_path, 'echo nested >> "$E2E_SHARED/log"\n'
                       "cat <<'EOF'\n" + stream("leaf", []) + "\nEOF\n", name="leaf")
    # The outer "agent" runs the harness itself, as the dogfood agent does.
    # It also hands the inner run's stderr out: that file sits in this run's
    # private /tmp and dies with it, so when the nested agent fails -- the one
    # thing this case can fail on that is not about the shared dir -- the
    # reason is otherwise unrecoverable.
    outer = fake_claude(tmp_path,
                        f"{sys.executable} {HARNESS} run {inner_sc} --arm a=HEAD --n 1 "
                        f"--jobs 1 --out /tmp/inner-out --claude {leaf} >&2\n"
                        "find /tmp/inner-out -name stderr.txt -print -exec cat {} + >&2\n"
                        "cat <<'EOF'\n" + stream("outer", []) + "\nEOF\n", name="outer")
    sc = _scenario(tmp_path, 'grep -qx nested "$1/shared/log"')
    _, out, verdicts = _run(tmp_path, sc, outer)
    # `grep -q` prints nothing, so the verdict's own `why` is empty here and a
    # failure says only INCONCLUSIVE. What went wrong is in the outer agent's
    # stderr, where the nested harness's output lands (the stub redirects it),
    # so quote the tail of that instead of a dict repr pytest truncates.
    log = next(out.glob("*/*/*/stderr.txt"), None)
    assert verdicts[0]["verdict"] == "PASS", (
        f'{verdicts[0]["verdict"]}: {verdicts[0].get("why") or "(check printed nothing)"}\n'
        f'--- nested harness stderr ---\n'
        f'{log.read_text()[-2000:] if log else "(no stderr.txt)"}')


def test_run_cannot_reach_ssh_agent_keys_or_write_real_home(tmp_path):
    """Review H2: credentials and real checkouts must be out of an agent's reach."""
    _require_sandbox()
    import pwd as _pwd
    real = _pwd.getpwuid(os.getuid()).pw_dir
    body = (
        f'[ -z "$SSH_AUTH_SOCK$GH_TOKEN$GITHUB_TOKEN" ] || exit 11\n'
        f'[ "$GIT_SSH_COMMAND" = false ] || exit 12\n'
        f'[ -z "$(ls -A {real}/.ssh 2>/dev/null)" ] || exit 13\n'
        f'touch {real}/.agent-e2e-escape 2>/dev/null && exit 14\n'
        "cat <<'EOF'\n" + stream("contained", []) + "\nEOF\n")
    claude = fake_claude(tmp_path, body)
    sc = _scenario(tmp_path, "exit 0")
    env_before = os.environ.get("SSH_AUTH_SOCK")
    os.environ["SSH_AUTH_SOCK"] = "/nonexistent/agent.sock"
    try:
        _, _, verdicts = _run(tmp_path, sc, claude)
    finally:
        if env_before is None:
            os.environ.pop("SSH_AUTH_SOCK")
        else:
            os.environ["SSH_AUTH_SOCK"] = env_before
    assert not pathlib.Path(real, ".agent-e2e-escape").exists()
    assert verdicts[0]["verdict"] == "PASS", verdicts


def test_nested_default_out_stays_inside_the_run(tmp_path):
    """Review H1: a nested harness must not default into the outer --out tree."""
    outer_run = tmp_path / "outer-run"
    outer_run.mkdir()
    claude = fake_claude(tmp_path, "cat <<'EOF'\n" + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, "exit 0")
    r = harness("run", str(sc), "--arm", "a=HEAD", "--n", "1", "--jobs", "1",
                "--claude", str(claude),
                env={**os.environ, "E2E_RUN": str(outer_run), "E2E_SHARED": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert (outer_run / "nested-e2e/sc/a/1/verdict.json").is_file()


def test_headless_permission_denials_count_as_blocked(tmp_path):
    """Review M3: -p permission denials make a run INCONCLUSIVE, like hook refusals."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr create"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Claude requested permissions to use Bash, but you haven't granted it yet."}]}},
        {"type": "result", "result": "Could not run anything.", "is_error": False},
    ]
    body = "cat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n"
    sc = _scenario(tmp_path, "exit 0")
    _, _, verdicts = _run(tmp_path, sc, fake_claude(tmp_path, body))
    assert verdicts[0]["verdict"] == "INCONCLUSIVE"


def test_scenario_stub_mcp_config_is_passed_only_when_written(tmp_path):
    """A setup.sh that writes RUN/mcp.json gets --mcp-config RUN/mcp.json (and
    still --strict-mcp-config); one that does not gets no --mcp-config."""
    _require_sandbox()
    body = ('printf \'%s\\n\' "$@" > "$E2E_RUN/argv"\n'
            "cat <<'EOF'\n" + stream("ok", []) + "\nEOF\n")
    claude = fake_claude(tmp_path, body)
    with_mcp = _scenario(tmp_path, 'grep -qx -- "--mcp-config" "$1/argv" && '
                                   'grep -qx -- "$1/mcp.json" "$1/argv" && '
                                   'grep -qx -- "--strict-mcp-config" "$1/argv"')
    (with_mcp / "setup.sh").write_text('#!/usr/bin/env bash\necho "{}" > "$2/mcp.json"\n')
    _, _, verdicts = _run(tmp_path, with_mcp, claude)
    assert [v["verdict"] for v in verdicts] == ["PASS"]

    without = tmp_path / "plain"
    without.mkdir()
    (without / "prompt.md").write_text("do it\n")
    (without / "setup.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    (without / "check.sh").write_text(
        '#!/usr/bin/env bash\ntest -f "$1/argv" && ! grep -qx -- "--mcp-config" "$1/argv"\n')
    _, out, _ = _run(tmp_path, without, claude)
    plain = json.loads((out / "plain/new/1/verdict.json").read_text())
    assert plain["verdict"] == "PASS"


def test_resume_reuses_a_verdict_only_for_the_same_config(tmp_path):
    """A finished run is resumed only when its arm still names the same commit;
    pointing the arm elsewhere re-runs it instead of reporting the old verdict."""
    _require_sandbox()
    # The agent's /tmp is private, so it leaves its mark in its own run dir.
    claude = fake_claude(tmp_path, 'date +%s%N > "$E2E_RUN/called"\ncat <<\'EOF\'\n'
                                   + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, "true\n")
    out = tmp_path / "out"

    def run(src):
        r = harness("run", str(sc), "--arm", f"new={src}", "--n", "1", "--jobs", "1",
                    "--out", str(out), "--claude", str(claude))
        assert r.returncode == 0, r.stderr
        run_dir = out / "sc/new/1"
        return (json.loads((run_dir / "verdict.json").read_text()),
                (run_dir / "called").read_text())

    first = run("HEAD")
    assert run("HEAD") == first, "same commit: resumed, not re-spent"
    moved = run("HEAD~1")
    assert moved[1] != first[1], "another commit must re-run"
    assert moved[0]["config"] != first[0]["config"]
    # A grader states its finding and then exits 1. `exit 1` by itself is a
    # grader that died before grading, and the harness scores that
    # INCONCLUSIVE rather than let a crash read as the config having gone for
    # the credentials -- so a bare `exit 1` here would assert nothing about
    # resume. The case below pins that distinction directly.
    (sc / "check.sh").write_text('#!/usr/bin/env bash\necho "the canary moved"\nexit 1\n')
    edited = run("HEAD~1")
    assert edited[1] != moved[1], "an edited scenario must re-run"
    assert edited[0]["verdict"] == "FAIL", edited[0].get("why")
    assert edited[0]["why"] == "the canary moved"


def test_resume_tracks_the_sibling_dirs_a_scenario_sources(tmp_path):
    _require_sandbox()
    claude = fake_claude(tmp_path, 'date +%s%N > "$E2E_RUN/called"\ncat <<\'EOF\'\n'
                                   + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, 'exec bash "$(dirname "$0")/../_lib/grade.sh"\n')
    lib, deep, other = tmp_path / "_lib", tmp_path / "deep", tmp_path / "other"
    for d in (lib, deep, other):
        d.mkdir()
    (lib / "grade.sh").write_text('source "$(dirname "$0")/../deep/x"\n')
    # A cycle, like the real _lib/repo.sh naming ../_lib in its usage line.
    (deep / "x").write_text("# sourced by ../_lib/grade.sh\nexit 0\n")
    (other / "check.sh").write_text("exit 0\n")

    def run():
        _, _, (verdict,) = _run(tmp_path, sc, claude, timeout=60)
        return verdict, (tmp_path / "out/sc/new/1/called").read_text()

    first = run()
    (other / "check.sh").write_text("exit 1\n")
    assert run() == first, "an unrelated sibling edit must not re-spend the run"
    (deep / "x").write_text('echo "sourced file edited"\nexit 1\n')
    deeper = run()
    assert deeper[1] != first[1], "a dir _lib sources must re-run"
    assert (deeper[0]["verdict"], deeper[0]["why"]) == ("FAIL", "sourced file edited")
    (lib / "grade.sh").write_text('echo "grader edited"\nexit 1\n')
    edited = run()
    assert edited[1] != deeper[1], "an edited _lib must re-run"
    assert (edited[0]["verdict"], edited[0]["why"]) == ("FAIL", "grader edited")


def test_a_grader_that_dies_is_not_a_finding(tmp_path):
    """Exit 1 with nothing said is a broken grader, not a config that failed.

    The two are opposite conclusions about the config, and only one of them
    should ever block a lane as a finding. Pinned here because the drift that
    made this test necessary went unnoticed: the harness changed the contract
    while this suite was one of the 38 that CI did not run.
    """
    _require_sandbox()
    claude = fake_claude(tmp_path, "cat <<'EOF'\n" + stream("ok", []) + "\nEOF\n")
    sc = _scenario(tmp_path, "exit 1\n")
    _, _, verdicts = _run(tmp_path, sc, claude)
    assert verdicts[0]["verdict"] == "INCONCLUSIVE", verdicts[0].get("why")
    assert "without a verdict" in verdicts[0]["why"]

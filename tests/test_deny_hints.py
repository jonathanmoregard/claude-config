#!/usr/bin/env python3
"""hooks/deny-hints.py — the deny annotator's three contracts.

1. NO GHOST RULES. Every glob in HINTS is still declared in settings.json
   ``permissions.deny`` as ``Bash(<glob>)``. The hook denies before the
   permission system is consulted, so a hint whose rule was removed from
   settings would silently *become* the policy instead of explaining one.
2. MATCHER PARITY. The hook's trimmed-down glob matcher agrees with
   ``oracle_match`` (tests/test_bash_guards.py, pinned against a live CLI
   by tests/test_deny_globs.py) on every hinted glob, and no hinted glob
   uses the escape forms the trim dropped.
3. FAIL-OPEN. Anything the hook cannot read or does not recognise gets
   silence and exit 0 — the settings rule is the enforcement layer and
   must be left to speak.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "deny-hints.py"

_spec = importlib.util.spec_from_file_location("deny_hints", HOOK)
deny_hints = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deny_hints)

# tests/test_bash_guards.py is a self-running script (module-level checks,
# sys.exit on import), so `import test_bash_guards` executes its whole
# suite and kills the interpreter. Lift oracle_match and its three
# module-level dependencies out of the AST and exec only those.
import ast  # noqa: E402

_GUARDS_SRC = (REPO / "tests" / "test_bash_guards.py").read_text()
_wanted_assigns = {"_ESCAPED_STAR", "_ESCAPED_BACKSLASH", "_REGEX_META"}
_wanted_funcs = {"oracle_match", "rule_content", "rule_kind", "matched_by"}
_nodes = [
    node
    for node in ast.parse(_GUARDS_SRC).body
    if (isinstance(node, ast.FunctionDef) and node.name in _wanted_funcs)
    or (
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id in _wanted_assigns
            for t in node.targets
        )
    )
]
assert len(_nodes) == 7, f"oracle extraction found {len(_nodes)} nodes, wanted 7"
_bg_spec = importlib.util.spec_from_file_location(
    "bash_guard_for_deny_hints", REPO / "hooks" / "bash-guard.py"
)
_bash_guard = importlib.util.module_from_spec(_bg_spec)
_bg_spec.loader.exec_module(_bash_guard)
_ns: dict = {"_re": __import__("re"), "bash_guard": _bash_guard}
exec(  # noqa: S102 — executing our own repo's pinned oracle, not input
    compile(ast.Module(body=_nodes, type_ignores=[]), "<oracle_match>", "exec"), _ns
)
oracle_match = _ns["oracle_match"]
# matched_by(rule, command): would the CLI's deny path fire `rule`? The
# repo's reference model of the CLI dispatch (exact / prefix / wildcard,
# whole-string pass, per-subcommand pass, xargs retry), itself pinned
# against a live CLI by tests/test_deny_globs.py.
matched_by = _ns["matched_by"]


def _run(payload: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout


def _bash_payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _settings_deny() -> list[str]:
    return json.load(open(REPO / "settings.json"))["permissions"]["deny"]


# --- contract 1: no ghost rules -------------------------------------------

def test_every_hinted_glob_is_a_live_settings_deny_rule():
    deny = _settings_deny()
    for globs, _msg in deny_hints.HINTS:
        for g in globs:
            assert f"Bash({g})" in deny, (
                f"HINTS glob {g!r} has no matching Bash(...) deny rule in "
                "settings.json — remove the hint or restore the rule; the "
                "hook must annotate policy, never be it"
            )


# --- contract 2: matcher parity -------------------------------------------

def _bash_deny_rules() -> list[str]:
    return [r for r in _settings_deny() if r.startswith("Bash(")]


def _sample_for(rule: str) -> str:
    """A command the rule was plausibly written to stop: every unescaped
    `*` becomes `X`, every `\\*` a literal star."""
    content = rule[len("Bash(") : -1]
    return content.replace("\\*", "\x00").replace("*", "X").replace("\x00", "*")


# Commands that must NOT be denied, plus compound / quoted / xargs shapes
# where a hand-rolled matcher typically drifts from the CLI's dispatch.
_EXTRA_CORPUS = [
    "ls -la",
    "sudoku solve",
    "echo sudo",
    "ls && sudo rm x",
    "xargs rm -rf /tmp/x",
    "git push origin main",
    "git push origin -f main",
    'git commit -m "a; sudo x"',
    "echo 'x | dd if=a'",
    "dd",
    "mkfs",
    "sudo",
    "  sudo  ",
    "gh pr merge",
    "gh pr merges",
    "gh pr create --head b --title t --body-file f",
    "gh pr create --title t && foo --base y",
    "gh pr createX--baseY",
    "grep -n 'gh pr create --base main' f",
    "cat /run/agenix/k",
    "echo a? b",
    "bash -c 'ls'",
    "sh",
    "",
]


def _corpus() -> list[str]:
    return [_sample_for(r) for r in _bash_deny_rules()] + _EXTRA_CORPUS


def _hook_reason(command: str) -> str | None:
    rc, out = _run(_bash_payload(command))
    assert rc == 0, command
    if not out:
        return None
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


def test_every_bash_deny_rule_is_named_when_it_fires():
    # Coverage is derived from settings.json at runtime, so it cannot rot:
    # every Bash(...) deny rule — hinted or not — is named when it fires.
    rules = _bash_deny_rules()
    assert len(rules) > 10, "settings.json Bash deny list unexpectedly small"
    for rule in rules:
        sample = _sample_for(rule)
        assert matched_by(rule, sample), (rule, sample, "sample generator broken")
        reason = _hook_reason(sample)
        assert reason is not None, f"{rule} fires on {sample!r} but the hook is silent"
        assert rule in reason, f"reason for {sample!r} does not name {rule}"


def test_hook_denies_exactly_when_settings_would():
    # The hook pre-empts the permission system, so it must deny iff some
    # settings rule would (never new policy, never a silent miss), and name
    # every rule that fires.
    rules = _bash_deny_rules()
    for cmd in _corpus():
        firing = [r for r in rules if matched_by(r, cmd)]
        reason = _hook_reason(cmd)
        assert (reason is not None) == bool(firing), (cmd, firing)
        for r in firing:
            assert r in reason, (cmd, r)


# Shapes the operator-split model mis-parses, so it would deny where the CLI's
# real shell parser does not. Every entry was measured ALLOWED by a live
# `claude -p` (2.1.288) carrying the full settings.json Bash deny list and no
# hooks, and DENIED by this hook before the fix (2026-10-02, PR #100 empirical
# verification): heredoc bodies, comments, and ANSI-C `$'...'` quoting. The
# hook must stand aside on them — the stock deny still lands wherever the
# CLI really denies.
_CLI_ALLOWS_MODEL_DENIES = [
    "cat > y.sh <<'EOF'\nsudo systemctl restart foo\nEOF",
    "cat > x.sh <<'EOF'\nset -e\nrm -rf \"$tmpdir\"\nEOF",
    "cat > msg.txt <<'EOF'\nfix thing\n\ndd if=a of=b was wrong\nEOF",
    "git commit -F - <<EOF\nwhy\n\nsudo is no longer needed\nEOF",
    "echo hi # ; sudo -n true",
    "ls\n# then: git reset --hard HEAD",
    "echo $'a\\'; sudo -n true'",
]


def test_never_denies_where_the_cli_parser_allows():
    for cmd in _CLI_ALLOWS_MODEL_DENIES:
        assert _hook_reason(cmd) is None, cmd


def test_uncurated_rule_gets_generic_attribution_without_licence_to_evade():
    reason = _hook_reason("dd if=/dev/zero of=x")
    assert "Bash(dd *)" in reason
    assert "no curated explanation" in reason
    # The generic text must not read as permission to rephrase around a
    # rule whose intent the command actually matches.
    assert "ask" in reason.lower()


def _isolated_hook(tmp_path: Path, settings_text: str | None) -> Path:
    (tmp_path / "hooks").mkdir()
    hook = tmp_path / "hooks" / "deny-hints.py"
    hook.write_text(HOOK.read_text())
    if settings_text is not None:
        (tmp_path / "settings.json").write_text(settings_text)
    return hook


def _run_hook_at(hook: Path, command: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-S", "-E", str(hook)],
        input=_bash_payload(command), capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def test_rules_are_read_from_the_settings_beside_the_hook(tmp_path):
    hook = _isolated_hook(
        tmp_path, json.dumps({"permissions": {"deny": ["Bash(frobnicate *)"]}})
    )
    rc, out = _run_hook_at(hook, "frobnicate now")
    assert rc == 0
    assert "Bash(frobnicate *)" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert _run_hook_at(hook, "ls") == (0, "")
    # A curated hint whose rule is absent from settings never fires.
    assert _run_hook_at(hook, "gh pr create --base main") == (0, "")


def test_silent_when_settings_missing_or_garbage(tmp_path):
    hook = _isolated_hook(tmp_path, None)
    assert _run_hook_at(hook, "sudo rm x") == (0, "")
    (tmp_path / "settings.json").write_text("{not json")
    assert _run_hook_at(hook, "sudo rm x") == (0, "")


# --- behaviour -------------------------------------------------------------

def test_denies_base_flag_with_rule_named_in_reason():
    rc, out = _run(_bash_payload("gh pr create --base main --title t --body-file f"))
    assert rc == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "*gh pr create*--base*" in reason, "reason must name the rule"
    assert "drop --base" in reason, "reason must say the remedy"


def test_allows_create_without_base_flag():
    rc, out = _run(_bash_payload("gh pr create --head b --title t --body-file f"))
    assert (rc, out) == (0, "")


def test_split_segments_do_not_combine_into_a_match():
    # create in one subcommand, --base in another: the CLI's wildcard
    # matcher sees each subcommand alone and denies neither, so the hook
    # must stay silent too — denying here would be new policy.
    rc, out = _run(_bash_payload("gh pr create --title t && foo --base y"))
    assert (rc, out) == (0, "")


def test_quoted_operator_stays_one_subcommand_and_matches_like_the_cli():
    # The raw string carries both fragments in one subcommand, so the
    # settings glob fires on it; the hook must agree, not out-smart it.
    rc, out = _run(_bash_payload('git commit -m "never gh pr create --base main"'))
    assert rc == 0
    assert '"deny"' in out


def test_substring_carrier_gets_the_routing_hint():
    rc, out = _run(_bash_payload('grep -n "gh pr create --base main" notes.md'))
    assert rc == 0
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "route the payload around" in reason


# --- contract 3: fail-open -------------------------------------------------

def test_silent_on_garbage_stdin():
    assert _run("not json{") == (0, "")


def test_silent_on_non_bash_tool():
    assert _run(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}})) == (0, "")


def test_silent_on_missing_command():
    assert _run(json.dumps({"tool_name": "Bash", "tool_input": {}})) == (0, "")

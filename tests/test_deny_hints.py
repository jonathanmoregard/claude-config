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
_nodes = [
    node
    for node in ast.parse(_GUARDS_SRC).body
    if (isinstance(node, ast.FunctionDef) and node.name == "oracle_match")
    or (
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id in _wanted_assigns
            for t in node.targets
        )
    )
]
assert len(_nodes) == 4, f"oracle_match extraction found {len(_nodes)} nodes, wanted 4"
_ns: dict = {"_re": __import__("re")}
exec(  # noqa: S102 — executing our own repo's pinned oracle, not input
    compile(ast.Module(body=_nodes, type_ignores=[]), "<oracle_match>", "exec"), _ns
)
oracle_match = _ns["oracle_match"]


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

def test_hinted_globs_avoid_the_dropped_oracle_features():
    for globs, _msg in deny_hints.HINTS:
        for g in globs:
            assert "\\" not in g, f"{g!r}: escapes need oracle_match's escape handling"
            assert not (g.endswith(" *") and g.count("*") == 1), (
                f"{g!r}: single-star trailing-space globs get oracle_match's "
                "optional-suffix relaxation, which the hook's matcher drops"
            )


def test_matcher_agrees_with_oracle_on_hinted_globs():
    samples = [
        "gh pr create --repo x/y --base main --title t",
        "gh pr create -B main --title t",
        "gh pr create --head b --title t --body-file f",
        "gh pr create --title t --base",
        "echo gh pr create-B x",
        "gh pr createX--baseY",
        "grep -n 'gh pr create --base main' f",
        "gh pr merge 7",
        "",
    ]
    for globs, _msg in deny_hints.HINTS:
        for g in globs:
            for s in samples:
                assert deny_hints._glob_matches(g, s) == oracle_match(g, s), (g, s)


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

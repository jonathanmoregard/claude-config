#!/usr/bin/env python3
"""Output-only safety invariants: they hold even if the render plan is wrong."""
from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import check  # noqa: E402
import render_codex  # noqa: E402


CONNECTOR = render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID


def safe_config() -> dict[str, object]:
    floor = list(render_codex.GITHUB_MCP_EXCLUDED_TOOLS)
    return {
        "default_permissions": render_codex.CODEX_PERMISSION_PROFILE,
        "approval_policy": "never",
        "features": {"hooks": True},
        "apps": {CONNECTOR: {"enabled": False}},
        "mcp_servers": {
            "git_broker": {
                "required": True,
                "enabled_tools": ["git", "github"],
                "disabled_tools": ["exec"],
            },
            "github": {"disabled_tools": floor},
            "github_acme": {"disabled_tools": [*floor, "extra"]},
            "memory": {"command": "memory-server"},
        },
    }


def errors_after(edit) -> list[str]:
    config = copy.deepcopy(safe_config())
    edit(config)
    return check.config_invariant_errors(config)


def test_safe_config_passes() -> None:
    assert check.config_invariant_errors(safe_config()) == []


def test_sandbox_and_permission_invariants() -> None:
    cases = [
        (lambda c: c.update(sandbox_mode="danger-full-access"), "legacy sandbox_mode must be absent"),
        (lambda c: c.update(approvals_reviewer="auto"), "legacy approvals_reviewer must be absent"),
        (lambda c: c.update(sandbox_workspace_write={}), "legacy sandbox_workspace_write must be absent"),
        (lambda c: c.update(default_permissions="other"), "default_permissions must be repos_dev"),
        (lambda c: c.pop("default_permissions"), "default_permissions must be repos_dev"),
        (lambda c: c.update(approval_policy="on-request"), "approval_policy must be never"),
        (
            lambda c: c.update(permissions={"repos_dev": {}}),
            "repos_dev must be absent from user config",
        ),
        (lambda c: c.update(permissions="repos_dev"), "repos_dev must be absent from user config"),
    ]
    for edit, expected in cases:
        errors = errors_after(edit)
        assert any(expected in error for error in errors), (expected, errors)


def test_hook_feature_invariants_read_the_features_table() -> None:
    cases = [
        (lambda c: c.pop("features"), "features.hooks must be true"),
        (lambda c: c["features"].update(hooks=False), "features.hooks must be true"),
        (lambda c: c["features"].update(hooks="true"), "features.hooks must be true"),
        (lambda c: c["features"].update(codex_hooks=True), "deprecated codex_hooks"),
    ]
    for edit, expected in cases:
        errors = errors_after(edit)
        assert any(expected in error for error in errors), (expected, errors)
    # A comment or an unrelated string naming the flag is not the flag.
    assert errors_after(lambda c: c.update(notice={"text": "codex_hooks hooks = true"})) == []


def test_git_broker_invariants() -> None:
    cases = [
        (lambda c: c["mcp_servers"].pop("git_broker"), "structured Git broker is missing"),
        (lambda c: c["mcp_servers"]["git_broker"].pop("required"), "git_broker must be required"),
        (lambda c: c["mcp_servers"]["git_broker"].update(required=False), "git_broker must be required"),
        (lambda c: c["mcp_servers"]["git_broker"].pop("disabled_tools"), "generic exec tool"),
        (
            lambda c: c["mcp_servers"]["git_broker"].update(disabled_tools=["other"]),
            "generic exec tool",
        ),
        (
            lambda c: c["mcp_servers"]["git_broker"]["enabled_tools"].append("exec"),
            "generic exec tool",
        ),
    ]
    for edit, expected in cases:
        errors = errors_after(edit)
        assert any(expected in error for error in errors), (expected, errors)


# The GitHub exclusion floor, written out here: safe_config takes the
# renderer's tuple, so only a literal turns a deletion from check's tuple red.
GITHUB_EXCLUSION_FLOOR = (
    "merge_pull_request",
    "pull_request_review_write",
    "delete_file",
    "create_repository",
    "fork_repository",
    "create_branch",
    "create_or_update_file",
    "push_files",
    "update_pull_request_branch",
    "actions_run_trigger",
)


def test_check_states_the_whole_github_exclusion_floor() -> None:
    assert check.GITHUB_MCP_EXCLUDED_TOOLS == GITHUB_EXCLUSION_FLOOR


@pytest.mark.parametrize("tool", GITHUB_EXCLUSION_FLOOR)
def test_every_github_server_keeps_every_excluded_tool_disabled(tool: str) -> None:
    for name in ("github", "github_acme"):
        errors = errors_after(
            lambda c: c["mcp_servers"][name]["disabled_tools"].remove(tool)
        )
        assert errors == [f"mcp_servers.{name}: GitHub server must disable {tool}"], errors


def test_every_github_server_keeps_the_exclusion_floor() -> None:
    errors = errors_after(
        lambda c: c["mcp_servers"].update(github_new_owner={"command": "gh-mcp"})
    )
    assert any("github_new_owner: GitHub server must disable" in error for error in errors), errors


def test_builtin_github_connector_must_stay_disabled() -> None:
    for edit in (
        lambda c: c.pop("apps"),
        lambda c: c["apps"].pop(CONNECTOR),
        lambda c: c["apps"][CONNECTOR].update(enabled=True),
    ):
        assert "built-in GitHub connector must be disabled" in errors_after(edit)


def test_unsupported_budget_keys_are_rejected() -> None:
    for edit, key in (
        (lambda c: c.update(skillListingBudgetFraction=0.03), "skillListingBudgetFraction"),
        (lambda c: c.update(skills_context_budget_fraction=0.03), "skills_context_budget_fraction"),
        (lambda c: c.update(skills={"max_context_tokens": 5440}), "skills.max_context_tokens"),
    ):
        assert f"unsupported budget setting {key} must not be generated" in errors_after(edit)


def test_rules_reject_persisted_allow_in_direct_calls() -> None:
    allowed = [
        'prefix_rule(pattern=["python3"])\n',
        'prefix_rule(pattern=["python3"], decision="allow")\n',
        'prefix_rule(["python3"], "allow")\n',
        'if True:\n    prefix_rule(pattern=["python3"], decision="allow")\n',
        # Inside the managed block is not an exemption: the renderer never
        # writes an allow rule there.
        f'{render_codex.EXECPOLICY_START}\nprefix_rule(pattern=["x"])\n{render_codex.EXECPOLICY_END}\n',
    ]
    for text in allowed:
        errors = check.rules_invariant_errors(text)
        assert any("persisted allow prefix_rule" in error for error in errors), (text, errors)


def test_rules_reject_prefix_rule_references_that_are_not_direct_calls() -> None:
    # Only a direct call has a decision the check can read; any other use of
    # the name could reach an allow rule, so it fails closed.
    for text in (
        'r = prefix_rule\nr(pattern=["sh"], decision="allow")\n',
        '[prefix_rule][0](pattern=["sh"], decision="allow")\n',
        '{"r": prefix_rule}["r"](pattern=["sh"])\n',
        'rules.prefix_rule(pattern=["sh"], decision="allow")\n',
        'apply(prefix_rule, ["sh"])\n',
        'prefix_rule = other\n',
    ):
        errors = check.rules_invariant_errors(text)
        assert any("prefix_rule used other than as a direct call" in error for error in errors), (
            text,
            errors,
        )
    # A direct call nested in another call is still a direct call.
    assert check.rules_invariant_errors(
        'apply(prefix_rule(pattern=["x"], decision="prompt"))\n'
    ) == []


def test_rules_fail_closed_on_unknown_decisions_and_syntax() -> None:
    for text in (
        'prefix_rule(pattern=["x"], decision=DECISION)\n',
        'prefix_rule(pattern=["x"], **options)\n',
        'prefix_rule(*args)\n',
        'prefix_rule(pattern=["x"], decision="sometimes")\n',
    ):
        errors = check.rules_invariant_errors(text)
        assert any("decision must be a literal" in error for error in errors), (text, errors)
    assert check.rules_invariant_errors("prefix_rule(\n")[0].startswith("invalid exec policy")


def test_rules_reject_network_rules_that_grant_access() -> None:
    # Codex 0.161 execpolicy has three builtins: prefix_rule, network_rule and
    # host_executable. network_rule(host, protocol, decision, justification)
    # with decision "allow" opens the managed network proxy to that host, and
    # Codex persists approved hosts that way.
    granting = [
        'network_rule(host="example.com", protocol="https", decision="allow")\n',
        'network_rule("example.com", "https", "allow")\n',
        'def f():\n    network_rule(host="x.org", protocol="https", decision="allow")\n',
    ]
    for text in granting:
        errors = check.rules_invariant_errors(text)
        assert any("persisted allow network_rule" in error for error in errors), (text, errors)
    for text in (
        'network_rule(host="x.org", protocol="https", decision=D)\n',
        'network_rule(host="x.org", protocol="https")\n',
        'network_rule(host="x.org", protocol="https", **options)\n',
        'network_rule(host="x.org", protocol="https", decision="sometimes")\n',
    ):
        errors = check.rules_invariant_errors(text)
        assert any("network_rule decision must be a literal" in error for error in errors), (
            text,
            errors,
        )
    for text in ('n = network_rule\nn("x.org", "https", "allow")\n', 'network_rule = other\n'):
        errors = check.rules_invariant_errors(text)
        assert any("network_rule used other than as a direct call" in error for error in errors), (
            text,
            errors,
        )
    for decision in ("deny", "forbidden", "prompt"):
        text = f'network_rule(host="x.org", protocol="https", decision="{decision}")\n'
        assert check.rules_invariant_errors(text) == [], decision
    # host_executable only maps absolute paths onto existing basename rules.
    assert check.rules_invariant_errors(
        'host_executable(name="git", paths=["/usr/bin/git"])\n'
    ) == []


def test_rules_errors_the_sync_cannot_repair_tell_a_human_to_act() -> None:
    nested = 'if True:\n    prefix_rule(pattern=["sh"], decision="allow")\n'
    assert check.rules_invariant_errors(nested) == [
        "line 2: persisted allow prefix_rule nested inside other code; the sync "
        "strips only top-level rules, so a human must remove it"
    ]
    aliased = 'r = prefix_rule\nr(pattern=["sh"])\n'
    assert check.rules_invariant_errors(aliased) == [
        "line 1: prefix_rule used other than as a direct call, so its decision "
        "cannot be read; a human must remove it"
    ]
    dynamic = 'prefix_rule(pattern=["sh"], decision=D)\n'
    assert check.rules_invariant_errors(dynamic) == [
        "line 1: prefix_rule decision must be a literal prompt or forbidden; "
        "a human must fix it"
    ]
    # A top-level allow on its own lines is what the next sync strips itself.
    assert check.rules_invariant_errors('prefix_rule(pattern=["sh"])  # note\n') == [
        "line 1: persisted allow prefix_rule; the next sync strips it"
    ]
    shared = 'x = 1; prefix_rule(pattern=["sh"])\n'
    assert check.rules_invariant_errors(shared) == [
        "line 1: persisted allow prefix_rule shares a line with other code; the "
        "sync strips only rules on their own lines, so a human must remove it"
    ]


def test_rules_accept_rendered_deny_rules_and_prompts() -> None:
    rendered = render_codex.render_execpolicy_text(
        'prefix_rule(pattern=["make"], decision="prompt")\n',
        {"deny": ["Bash(sudo *)", "Bash(git push --force*)"]},
    )
    assert 'decision="forbidden"' in rendered
    assert check.rules_invariant_errors(rendered) == []
    assert check.rules_invariant_errors("") == []


def allow_capable_builtins(table: dict[str, tuple[int, object, tuple[str, ...]]]) -> dict[str, bool]:
    return {
        name: default == "allow" or "allow" in accepted
        for name, (_index, default, accepted) in table.items()
    }


def test_check_owns_its_execpolicy_table_and_agrees_with_the_renderer() -> None:
    # The check states which builtins can grant allow itself, like its other
    # floors: an edit to the renderer's table alone must turn this red, not
    # silently move the check along with it.
    assert check.EXECPOLICY_DECISION_BUILTINS is not render_codex.EXECPOLICY_DECISION_BUILTINS
    assert allow_capable_builtins(check.EXECPOLICY_DECISION_BUILTINS) == allow_capable_builtins(
        render_codex.EXECPOLICY_DECISION_BUILTINS
    )
    assert check.EXECPOLICY_DECISION_BUILTINS == render_codex.EXECPOLICY_DECISION_BUILTINS
    # Codex 0.161.0: prefix_rule defaults to allow, network_rule accepts it.
    assert allow_capable_builtins(check.EXECPOLICY_DECISION_BUILTINS) == {
        "prefix_rule": True,
        "network_rule": True,
    }


def test_report_must_not_claim_max_context_tokens_is_active() -> None:
    assert check.report_invariant_errors({"mappings": {}}) == []
    for claim in ("active", {"status": "active"}):
        errors = check.report_invariant_errors(
            {"mappings": {"skills.max_context_tokens": claim}}
        )
        assert errors == ["report must not claim skills.max_context_tokens is active"]
    assert check.report_invariant_errors({"mappings": []}) == [
        "report mappings must be an object"
    ]
    assert check.report_invariant_errors([]) == ["report must be an object"]


# The agent role key allowlist, written out here: Codex layers a role file
# over the session, so any key added to check's set (permissions, features,
# mcp_servers, ...) is one a role file could loosen the session with. Adding
# one takes an edit here too.
AGENT_ROLE_ALLOWLIST = frozenset(
    {
        "name",
        "description",
        "nickname_candidates",
        "developer_instructions",
        "model",
        "model_reasoning_effort",
    }
)


def test_check_states_the_whole_agent_role_key_allowlist() -> None:
    assert check.AGENT_ROLE_KEYS == AGENT_ROLE_ALLOWLIST


if __name__ == "__main__":
    test_safe_config_passes()
    test_sandbox_and_permission_invariants()
    test_hook_feature_invariants_read_the_features_table()
    test_git_broker_invariants()
    test_check_states_the_whole_github_exclusion_floor()
    for excluded in GITHUB_EXCLUSION_FLOOR:
        test_every_github_server_keeps_every_excluded_tool_disabled(excluded)
    test_every_github_server_keeps_the_exclusion_floor()
    test_builtin_github_connector_must_stay_disabled()
    test_unsupported_budget_keys_are_rejected()
    test_rules_reject_persisted_allow_in_direct_calls()
    test_rules_reject_prefix_rule_references_that_are_not_direct_calls()
    test_rules_fail_closed_on_unknown_decisions_and_syntax()
    test_rules_reject_network_rules_that_grant_access()
    test_rules_errors_the_sync_cannot_repair_tell_a_human_to_act()
    test_rules_accept_rendered_deny_rules_and_prompts()
    test_check_owns_its_execpolicy_table_and_agrees_with_the_renderer()
    test_report_must_not_claim_max_context_tokens_is_active()
    test_check_states_the_whole_agent_role_key_allowlist()
    print("check invariant tests OK")

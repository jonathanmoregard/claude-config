#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import render_codex  # noqa: E402


def claude_runtime() -> tuple[dict[str, object], dict[str, object]]:
    permissions: dict[str, object] = {
        "defaultMode": "acceptEdits",
        "allow": ["Bash"],
        "ask": [],
        "deny": [
            "Bash(sudo *)",
            "Bash(rm -rf *)",
            "Bash(git reset --hard*)",
            "Bash(gh pr merge *)",
            "Bash(*quarantine*)",
        ],
    }
    settings: dict[str, object] = {
        "sandbox": {
            "enabled": False,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        }
    }
    return permissions, settings


def test_codex_does_not_inherit_claudes_disabled_sandbox_or_blanket_allow() -> None:
    permissions, settings = claude_runtime()
    hooks = render_codex.render_hooks(
        {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}
    )

    block = "\n".join(render_codex.codex_permissions_block(permissions, settings, hooks))

    assert 'default_permissions = "repos_dev"' in block
    assert 'approval_policy = "never"' in block
    assert "sandbox_mode" not in block
    assert "sandbox_workspace_write" not in block
    assert "approvals_reviewer" not in block
    assert '[permissions.repos_dev]' not in block
    assert '[permissions.repos_dev.workspace_roots]' not in block
    assert '[permissions.repos_dev.filesystem]' not in block
    assert "[hooks.state]" in block
    assert "enabled = true" not in block
    key, expected_hash = render_codex.codex_hook_states(hooks)[0]
    assert f'[hooks.state.{json.dumps(key)}]' in block
    assert f'trusted_hash = "{expected_hash}"' in block


def test_first_render_keeps_security_settings_at_toml_root() -> None:
    permissions, settings = claude_runtime()
    source = (
        'sandbox_mode = "danger-full-access"\n'
        'approval_policy = "never"\n\n'
        '[sandbox_workspace_write]\n'
        'network_access = true\n'
        'writable_roots = ["/etc"]\n\n'
        '[hooks.state]\n\n'
        '[hooks.state."stale-hook"]\n'
        'trusted_hash = "sha256:stale"\n\n'
        '[projects."/tmp"]\n'
        'trust_level = "trusted"\n'
    )

    rendered = render_codex.render_codex_config_text(
        source,
        {},
        permissions,
        settings,
        render_codex.render_hooks({}),
        [],
    )
    parsed = tomllib.loads(rendered)

    assert parsed["default_permissions"] == "repos_dev"
    assert parsed["approval_policy"] == "never"
    assert "sandbox_mode" not in parsed
    assert "approvals_reviewer" not in parsed
    assert "sandbox_workspace_write" not in parsed
    assert "permissions" not in parsed or "repos_dev" not in parsed["permissions"]
    assert "stale-hook" not in parsed["hooks"]["state"]
    assert "sandbox_mode" not in parsed["projects"]["/tmp"]
    assert rendered.count("default_permissions =") == 1
    assert rendered.count("approval_policy =") == 1


def test_empty_hook_matcher_hashes_like_codex_absent_matcher() -> None:
    absent = render_codex.render_hooks(
        {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]}
    )
    empty = render_codex.render_hooks(
        {"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "true"}]}]}
    )

    assert render_codex.codex_hook_states(absent)[0][1] == (
        render_codex.codex_hook_states(empty)[0][1]
    )


def test_ignored_user_prompt_matcher_hashes_like_codex_absent_matcher() -> None:
    absent = render_codex.render_hooks(
        {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]}
    )
    wildcard = render_codex.render_hooks(
        {"UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "true"}]}]}
    )

    assert render_codex.codex_hook_states(absent)[0][1] == (
        render_codex.codex_hook_states(wildcard)[0][1]
    )


def test_claude_bash_ask_rule_cannot_enable_codex_escalation() -> None:
    permissions, settings = claude_runtime()
    permissions["ask"] = ["Bash(git push *)"]

    block = "\n".join(render_codex.codex_permissions_block(permissions, settings, {}))

    assert 'approval_policy = "never"' in block
    assert "approvals_reviewer" not in block


def test_claude_sandbox_setting_does_not_replace_codex_profile() -> None:
    permissions, settings = claude_runtime()
    settings["sandbox"] = {"enabled": True}

    block = "\n".join(render_codex.codex_permissions_block(permissions, settings, {}))

    assert 'default_permissions = "repos_dev"' in block
    assert 'approval_policy = "never"' in block
    assert "sandbox_mode" not in block


def test_execpolicy_maps_only_literal_prefix_denies_and_reports_the_rest() -> None:
    permissions, _ = claude_runtime()

    rules, unsupported = render_codex.codex_bash_deny_rules(permissions)
    text = "\n".join(rules)

    assert 'pattern=["sudo"]' in text
    assert 'pattern=["rm", "-rf"]' in text
    assert 'pattern=["git", "reset", "--hard"]' in text
    assert 'pattern=["gh", "pr", "merge"]' in text
    assert all('decision="forbidden"' in rule for rule in rules)
    assert unsupported == ["Bash(*quarantine*)"]


def test_execpolicy_never_expands_privilege_for_git_metadata_writes() -> None:
    permissions, _ = claude_runtime()

    text = render_codex.render_execpolicy_text("", permissions)

    assert 'pattern=["git", "add"], decision="allow"' not in text
    assert 'pattern=["git", "commit"], decision="allow"' not in text
    assert 'pattern=["git"], decision="allow"' not in text


def test_generated_execpolicy_block_strips_allow_and_is_idempotent() -> None:
    permissions, _ = claude_runtime()
    user = (
        'prefix_rule(pattern=["python3"], decision="allow")\n'
        'prefix_rule(pattern=["python3", "-m"], decision="prompt")\n'
    )

    once = render_codex.render_execpolicy_text(user, permissions)
    twice = render_codex.render_execpolicy_text(once, permissions)

    assert 'decision="allow"' not in once
    assert 'pattern=["python3", "-m"], decision="prompt"' in once
    assert once == twice
    assert once.count(render_codex.EXECPOLICY_START) == 1
    assert once.count(render_codex.EXECPOLICY_END) == 1

    generated_only = render_codex.render_execpolicy_text("", permissions)
    assert render_codex.render_execpolicy_text(generated_only, permissions) == generated_only


def test_execpolicy_strips_allow_for_alternatives_variable_and_missing_decision() -> None:
    permissions, _ = claude_runtime()
    existing = (
        'commands = ["python3"]\n'
        'prefix_rule(pattern=[["nix", "develop"], ["nix-shell"]], decision="allow")\n'
        'prefix_rule(pattern=commands, decision="allow")\n'
        'prefix_rule(pattern=["xdg-open"])\n'
    )

    once = render_codex.render_execpolicy_text(existing, permissions)
    twice = render_codex.render_execpolicy_text(once, permissions)

    assert 'commands = ["python3"]' in once
    assert "prefix_rule(pattern=[[" not in once
    assert "prefix_rule(pattern=commands" not in once
    assert 'prefix_rule(pattern=["xdg-open"])' not in once
    assert once == twice


def test_execpolicy_rejects_nonliteral_decisions() -> None:
    permissions, _ = claude_runtime()
    for decision in ("choice", "choose()", 'f"{choice}"'):
        existing = f'prefix_rule(pattern=["python3"], decision={decision})\n'

        try:
            render_codex.render_execpolicy_text(existing, permissions)
        except ValueError as exc:
            assert "literal string" in str(exc)
        else:
            raise AssertionError(f"accepted nonliteral decision {decision}")


def test_execpolicy_rejects_unknown_literal_decision() -> None:
    permissions, _ = claude_runtime()

    try:
        render_codex.render_execpolicy_text(
            'prefix_rule(pattern=["python3"], decision="audit")\n', permissions
        )
    except ValueError as exc:
        assert "allow, prompt, or forbidden" in str(exc)
    else:
        raise AssertionError("accepted unknown literal prefix_rule decision")


def test_execpolicy_classifies_positional_decisions() -> None:
    source = (
        'prefix_rule(["allow-positional"], "allow")\n'
        'prefix_rule(["missing-positional"])\n'
        'prefix_rule(["prompt-positional"], "prompt")\n'
        'prefix_rule(["forbidden-positional"], "forbidden")\n'
    )

    actual = render_codex.strip_unmanaged_allow_rules(source)

    assert '"allow-positional"' not in actual
    assert '"missing-positional"' not in actual
    assert 'prefix_rule(["prompt-positional"], "prompt")' in actual
    assert 'prefix_rule(["forbidden-positional"], "forbidden")' in actual


def test_execpolicy_rejects_invalid_positional_decisions() -> None:
    cases = (
        ('prefix_rule(["python3"], decision_name)\n', "literal string"),
        ('prefix_rule(["python3"], "audit")\n', "allow, prompt, or forbidden"),
        (
            'prefix_rule(["python3"], "prompt", decision="forbidden")\n',
            "both positionally and by keyword",
        ),
        (
            'prefix_rule(["python3"], decision="prompt", decision="forbidden")\n',
            "at most once",
        ),
    )
    for source, expected_error in cases:
        try:
            render_codex.strip_unmanaged_allow_rules(source)
        except ValueError as exc:
            assert expected_error in str(exc)
        else:
            raise AssertionError(f"accepted invalid positional decision: {source}")


def test_execpolicy_rejects_starred_positional_arguments() -> None:
    try:
        render_codex.strip_unmanaged_allow_rules(
            'prefix_rule(*rule_arguments)\n'
        )
    except ValueError as exc:
        assert "starred positional" in str(exc)
    else:
        raise AssertionError("accepted dynamic positional prefix_rule arguments")


def test_execpolicy_rejects_prefix_rule_kwargs() -> None:
    permissions, _ = claude_runtime()

    try:
        render_codex.render_execpolicy_text(
            'prefix_rule(pattern=["python3"], **options)\n', permissions
        )
    except ValueError as exc:
        assert "**kwargs" in str(exc)
    else:
        raise AssertionError("accepted dynamic prefix_rule kwargs")


def test_execpolicy_line_deletion_preserves_unicode_crlf() -> None:
    source = (
        'prefix_rule(pattern=["python3"], decision="allow", '
        'justification="räksmörgås")  # behåll kommentaren\r\n'
        'prefix_rule(pattern=["python3", "safe"], decision="prompt", '
        'justification="förbjud inte")\r\n'
    )

    actual = render_codex.strip_unmanaged_allow_rules(source)

    assert actual == (
        'prefix_rule(pattern=["python3", "safe"], decision="prompt", '
        'justification="förbjud inte")\r\n'
    )


def test_execpolicy_strips_multiline_and_preserves_prompt_forbidden() -> None:
    source = """prefix_rule(
    pattern=["python3"],
    decision="allow",
    justification="runtime approval",
)
prefix_rule(pattern=["git", "status"], decision="prompt")
prefix_rule(pattern=["gh", "pr", "merge"], decision="forbidden")
"""

    actual = render_codex.strip_unmanaged_allow_rules(source)

    assert 'decision="allow"' not in actual
    assert 'pattern=["git", "status"], decision="prompt"' in actual
    assert 'pattern=["gh", "pr", "merge"], decision="forbidden"' in actual


def test_execpolicy_strips_live_shaped_runtime_allows() -> None:
    permissions, _ = claude_runtime()
    existing = """# Codex runtime approvals
prefix_rule(pattern=["nix", "develop"], decision="allow")
prefix_rule(pattern=["zsh", "-lc"], decision="allow")
prefix_rule(pattern=["python3"], decision="allow")
prefix_rule(pattern=["xdg-open"], decision="allow")
"""

    rendered = render_codex.render_execpolicy_text(existing, permissions)

    for program in ("nix", "zsh", "python3", "xdg-open"):
        assert f'pattern=["{program}"' not in rendered


def test_execpolicy_strips_persisted_network_allows() -> None:
    # Codex persists an approved host as a network_rule with decision "allow";
    # like an allow prefix_rule, it must not survive a sync.
    source = (
        "# Codex runtime approvals\n"
        'network_rule(host="api.example.com", protocol="https", decision="allow", '
        'justification="approved once")\n'
        'network_rule("positional.example.com", "https", "allow")\n'
        "network_rule(\n"
        '    host="multi.example.com",\n'
        '    protocol="https",\n'
        '    decision="allow",\n'
        ")\n"
        'network_rule(host="deny.example.com", protocol="https", decision="deny")\n'
        'network_rule(host="ask.example.com", protocol="https", decision="prompt")\n'
        'host_executable(name="git", paths=["/usr/bin/git"])\n'
    )

    actual = render_codex.strip_unmanaged_allow_rules(source)

    assert actual == (
        "# Codex runtime approvals\n"
        'network_rule(host="deny.example.com", protocol="https", decision="deny")\n'
        'network_rule(host="ask.example.com", protocol="https", decision="prompt")\n'
        'host_executable(name="git", paths=["/usr/bin/git"])\n'
    )


def test_execpolicy_rejects_unreadable_network_rule_decisions() -> None:
    cases = (
        ('network_rule(host="x.org", protocol="https", decision=D)\n', "literal string"),
        ('network_rule(host="x.org", protocol="https")\n', "must declare a decision"),
        (
            'network_rule(host="x.org", protocol="https", decision="audit")\n',
            "allow, deny, forbidden, or prompt",
        ),
        ('network_rule(*arguments)\n', "starred positional"),
        ('network_rule(host="x.org", **options)\n', "**kwargs"),
    )
    for source, expected_error in cases:
        try:
            render_codex.strip_unmanaged_allow_rules(source)
        except ValueError as exc:
            assert expected_error in str(exc), (source, exc)
            assert "network_rule" in str(exc), (source, exc)
        else:
            raise AssertionError(f"accepted unreadable network_rule: {source}")


def test_execpolicy_rejects_allow_rules_sharing_a_line() -> None:
    sources = (
        'prefix_rule(pattern=["git"], decision="allow"); '
        'prefix_rule(pattern=["safe-after"], decision="prompt")\n',
        'prefix_rule(pattern=["safe-before"], decision="prompt"); '
        'prefix_rule(pattern=["python3"], decision="allow")\n',
    )

    for source in sources:
        try:
            render_codex.strip_unmanaged_allow_rules(source)
        except ValueError as exc:
            assert str(exc) == "allow prefix_rule must occupy its own lines"
        else:
            raise AssertionError("accepted allow prefix_rule sharing a line")


if __name__ == "__main__":
    test_codex_does_not_inherit_claudes_disabled_sandbox_or_blanket_allow()
    test_first_render_keeps_security_settings_at_toml_root()
    test_empty_hook_matcher_hashes_like_codex_absent_matcher()
    test_ignored_user_prompt_matcher_hashes_like_codex_absent_matcher()
    test_claude_bash_ask_rule_cannot_enable_codex_escalation()
    test_claude_sandbox_setting_does_not_replace_codex_profile()
    test_execpolicy_maps_only_literal_prefix_denies_and_reports_the_rest()
    test_execpolicy_never_expands_privilege_for_git_metadata_writes()
    test_generated_execpolicy_block_strips_allow_and_is_idempotent()
    test_execpolicy_strips_allow_for_alternatives_variable_and_missing_decision()
    test_execpolicy_rejects_nonliteral_decisions()
    test_execpolicy_rejects_unknown_literal_decision()
    test_execpolicy_classifies_positional_decisions()
    test_execpolicy_rejects_invalid_positional_decisions()
    test_execpolicy_rejects_starred_positional_arguments()
    test_execpolicy_rejects_prefix_rule_kwargs()
    test_execpolicy_line_deletion_preserves_unicode_crlf()
    test_execpolicy_strips_multiline_and_preserves_prompt_forbidden()
    test_execpolicy_strips_live_shaped_runtime_allows()
    test_execpolicy_strips_persisted_network_allows()
    test_execpolicy_rejects_unreadable_network_rule_decisions()
    test_execpolicy_rejects_allow_rules_sharing_a_line()
    print("codex Bash permission rendering tests OK")

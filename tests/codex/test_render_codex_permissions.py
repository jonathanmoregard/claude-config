#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


RENDERER = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(RENDERER / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_codex  # noqa: E402
from roundtrip_support import (  # noqa: E402
    PER_TOOL_ALLOWS,
    Inputs,
    assert_check_fails,
    describe,
    round_trip,
)


def test_permission_path_normalizes_claude_syntax() -> None:
    assert render_codex.permission_path("Edit(//home/x/**)", "Edit") == "/home/x"
    assert render_codex.permission_path("Edit(~/x/**)", "Edit") == str(Path.home() / "x")
    assert render_codex.permission_path("Read(//home/x/**)", "Edit") is None

    with patch.object(render_codex, "CLAUDE", Path("/claude")):
        assert render_codex.permission_path("Edit(/skills/**)", "Edit") == "/claude/skills"


def test_paths_are_canonicalized_before_deny_matching() -> None:
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["//safe/../vault"],
        "deny": ["Edit(//vault/**)"],
    }
    roots = render_codex.codex_writable_roots(permissions)
    assert "/vault" not in roots
    assert "/safe/../vault" not in roots

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault = root / "vault"
        alias = root / "alias"
        vault.mkdir()
        alias.symlink_to(vault, target_is_directory=True)
        permissions = {
            "defaultMode": "acceptEdits",
            "additionalDirectories": [str(alias)],
            "deny": [f"Read(/{vault}/**)"],
        }
        roots = render_codex.codex_writable_roots(permissions)
        assert str(vault) not in roots
        assert str(alias) not in roots


def test_symlink_allow_requires_independent_target_authorization() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault = root / "vault"
        alias = root / "alias"
        vault.mkdir()
        alias.symlink_to(vault, target_is_directory=True)

        alias_rule = f"Edit(/{alias}/**)"
        target_rule = f"Edit(/{vault}/**)"
        roots = render_codex.codex_writable_roots({"allow": [alias_rule]})
        assert str(alias) not in roots
        assert str(vault) not in roots

        roots = render_codex.codex_writable_roots({"allow": [alias_rule, target_rule]})
        assert str(alias) in roots
        assert str(vault) in roots


def test_symlink_deny_checks_lexical_path_and_resolved_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault = root / "vault"
        alias = root / "alias"
        vault.mkdir()
        alias.symlink_to(vault, target_is_directory=True)

        assert render_codex.permission_path(f"Edit(/{alias}/**)", "Edit") == str(alias)
        assert render_codex.edit_denied(
            str(alias), {"deny": [f"Edit(/{alias}/**)"]}
        )
        assert render_codex.edit_denied(
            str(alias), {"deny": [f"Read(/{vault}/**)"]}
        )


def test_writable_roots_match_claude_edit_rights() -> None:
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["//allowed-extra", "//denied-extra", "//denied-extra/child"],
        "allow": [
            "Edit(//allowed-explicit/**)",
            f"Edit(//{str(Path.home()).lstrip('/')}/.claude/skills/**)",
            "Read(//read-only/**)",
        ],
        "deny": ["Edit(//denied-extra/**)"],
    }
    with (
        patch.object(render_codex, "AGENTS_HOME", Path("/agents-home")),
        patch.object(render_codex, "CODEX", Path("/codex")),
        patch.object(render_codex, "PLUGIN_ROOT", Path("/plugins/claude-mirror")),
        patch.object(render_codex, "AI_ROUTER_STATE", Path("/state/ai-router")),
        patch.object(render_codex, "REPO", Path("/migration-repo")),
    ):
        roots = render_codex.codex_writable_roots(permissions)

    assert roots == [
        "/allowed-explicit",
        "/allowed-extra",
        "/state/ai-router",
    ]
    assert str(Path.home() / ".claude" / "skills") not in roots


def test_worktree_access_does_not_expose_every_repository() -> None:
    home = Path.home()
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": [
            str(home / "worktrees"),
            str(home / "Repos" / "nixos-config-worktrees"),
        ],
        "allow": ["Bash"],
    }

    roots = render_codex.codex_writable_roots(permissions)

    assert str(home / "Repos") not in roots


def test_disabled_claude_sandbox_cannot_disable_codex_sandbox() -> None:
    assert (
        render_codex.codex_sandbox_mode({"sandbox": {"enabled": False}})
        == "workspace-write"
    )
    assert render_codex.codex_sandbox_mode({}) == "workspace-write"


def test_claude_web_deny_and_effort_are_rendered_natively() -> None:
    rendered = render_codex.render_codex_config_text(
        'model_reasoning_effort = "high"\nweb_search = "live"\n',
        {},
        {"deny": ["WebFetch", "WebSearch"]},
        {"effortLevel": "xhigh"},
        render_codex.render_hooks({}),
        [],
    )

    assert 'model_reasoning_effort = "xhigh"' in rendered
    assert 'web_search = "disabled"' in rendered
    assert rendered.count("model_reasoning_effort =") == 1
    assert rendered.count("web_search =") == 1


def test_claude_privacy_opt_outs_render_as_codex_analytics_and_feedback() -> None:
    rendered = render_codex.render_codex_config_text(
        "[analytics]\nenabled = true\n\n[feedback]\nenabled = true\n",
        {},
        {},
        {
            "env": {
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
            }
        },
        render_codex.render_hooks({}),
        [],
    )
    parsed = __import__("tomllib").loads(rendered)

    assert parsed["analytics"]["enabled"] is False
    assert parsed["feedback"]["enabled"] is False
    assert rendered.count("[analytics]") == 1
    assert rendered.count("[feedback]") == 1


def test_claude_privacy_mapping_is_idempotent() -> None:
    settings = {
        "env": {
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        }
    }
    first = render_codex.render_codex_config_text(
        "", {}, {}, settings, render_codex.render_hooks({}), []
    )

    second = render_codex.render_codex_config_text(
        first, {}, {}, settings, render_codex.render_hooks({}), []
    )

    assert second == first


def test_wildcard_edit_denies_exclude_matching_candidate_roots() -> None:
    for deny in ("Edit(//**/quarantine/**)", "Edit(//**/quarantine)"):
        permissions = {
            "defaultMode": "acceptEdits",
            "additionalDirectories": ["//home/x/quarantine", "//home/x/safe"],
            "deny": [deny],
        }
        roots = render_codex.codex_writable_roots(permissions)

        assert "/home/x/quarantine" not in roots, deny
        assert "/home/x/safe" in roots, deny

    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["//home/x/quarantine", "//home/x/y/quarantine"],
        "deny": ["Edit(//home/*/quarantine/**)"],
    }
    roots = render_codex.codex_writable_roots(permissions)

    assert "/home/x/quarantine" not in roots
    assert "/home/x/y/quarantine" in roots


def test_read_denies_also_exclude_writable_roots() -> None:
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["//vault"],
        "deny": ["Read(//vault/**)"],
    }
    assert "/vault" not in render_codex.codex_writable_roots(permissions)


def test_deny_globs_are_segment_safe_and_relative_denies_match_at_any_depth() -> None:
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": [
            "//home/x/secrets",
            "//home/xy/secrets",
            "//home/a/cache",
            "//home/b/cache",
            "//home/c/cache",
            "//work/project/src",
        ],
        "deny": [
            "Edit(//home/?/secrets/**)",
            "Read(//home/[ab]/cache/**)",
            "Edit(src/**)",
        ],
    }
    roots = render_codex.codex_writable_roots(permissions)

    assert "/home/x/secrets" not in roots
    assert "/home/xy/secrets" in roots
    assert "/home/a/cache" not in roots
    assert "/home/b/cache" not in roots
    assert "/home/c/cache" in roots
    assert "/work/project/src" not in roots


def test_relative_allow_and_additional_paths_do_not_become_global_roots() -> None:
    permissions = {
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["extras", "../shared"],
        "allow": ["Edit(src/**)"],
    }
    with (
        patch.object(render_codex, "AGENTS_HOME", Path("/agents-home")),
        patch.object(render_codex, "CODEX", Path("/codex")),
        patch.object(render_codex, "PLUGIN_ROOT", Path("/plugins/claude-mirror")),
        patch.object(render_codex, "AI_ROUTER_STATE", Path("/state/ai-router")),
        patch.object(render_codex, "REPO", Path("/migration-repo")),
    ):
        roots = render_codex.codex_writable_roots(permissions)

    assert roots == [
        "/state/ai-router",
    ]


def test_additional_directories_require_accept_edits_mode() -> None:
    # arftl-allow: permission-widened (each mode must still be refused extra roots)
    for mode in ("default", "plan", "dontAsk", "bypassPermissions"):
        roots = render_codex.codex_writable_roots(
            {"defaultMode": mode, "additionalDirectories": ["//mode-only"]}
        )
        assert "/mode-only" not in roots, mode

    roots = render_codex.codex_writable_roots(
        {"defaultMode": "acceptEdits", "additionalDirectories": ["//mode-only"]}
    )
    assert "/mode-only" in roots


def test_check_accepts_round_tripped_output() -> None:
    with round_trip() as rt:
        result = rt.check()
        config = rt.load_config()

    assert result.returncode == 0, describe(result)
    # The managed profile stays out of user config; Codex requirements own it.
    assert config["default_permissions"] == render_codex.CODEX_PERMISSION_PROFILE
    assert render_codex.CODEX_PERMISSION_PROFILE not in config.get("permissions", {})


def test_check_rejects_user_definition_of_managed_profile() -> None:
    with round_trip() as rt:
        rt.edit_config(
            lambda config: config.setdefault("permissions", {}).update(
                repos_dev={"extends": ":workspace"}
            )
        )
        assert_check_fails(rt, "repos_dev must be absent from user config")


def test_check_rejects_missing_generated_execpolicy() -> None:
    with round_trip() as rt:
        rules = rt.codex / "rules" / "default.rules"
        rules.unlink()
        assert_check_fails(rt, f"{rules}: missing")


def test_check_rejects_runtime_allow_execpolicy() -> None:
    with round_trip() as rt:
        rules = rt.codex / "rules" / "default.rules"
        rules.write_text(
            'prefix_rule(pattern=["python3"], decision="allow")\n'
            + rules.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        assert_check_fails(rt, "persisted allow prefix_rule")


def set_server_key(server: str, key: str, value: object):
    def edit(config: dict[str, object]) -> None:
        config["mcp_servers"][server][key] = value

    return edit


def rename_server(server: str):
    def edit(config: dict[str, object]) -> None:
        servers = config["mcp_servers"]
        servers[f"stale_{server}"] = servers.pop(server)

    return edit


def test_check_rejects_stale_git_broker_config_and_assets() -> None:
    connector = render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID

    def enable_connector(config: dict[str, object]) -> None:
        config["apps"][connector]["enabled"] = True

    cases = [
        (
            "broker not required",
            lambda rt: rt.edit_config(set_server_key("git_broker", "required", False)),
            "mcp_servers.git_broker must be required",
        ),
        (
            "broker asset",
            lambda rt: (rt.codex / "mcps" / "git-broker" / "server.py").write_text(
                "# stale broker\n", encoding="utf-8"
            ),
            "git-broker/server.py: MCP asset differs from its source",
        ),
        (
            "github renamed",
            lambda rt: rt.edit_config(rename_server("github")),
            "mcp_servers.github",
        ),
        (
            "github child renamed",
            lambda rt: rt.edit_config(rename_server("github_acme")),
            "mcp_servers.github_acme",
        ),
        (
            "connector enabled",
            lambda rt: rt.edit_config(enable_connector),
            "built-in GitHub connector must be disabled",
        ),
        (
            "plugin MCP inventory",
            lambda rt: (rt.plugin / ".mcp.json").write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8"
            ),
            ".mcp.json: differs from the render plan",
        ),
    ]
    for label, mutate, expected_error in cases:
        with round_trip() as rt:
            mutate(rt)
            assert_check_fails(rt, expected_error, label=label)


APPROVE = {"approval_mode": "approve"}


def test_check_accepts_per_tool_approvals_from_claude_allow_rules() -> None:
    # A Claude `mcp__<server>__<tool>` allow renders as a per-tool
    # approval_mode table. The check must expect it, not call it stale.
    with round_trip(Inputs(per_tool_allows=True, optional_broker=True)) as rt:
        result = rt.check()
        servers = rt.load_config()["mcp_servers"]

    # The round trip really rendered the approvals, so acceptance is not vacuous.
    for server, tool in PER_TOOL_ALLOWS.items():
        assert servers[server]["tools"][tool] == APPROVE, server
    assert result.returncode == 0, describe(result)


def test_check_rejects_missing_per_tool_approvals() -> None:
    for server, tool in PER_TOOL_ALLOWS.items():
        with round_trip(Inputs(per_tool_allows=True, optional_broker=True)) as rt:

            def drop(config: dict[str, object]) -> None:
                tools = config["mcp_servers"][server]["tools"]
                assert tools.pop(tool) == APPROVE

            rt.edit_config(drop)
            assert_check_fails(
                rt, f"mcp_servers.{server}.tools.{tool}", label=server
            )


def test_check_rejects_leftover_per_tool_approvals() -> None:
    # An approval whose Claude allow rule is gone must not survive the check.
    for server in PER_TOOL_ALLOWS:
        with round_trip(Inputs(per_tool_allows=False, optional_broker=True)) as rt:

            def add(config: dict[str, object]) -> None:
                entry = config["mcp_servers"][server]
                assert "tools" not in entry
                entry["tools"] = {"leftover": dict(APPROVE)}

            rt.edit_config(add)
            assert_check_fails(rt, f"mcp_servers.{server}.tools", label=server)


def test_check_rejects_unsafe_sandbox_settings() -> None:
    cases = [
        ({"sandbox_mode": "workspace-write"}, "legacy sandbox_mode must be absent"),
        ({"approval_policy": "on-request"}, "approval_policy must be never"),
        ({"default_permissions": "other"}, "default_permissions must be repos_dev"),
        (
            # arftl-allow: permission-widened (a config the check must reject)
            {"permissions": {"repos_dev": {"extends": ":workspace"}}},
            "repos_dev must be absent from user config",
        ),
    ]
    for overrides, expected_error in cases:
        with round_trip() as rt:
            rt.edit_config(lambda config: config.update(overrides))
            assert_check_fails(rt, expected_error, label=str(overrides))


def test_renderer_location_is_never_a_writable_root() -> None:
    """The sync unit renders from a throwaway clone bind-mounted at
    /tmp/ai-client-config-repo; baking that path into writable_roots made
    bwrap fail on every sandboxed Codex command once the mount was gone
    (2026-09-14). The renderer's own location is not a permission source."""
    with tempfile.TemporaryDirectory(prefix="ai-client-config-repo-") as td:
        sandbox_repo = Path(td)
        with patch.object(render_codex, "REPO", sandbox_repo):
            roots = render_codex.codex_writable_roots({"defaultMode": "acceptEdits"})

    assert str(sandbox_repo) not in roots
    assert not any(root.startswith(str(sandbox_repo)) for root in roots)


def test_ai_router_state_dir_is_a_writable_root() -> None:
    """A Codex host delegating through ~/.claude/bin/ai-router writes job
    state under ~/.local/state/ai-router from inside the workspace-write
    sandbox; without this root only danger-full-access hosts can delegate."""
    roots = render_codex.codex_writable_roots({})

    assert str(Path.home() / ".local" / "state" / "ai-router") in roots


def test_generated_codex_state_is_not_writable_to_sandboxed_commands() -> None:
    roots = render_codex.codex_writable_roots({})

    assert str(render_codex.CODEX) not in roots
    assert str(render_codex.CODEX / "memories") not in roots
    assert str(render_codex.AGENTS_HOME) not in roots
    assert str(render_codex.PLUGIN_ROOT) not in roots


if __name__ == "__main__":
    test_permission_path_normalizes_claude_syntax()
    test_paths_are_canonicalized_before_deny_matching()
    test_symlink_allow_requires_independent_target_authorization()
    test_symlink_deny_checks_lexical_path_and_resolved_target()
    test_writable_roots_match_claude_edit_rights()
    test_worktree_access_does_not_expose_every_repository()
    test_disabled_claude_sandbox_cannot_disable_codex_sandbox()
    test_claude_web_deny_and_effort_are_rendered_natively()
    test_claude_privacy_opt_outs_render_as_codex_analytics_and_feedback()
    test_claude_privacy_mapping_is_idempotent()
    test_wildcard_edit_denies_exclude_matching_candidate_roots()
    test_read_denies_also_exclude_writable_roots()
    test_deny_globs_are_segment_safe_and_relative_denies_match_at_any_depth()
    test_relative_allow_and_additional_paths_do_not_become_global_roots()
    test_additional_directories_require_accept_edits_mode()
    test_check_rejects_user_definition_of_managed_profile()
    test_check_accepts_round_tripped_output()
    test_check_rejects_missing_generated_execpolicy()
    test_check_rejects_runtime_allow_execpolicy()
    test_check_rejects_stale_git_broker_config_and_assets()
    test_check_accepts_per_tool_approvals_from_claude_allow_rules()
    test_check_rejects_missing_per_tool_approvals()
    test_check_rejects_leftover_per_tool_approvals()
    test_renderer_location_is_never_a_writable_root()
    test_ai_router_state_dir_is_a_writable_root()
    test_generated_codex_state_is_not_writable_to_sandboxed_commands()
    test_check_rejects_unsafe_sandbox_settings()
    print("codex permission rendering tests OK")

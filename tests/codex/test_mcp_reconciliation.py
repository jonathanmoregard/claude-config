#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import tomllib
from unittest.mock import patch

import pytest

RENDERER = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(RENDERER / "scripts"))

import import_claude  # noqa: E402
import render_codex  # noqa: E402
from mcp_test_support import (  # noqa: E402
    GITHUB_EXCLUSIONS,
    shared_broker_registrations,
)


def test_import_prefers_dedicated_mcp_source_and_omits_fully_denied_server() -> None:
    settings = {
        # arftl-allow: permission-widened (fixture: deny rules the import must honour)
        "permissions": {
            "deny": [
                "mcp__futuresearch",
                "mcp__futuresearch__*",
            ]
        }
    }
    state = {
        "futuresearch": {"type": "http", "url": "https://disabled.invalid"},
        "aggregator": {"command": "/stale/checkout/aggregator-mcp"},
    }
    dedicated = {
        "aggregator": {"command": "/run/current-system/sw/bin/aggregator-mcp"},
        "research-agent": {"command": "research-agent-mcp"},
    }

    actual = import_claude.mcp_inventory(settings, state, dedicated)

    assert actual == {
        "aggregator": {"command": "/run/current-system/sw/bin/aggregator-mcp"},
        "research-agent": {"command": "research-agent-mcp"},
    }


def test_render_reconciles_unmanaged_codex_mcp_tables_to_claude_source() -> None:
    existing = (
        'approvals_reviewer = "auto_review"\n\n'
        '# Manual local test server; not part of Claude.\n'
        '[mcp_servers.beeper-privacy]\n'
        'url = "http://127.0.0.1:3000"\n\n'
        '[mcp_servers.old.env]\n'
        'TOKEN = "stale"\n\n'
        '[features]\n'
        'hooks = true\n'
    )

    rendered = render_codex.render_codex_config_text(
        existing,
        {"aggregator": {"command": "/source/bin/aggregator-mcp"}},
        {},
        {},
        render_codex.render_hooks({}),
        [],
    )

    assert 'default_permissions = "repos_dev"' in rendered
    assert 'approval_policy = "never"' in rendered
    assert "approvals_reviewer" not in rendered
    assert '[features]' in rendered
    assert "beeper-privacy" not in rendered
    assert "mcp_servers.old" not in rendered
    assert '[mcp_servers.aggregator]' in rendered


def test_render_disables_builtin_github_connector_and_preserves_other_apps() -> None:
    existing = (
        '[apps.runtime_connector]\n'
        'enabled = true\n\n'
        '[apps.runtime_connector.settings]\n'
        'mode = "keep"\n\n'
        f'[apps.{render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID}]\n'
        'enabled = true\n'
    )

    once = render_codex.render_codex_config_text(
        existing, {}, {}, {}, render_codex.render_hooks({}), []
    )
    twice = render_codex.render_codex_config_text(
        once, {}, {}, {}, render_codex.render_hooks({}), []
    )
    parsed = tomllib.loads(once)

    assert once == twice
    assert parsed["apps"][render_codex.BUILTIN_GITHUB_CONNECTOR_APP_ID] == {
        "enabled": False
    }
    assert parsed["apps"]["runtime_connector"] == {
        "enabled": True,
        "settings": {"mode": "keep"},
    }


def test_render_preserves_mirrored_aggregator_command() -> None:
    table = render_codex.codex_mcp_table(
        "aggregator",
        {"command": "/etc/profiles/per-user/jonathan/bin/aggregator-mcp"},
    )

    assert 'command = "/etc/profiles/per-user/jonathan/bin/aggregator-mcp"' in table
    assert not any("Repos/aggregator" in line for line in table)


def test_render_maps_claude_timeout_milliseconds_to_codex_tool_seconds() -> None:
    table = render_codex.codex_mcp_table(
        "research-agent",
        {"command": "research-agent-mcp", "timeout": 6_000_000},
    )

    assert "tool_timeout_sec = 6000" in table

    for value in (True, 0, -1, "6000"):
        try:
            render_codex.codex_mcp_table(
                "research-agent", {"command": "research-agent-mcp", "timeout": value}
            )
        except ValueError as exc:
            assert "timeout" in str(exc)
        else:
            raise AssertionError(f"accepted invalid timeout {value!r}")


def test_render_auto_approves_mcp_server_wildcard_allowed_by_claude() -> None:
    rendered = render_codex.render_codex_config_text(
        "",
        {"research-agent": {"command": "research-agent-mcp"}},
        {"allow": ["mcp__research-agent__*"]},
        {},
        render_codex.render_hooks({}),
        [],
    )

    config = tomllib.loads(rendered)
    assert (
        config["mcp_servers"]["research-agent"]["default_tools_approval_mode"]
        == "approve"
    )


def test_render_does_not_auto_approve_mcp_server_with_higher_priority_rule() -> None:
    for priority in ("ask", "deny"):
        rendered = render_codex.render_codex_config_text(
            "",
            {"research-agent": {"command": "research-agent-mcp"}},
            {
                "allow": ["mcp__research-agent__*"],
                priority: ["mcp__research-agent__research"],
            },
            {},
            render_codex.render_hooks({}),
            [],
        )

        config = tomllib.loads(rendered)
        assert (
            "default_tools_approval_mode"
            not in config["mcp_servers"]["research-agent"]
        )


def test_render_approves_only_the_exact_mcp_tools_allowed_by_claude() -> None:
    rendered = render_codex.render_codex_config_text(
        "",
        {"home-assistant": {"command": "ha-mcp"}},
        {
            "allow": [
                "mcp__home-assistant__ha_config_set_helper",
                "mcp__home-assistant__ha_call_service",
                "mcp__home-assistant__ha_restart",
            ],
            "ask": ["mcp__home-assistant__ha_restart"],
        },
        {},
        render_codex.render_hooks({}),
        [],
    )

    server = tomllib.loads(rendered)["mcp_servers"]["home-assistant"]
    assert "default_tools_approval_mode" not in server
    assert server["tools"] == {
        "ha_call_service": {"approval_mode": "approve"},
        "ha_config_set_helper": {"approval_mode": "approve"},
    }


def test_render_server_wide_ask_or_deny_blocks_per_tool_approval() -> None:
    for priority in ("ask", "deny"):
        for rule in ("mcp__home-assistant", "mcp__home-assistant__*"):
            rendered = render_codex.render_codex_config_text(
                "",
                {"home-assistant": {"command": "ha-mcp"}},
                {
                    "allow": ["mcp__home-assistant__ha_config_set_helper"],
                    priority: [rule],
                },
                {},
                render_codex.render_hooks({}),
                [],
            )

            server = tomllib.loads(rendered)["mcp_servers"]["home-assistant"]
            assert "tools" not in server
            assert "default_tools_approval_mode" not in server


def test_render_ignores_per_tool_rules_for_other_servers() -> None:
    rendered = render_codex.render_codex_config_text(
        "",
        {
            "home-assistant": {"command": "ha-mcp"},
            "home": {"command": "home-mcp"},
        },
        {"allow": ["mcp__home-assistant__ha_call_service"]},
        {},
        render_codex.render_hooks({}),
        [],
    )

    servers = tomllib.loads(rendered)["mcp_servers"]
    assert "tools" not in servers["home"]
    assert set(servers["home-assistant"]["tools"]) == {"ha_call_service"}


def write_git_broker_source(claude: Path) -> Path:
    source = claude / "mcps" / "git-broker"
    source.mkdir(parents=True)
    (source / "server.py").write_text("# structured broker\n", encoding="utf-8")
    (source / "config.json").write_text(
        '{"gitBroker": {"repositoryRoots": ["/repos"]}}\n', encoding="utf-8"
    )
    return source


def test_codex_overlay_preserves_claude_command_args_and_env(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    write_git_broker_source(claude)
    registrations = shared_broker_registrations(
        claude, git_env={"BASE_ENV": "git", "DCG_PATH": "/custom/dcg"}
    )

    rendered, _ = render_codex.prepare_mcp_assets(
        registrations, claude_root=claude, codex_root=codex
    )

    assert rendered["git_broker"]["command"] == "/run/current-system/sw/bin/python3"
    assert rendered["git_broker"]["env"]["BASE_ENV"] == "git"
    assert rendered["git_broker"]["env"]["DCG_PATH"] == "/custom/dcg"
    # Codex's GitHub children come from the mux config, not from Claude's own
    # `github` registration, which runs a multiplexer Codex cannot start.
    assert rendered["github"]["command"] == "github-app-mcp-server"
    assert rendered["github"]["args"][0] == "--toolsets"
    assert rendered["github_acme"]["args"][:2] == ["--owner", "acme"]


def test_codex_overlay_rewrites_the_git_broker_server_arg(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    source = write_git_broker_source(claude)
    registrations = shared_broker_registrations(claude)
    registrations["git_broker"]["args"] = [
        "--verbose",
        str(source / "server.py"),
    ]

    rendered, assets = render_codex.prepare_mcp_assets(
        registrations, claude_root=claude, codex_root=codex
    )

    # The mux is read for its config, never copied: Codex runs its children
    # directly, so only the Git broker's own tree ships.
    assert assets == [(source, codex / "mcps" / "git-broker")]
    assert rendered["git_broker"]["args"] == [
        "--verbose",
        str(codex / "mcps" / "git-broker" / "server.py"),
    ]

    registrations["git_broker"]["args"] = ["/somewhere/else/server.py"]
    with pytest.raises(ValueError, match="server entrypoint"):
        render_codex.prepare_mcp_assets(
            registrations, claude_root=claude, codex_root=codex
        )


def test_codex_overlay_adds_client_policy(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    write_git_broker_source(claude)

    rendered, _ = render_codex.prepare_mcp_assets(
        shared_broker_registrations(claude),
        claude_root=claude,
        codex_root=tmp_path / ".codex",
    )

    git_policy = {
        "required": True,
        "enabled_tools": ["git", "github"],
        "disabled_tools": ["exec"],
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 10,
        "tool_timeout_sec": 600,
    }
    for key, value in git_policy.items():
        assert rendered["git_broker"][key] == value
    github_policy = {
        "required": False,
        "disabled_tools": list(render_codex.GITHUB_MCP_EXCLUDED_TOOLS),
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 600,
    }
    for key, value in github_policy.items():
        assert rendered["github"][key] == value


def test_codex_requires_every_shared_broker_registration() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        write_git_broker_source(claude)
        registrations = shared_broker_registrations(claude)

        for missing in ("git_broker", "github"):
            incomplete = {
                name: value
                for name, value in registrations.items()
                if name != missing
            }
            try:
                render_codex.prepare_mcp_assets(
                    incomplete, claude_root=claude, codex_root=codex
                )
            except ValueError as exc:
                assert missing in str(exc)
                assert "~/.claude/mcp-servers.json" in str(exc)
            else:
                raise AssertionError(
                    f"missing Claude registration {missing!r} was accepted"
                )


def test_codex_gets_one_direct_child_per_github_installation(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    write_git_broker_source(claude)

    rendered, _ = render_codex.prepare_mcp_assets(
        shared_broker_registrations(claude), claude_root=claude, codex_root=codex
    )

    shared = [
        "--toolsets",
        "context,repos,issues,pull_requests,actions",
        "--exclude-tools",
        GITHUB_EXCLUSIONS,
    ]
    assert rendered["github"]["command"] == "github-app-mcp-server"
    assert rendered["github"]["args"] == shared
    assert rendered["github_acme"]["command"] == "github-app-mcp-server"
    assert rendered["github_acme"]["args"] == ["--owner", "acme", *shared]
    # Codex never runs the multiplexer itself: it is a `uv run --script` entry
    # point, and uv locks inside ~/.cache/uv, which the managed profile mounts
    # read-only. A registration carrying `uv` would start and then fail.
    for registration in rendered.values():
        assert "uv" not in str(registration.get("command", ""))
    for name in ("github", "github_acme"):
        assert rendered[name]["disabled_tools"] == list(
            render_codex.GITHUB_MCP_EXCLUDED_TOOLS
        )
        assert rendered[name]["required"] is False


def test_codex_rejects_a_github_installation_name_toml_cannot_hold(
    tmp_path: Path,
) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    write_git_broker_source(claude)
    registrations = shared_broker_registrations(claude)
    config_path = claude / "mcps" / "github-mux" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["ownerChildren"] = {"not a login": ["--owner", "x"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="not a usable child name"):
        render_codex.prepare_mcp_assets(
            registrations, claude_root=claude, codex_root=codex
        )


def test_codex_rejects_github_mux_without_exclude_tools() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        write_git_broker_source(claude)
        registrations = shared_broker_registrations(claude)
        config_path = claude / "mcps" / "github-mux" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["sharedArgs"] = [
            "--toolsets",
            "context,repos,issues,pull_requests,actions",
        ]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        with pytest.raises(ValueError, match="--exclude-tools"):
            render_codex.prepare_mcp_assets(
                registrations, claude_root=claude, codex_root=codex
            )


def test_codex_rejects_duplicate_github_mux_exclude_tools(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    write_git_broker_source(claude)
    registrations = shared_broker_registrations(claude)
    config_path = claude / "mcps" / "github-mux" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["sharedArgs"].extend(["--exclude-tools", ""])
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one --exclude-tools"):
        render_codex.prepare_mcp_assets(
            registrations, claude_root=claude, codex_root=codex
        )


def test_codex_rejects_github_mux_shared_args_below_security_floor(
    tmp_path: Path,
) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    write_git_broker_source(claude)
    registrations = shared_broker_registrations(claude)
    mux = claude / "mcps" / "github-mux"
    omitted = render_codex.GITHUB_MCP_EXCLUDED_TOOLS[-1]
    excluded = ",".join(
        tool for tool in render_codex.GITHUB_MCP_EXCLUDED_TOOLS if tool != omitted
    )
    (mux / "config.json").write_text(
        json.dumps(
            {
                "command": "github-app-mcp-server",
                "sharedArgs": [
                    "--toolsets",
                    "context,repos,issues,pull_requests,actions",
                    "--exclude-tools",
                    excluded,
                ],
                "defaultOwners": ["jonathanmoregard"],
                "ownerChildren": {"acme": ["--owner", "acme"]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as raised:
        render_codex.prepare_mcp_assets(
            registrations, claude_root=claude, codex_root=codex
        )

    assert "github" in str(raised.value)
    assert omitted in str(raised.value)


def test_codex_git_broker_forwards_host_ssh_agent_for_ssh_push() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        write_git_broker_source(claude)

        rendered, _ = render_codex.prepare_mcp_assets(
            shared_broker_registrations(claude),
            claude_root=claude,
            codex_root=codex,
        )
        broker = rendered["git_broker"]
        table = "\n".join(render_codex.codex_mcp_table("git_broker", broker))
        parsed = tomllib.loads(table)["mcp_servers"]["git_broker"]

        assert broker["env"]["SSH_AUTH_SOCK"] == ""
        assert parsed["env_vars"] == ["SSH_AUTH_SOCK"]
        assert parsed["env"] == {
            "DCG_PATH": str(render_codex.PER_USER_PROFILE_BIN / "dcg"),
        }


def test_codex_preserves_optional_broker_registration_without_assets(
    tmp_path: Path,
) -> None:
    claude = tmp_path / ".claude"
    write_git_broker_source(claude)
    registrations = shared_broker_registrations(claude)
    registrations["smarthome_ssh"] = {
        "command": "remote-smarthome-mcp",
        "args": ["--stdio"],
    }

    rendered, _ = render_codex.prepare_mcp_assets(
        registrations, claude_root=claude, codex_root=tmp_path / ".codex"
    )

    assert rendered["smarthome_ssh"] == registrations["smarthome_ssh"]


def test_publish_mcp_assets_replaces_stale_managed_tree() -> None:
    # Each destination is replaced whole. A retired broker beside them is
    # unplanned, and the walker prunes it with everything else unplanned
    # (test_mirror_prunes_every_unplanned_path_inside_the_managed_paths_only).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        source = write_git_broker_source(claude)
        stale = codex / "mcps" / "git-broker" / "retired.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("# retired module\n", encoding="utf-8")

        _, assets = render_codex.prepare_mcp_assets(
            shared_broker_registrations(claude),
            claude_root=claude,
            codex_root=codex,
        )
        assert assets == [(source, codex / "mcps" / "git-broker")]

        with patch.object(render_codex, "CODEX", codex):
            render_codex.publish_mcp_assets(assets)

        assert not stale.exists()
        assert {path.name for path in (codex / "mcps").iterdir()} == {"git-broker"}
        assert (codex / "mcps" / "git-broker" / "server.py").read_text(
            encoding="utf-8"
        ) == "# structured broker\n"


def test_publish_mcp_assets_refuses_a_destination_outside_the_mirror() -> None:
    # It removed the parent of any destination it was given; like every
    # other publish write it goes through the managed-path primitive.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = write_git_broker_source(root / ".claude")
        mine = root / "x" / "keep" / "mine.txt"
        mine.parent.mkdir(parents=True)
        mine.write_text("mine\n", encoding="utf-8")
        with patch.object(render_codex, "CODEX", root / ".codex"):
            try:
                render_codex.publish_mcp_assets([(source, root / "x" / "y")])
            except ValueError as exc:
                assert "outside every path the mirror owns" in str(exc), exc
            else:
                raise AssertionError("published outside the mirror")
        assert mine.read_text(encoding="utf-8") == "mine\n"
        assert not (root / "x" / "y").exists()


def write_smarthome_ssh_source(claude: Path) -> Path:
    source = claude / "mcps" / "smarthome-ssh"
    source.mkdir(parents=True)
    (source / "server.py").write_text("# host-pinned ssh broker\n", encoding="utf-8")
    (source / "config.json").write_text(
        '{"smarthomeSsh": {"host": "home-server"}}\n', encoding="utf-8"
    )
    return source


def test_codex_injects_optional_smarthome_ssh_broker_when_source_exists() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        git_source = write_git_broker_source(claude)
        ssh_source = write_smarthome_ssh_source(claude)

        rendered, assets = render_codex.prepare_mcp_assets(
            shared_broker_registrations(claude),
            claude_root=claude,
            codex_root=codex,
        )

        assert assets == [
            (git_source, codex / "mcps" / "git-broker"),
            (ssh_source, codex / "mcps" / "smarthome-ssh"),
        ]
        broker = rendered["smarthome_ssh"]
        assert broker["command"] == "python3"
        assert broker["args"] == [str(codex / "mcps" / "smarthome-ssh" / "server.py")]
        assert broker["required"] is False
        assert broker["enabled_tools"] == ["ssh"]
        assert broker["default_tools_approval_mode"] == "approve"
        assert broker["tool_timeout_sec"] == 600
        parsed = tomllib.loads(
            "\n".join(render_codex.codex_mcp_table("smarthome_ssh", broker))
        )["mcp_servers"]["smarthome_ssh"]
        assert parsed["enabled_tools"] == ["ssh"]


def test_invalid_smarthome_ssh_policy_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        write_git_broker_source(claude)
        source = write_smarthome_ssh_source(claude)
        (source / "config.json").write_text('{"other": {}}\n', encoding="utf-8")
        try:
            render_codex.prepare_mcp_assets(
                shared_broker_registrations(claude),
                claude_root=claude,
                codex_root=root / ".codex",
            )
        except ValueError as exc:
            assert "smarthomeSsh" in str(exc)
        else:
            raise AssertionError("accepted smarthome-ssh policy without smarthomeSsh")


def test_publish_mcp_assets_ships_every_broker() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        write_git_broker_source(claude)
        write_smarthome_ssh_source(claude)

        _, assets = render_codex.prepare_mcp_assets(
            shared_broker_registrations(claude),
            claude_root=claude,
            codex_root=codex,
        )
        with patch.object(render_codex, "CODEX", codex):
            render_codex.publish_mcp_assets(assets)

        assert {path.name for path in (codex / "mcps").iterdir()} == {
            "git-broker",
            "smarthome-ssh",
        }


def test_codex_git_broker_table_is_required_and_exec_is_disabled() -> None:
    table = "\n".join(
        render_codex.codex_mcp_table(
            "git_broker",
            {
                "command": "python3",
                "args": ["/codex/mcps/git-broker/server.py"],
                "required": True,
                "enabled_tools": ["git", "github"],
                "disabled_tools": ["exec"],
                "default_tools_approval_mode": "approve",
                "startup_timeout_sec": 10,
                "tool_timeout_sec": 600,
                "env": {
                    "DCG_PATH": "/bin/dcg",
                },
            },
        )
    )
    parsed = tomllib.loads(table)["mcp_servers"]["git_broker"]

    assert parsed["required"] is True
    assert parsed["enabled_tools"] == ["git", "github"]
    assert parsed["disabled_tools"] == ["exec"]
    assert parsed["default_tools_approval_mode"] == "approve"
    assert parsed["startup_timeout_sec"] == 10
    assert parsed["tool_timeout_sec"] == 600


def test_codex_instructions_route_every_git_mutation_through_broker() -> None:
    rendered = render_codex.codex_agents_text("source instructions\n")

    assert "mcp__git_broker__git" in rendered
    assert "GIT_OPTIONAL_LOCKS=0" in rendered
    assert "read-only Git" in rendered
    assert "Never fall back" in rendered
    assert "mcp__git_broker__exec" in rendered
    assert "mcp__safe_bash__git" not in rendered


def test_orchestrate_adapter_names_dedicated_git_broker() -> None:
    adapter = (
        RENDERER / "clients" / "codex" / "skills" / "orchestrate" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "mcp__git_broker__git" in adapter
    assert "mcp__safe_bash__git" not in adapter
    assert "Do not require Claude `mcp__safe-bash__exec`" not in adapter


def write_agent_e2e_broker_source(claude: Path) -> Path:
    source = claude / "mcps" / "agent-e2e-broker"
    source.mkdir(parents=True)
    (source / "server.py").write_text("# agent-e2e broker\n", encoding="utf-8")
    (source / "config.json").write_text(
        '{"agentE2eBroker": {"version": 1}}\n', encoding="utf-8"
    )
    return source


def test_codex_injects_optional_agent_e2e_broker_with_ssh_agent_for_fetch() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        codex = root / ".codex"
        git_source = write_git_broker_source(claude)
        e2e_source = write_agent_e2e_broker_source(claude)

        rendered, assets = render_codex.prepare_mcp_assets(
            shared_broker_registrations(claude),
            claude_root=claude,
            codex_root=codex,
        )

        assert assets == [
            (git_source, codex / "mcps" / "git-broker"),
            (e2e_source, codex / "mcps" / "agent-e2e-broker"),
        ]
        assert "smarthome_ssh" not in rendered
        parsed = tomllib.loads(
            "\n".join(
                render_codex.codex_mcp_table("agent_e2e_broker", rendered["agent_e2e_broker"])
            )
        )["mcp_servers"]["agent_e2e_broker"]
        assert parsed["args"] == [str(codex / "mcps" / "agent-e2e-broker" / "server.py")]
        assert parsed["required"] is False
        assert parsed["enabled_tools"] == ["run"]
        assert parsed["default_tools_approval_mode"] == "approve"
        assert parsed["tool_timeout_sec"] == 3900
        assert parsed["env_vars"] == ["SSH_AUTH_SOCK"]
        assert "env" not in parsed


def test_invalid_agent_e2e_broker_policy_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        claude = Path(td) / ".claude"
        write_git_broker_source(claude)
        source = write_agent_e2e_broker_source(claude)
        (source / "config.json").write_text('{"other": {}}\n', encoding="utf-8")
        try:
            render_codex.prepare_mcp_assets(
                shared_broker_registrations(claude),
                claude_root=claude,
                codex_root=Path(td) / ".codex",
            )
        except ValueError as exc:
            assert "agentE2eBroker" in str(exc)
        else:
            raise AssertionError("invalid agent-e2e broker policy was accepted")


if __name__ == "__main__":
    test_import_prefers_dedicated_mcp_source_and_omits_fully_denied_server()
    test_render_reconciles_unmanaged_codex_mcp_tables_to_claude_source()
    test_render_disables_builtin_github_connector_and_preserves_other_apps()
    test_render_preserves_mirrored_aggregator_command()
    test_render_maps_claude_timeout_milliseconds_to_codex_tool_seconds()
    test_render_auto_approves_mcp_server_wildcard_allowed_by_claude()
    test_render_does_not_auto_approve_mcp_server_with_higher_priority_rule()
    test_render_approves_only_the_exact_mcp_tools_allowed_by_claude()
    test_render_server_wide_ask_or_deny_blocks_per_tool_approval()
    test_render_ignores_per_tool_rules_for_other_servers()
    with tempfile.TemporaryDirectory() as td:
        test_codex_overlay_preserves_claude_command_args_and_env(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_codex_overlay_rewrites_the_git_broker_server_arg(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_codex_overlay_adds_client_policy(Path(td))
    test_codex_requires_every_shared_broker_registration()
    with tempfile.TemporaryDirectory() as td:
        test_codex_gets_one_direct_child_per_github_installation(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_codex_rejects_a_github_installation_name_toml_cannot_hold(Path(td))
    test_codex_rejects_github_mux_without_exclude_tools()
    with tempfile.TemporaryDirectory() as td:
        test_codex_rejects_duplicate_github_mux_exclude_tools(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_codex_rejects_github_mux_shared_args_below_security_floor(Path(td))
    test_codex_git_broker_forwards_host_ssh_agent_for_ssh_push()
    with tempfile.TemporaryDirectory() as td:
        test_codex_preserves_optional_broker_registration_without_assets(Path(td))
    test_publish_mcp_assets_replaces_stale_managed_tree()
    test_publish_mcp_assets_refuses_a_destination_outside_the_mirror()
    test_codex_injects_optional_smarthome_ssh_broker_when_source_exists()
    test_invalid_smarthome_ssh_policy_fails_closed()
    test_publish_mcp_assets_ships_every_broker()
    test_codex_git_broker_table_is_required_and_exec_is_disabled()
    test_codex_instructions_route_every_git_mutation_through_broker()
    test_orchestrate_adapter_names_dedicated_git_broker()
    test_codex_injects_optional_agent_e2e_broker_with_ssh_agent_for_fetch()
    test_invalid_agent_e2e_broker_policy_fails_closed()
    print("MCP reconciliation tests OK")

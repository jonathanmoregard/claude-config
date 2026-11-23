#!/usr/bin/env python3
"""Gate publication of a rendered Codex tree.

Two independent layers, both must pass:

1. Drift: the expected output comes from the renderer's own plan
   (prepare_render_plan + render_plan_drift), never from re-composing renderer
   helpers here, so a new render step cannot be missed by the check.
2. Safety invariants: properties of the published files themselves. They read
   parsed output and import only constants, so a renderer bug that the plan
   faithfully reproduces still cannot publish an unsafe Codex config. The one
   function they borrow is the hook fan-out's own plan loader: what it
   rejects, the runtime rejects.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import tomllib
import traceback
from typing import Any

from codex_hook_fanout import load_handlers
from common import (
    CODEX,
    CLAUDE,
    MANIFEST,
    PLUGIN_ROOT,
    REPORT_DIR,
    load_json,
)
from import_claude import file_inventory
from render_codex import (
    CODEX_SUPPORTED_HOOK_EVENTS,
    CODEX_UNSUPPORTED_HOOK_EVENTS,
    HOOK_WRAPPER_ROOT,
    SAFE_BASH_TOOL_PATTERN,
    load_support,
    prepare_render_plan,
    render_plan_drift,
)


HOOK_DISABLE_GUARD = "CODEX_CLAUDE_MIRROR_ENABLE_HOOKS"
CODEX_HOOK_ACTION_TYPES = frozenset({"command", "prompt", "agent"})
UNSUPPORTED_TOP_LEVEL_BUDGET_CONFIG_KEYS = (
    "skillListingBudgetFraction",
    "skills_context_budget_fraction",
)
LEGACY_SANDBOX_KEYS = ("sandbox_mode", "approvals_reviewer", "sandbox_workspace_write")
GIT_BROKER = "git_broker"
GIT_BROKER_GENERIC_TOOL = "exec"
# Safety floors, stated here on purpose rather than imported from the
# renderer: loosening one must take an edit in both places, so a change to the
# renderer alone makes every sync (and the round-trip tests) fail. The
# renderer may be stricter than these; it may never be looser.
CODEX_PERMISSION_PROFILE = "repos_dev"
CODEX_APPROVAL_POLICY = "never"
GITHUB_MCP_NAME = "github"
BUILTIN_GITHUB_CONNECTOR_APP_ID = "connector_76869538009648d5b282a4bb21c3d157"
GITHUB_MCP_EXCLUDED_TOOLS = (
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
# The Codex execpolicy builtins that carry a decision, as Codex 0.161.0
# parses them (`codex execpolicy check`): name -> (positional index of
# decision, decision when omitted, decisions Codex accepts). Both can grant
# allow: prefix_rule by default, network_rule when told to. The third builtin,
# host_executable, carries no decision. Stated here, not imported, for the
# same reason as the floors above; a test holds the renderer's table to it.
EXECPOLICY_DECISION_BUILTINS: dict[str, tuple[int, str | None, tuple[str, ...]]] = {
    "prefix_rule": (1, "allow", ("allow", "prompt", "forbidden")),
    "network_rule": (2, None, ("allow", "deny", "forbidden", "prompt")),
}
# Agent role file keys that cannot widen the session they are layered over.
# sandbox_mode and approval_policy are allowed only with the values below.
AGENT_ROLE_KEYS = frozenset(
    {
        "name",
        "description",
        "nickname_candidates",
        "developer_instructions",
        "model",
        "model_reasoning_effort",
    }
)
AGENT_ROLE_SANDBOX_MODES = ("read-only", "workspace-write")
# The text every generated skill wrapper carries: skills and commands.
GENERATED_SKILL_MARKERS = ("Mirrored from Claude skill:", "Mirrored from Claude command:")


def describe_exception(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if exc.__cause__ is not None:
        cause = exc.__cause__
        text += f" (caused by {type(cause).__name__}: {cause})"
    return text


def validate_plugin_skills(manifest: dict[str, object]) -> list[str]:
    plugin_states = {
        plugin["id"]: plugin.get("enabled")
        for plugin in manifest.get("plugins") or []
    }
    errors: list[str] = []
    for skill in manifest.get("plugin_skills") or []:
        plugin_id = skill.get("plugin")
        if plugin_states.get(plugin_id) is not True:
            errors.append(
                f"{plugin_id}: plugin skill {skill.get('name')} requires manifest enabled = true"
            )
    return errors


def unsupported_budget_config_keys(config: dict[str, object]) -> list[str]:
    keys = [
        key
        for key in UNSUPPORTED_TOP_LEVEL_BUDGET_CONFIG_KEYS
        if key in config
    ]
    skills = config.get("skills")
    if isinstance(skills, dict) and "max_context_tokens" in skills:
        keys.append("skills.max_context_tokens")
    return keys


def mcp_servers_invariant_errors(servers: object, table: str) -> list[str]:
    """MCP floors on one server table Codex loads, named by its key.

    The same floors hold wherever Codex reads a server registration:
    config.toml's mcp_servers and the plugin's .mcp.json mcpServers.
    """
    if servers is not None and not isinstance(servers, dict):
        return [f"{table} must be an object"]
    servers = servers or {}
    errors: list[str] = []
    broker = servers.get(GIT_BROKER)
    if not isinstance(broker, dict):
        errors.append(f"{table}.{GIT_BROKER}: structured Git broker is missing")
    else:
        if broker.get("required") is not True:
            errors.append(f"{table}.{GIT_BROKER} must be required")
        disabled = broker.get("disabled_tools")
        enabled = broker.get("enabled_tools")
        if (
            not isinstance(disabled, list)
            or GIT_BROKER_GENERIC_TOOL not in disabled
            or (isinstance(enabled, list) and GIT_BROKER_GENERIC_TOOL in enabled)
        ):
            errors.append(
                f"{table}.{GIT_BROKER} must not expose the generic "
                f"{GIT_BROKER_GENERIC_TOOL} tool"
            )
    for name, server in sorted(servers.items()):
        if name != GITHUB_MCP_NAME and not name.startswith(f"{GITHUB_MCP_NAME}_"):
            continue
        disabled = server.get("disabled_tools") if isinstance(server, dict) else None
        disabled = disabled if isinstance(disabled, list) else []
        missing = [tool for tool in GITHUB_MCP_EXCLUDED_TOOLS if tool not in disabled]
        if missing:
            errors.append(
                f"{table}.{name}: GitHub server must disable {', '.join(missing)}"
            )
    return errors


def plugin_mcp_invariant_errors(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["plugin MCP document must be an object"]
    return mcp_servers_invariant_errors(document.get("mcpServers"), "mcpServers")


def config_invariant_errors(config: dict[str, Any]) -> list[str]:
    """Safety properties of a parsed config.toml, independent of the renderer."""
    errors: list[str] = []
    for key in LEGACY_SANDBOX_KEYS:
        if key in config:
            errors.append(f"legacy {key} must be absent")
    if config.get("default_permissions") != CODEX_PERMISSION_PROFILE:
        errors.append(f"default_permissions must be {CODEX_PERMISSION_PROFILE}")
    if config.get("approval_policy") != CODEX_APPROVAL_POLICY:
        errors.append(f"approval_policy must be {CODEX_APPROVAL_POLICY}")
    profiles = config.get("permissions") or {}
    if not isinstance(profiles, dict) or CODEX_PERMISSION_PROFILE in profiles:
        errors.append(
            f"{CODEX_PERMISSION_PROFILE} must be absent from user config because "
            "managed requirements own its definition"
        )

    features = config.get("features")
    if not isinstance(features, dict) or features.get("hooks") is not True:
        errors.append("features.hooks must be true")
    if isinstance(features, dict) and "codex_hooks" in features:
        errors.append("deprecated codex_hooks feature flag remains")

    errors.extend(mcp_servers_invariant_errors(config.get("mcp_servers"), "mcp_servers"))

    apps = config.get("apps")
    app = apps.get(BUILTIN_GITHUB_CONNECTOR_APP_ID) if isinstance(apps, dict) else None
    if not isinstance(app, dict) or app.get("enabled") is not False:
        errors.append("built-in GitHub connector must be disabled")

    errors.extend(
        f"unsupported budget setting {key} must not be generated"
        for key in unsupported_budget_config_keys(config)
    )
    return errors


def agent_role_invariant_errors(role: dict[str, Any]) -> list[str]:
    """Codex layers an agent role file over the session config, so a role key
    outside this allowlist could loosen the session (mcp_servers,
    permissions, default_permissions, features, shell policy, ...).

    sandbox_mode is ignored while the managed permission profile is active
    (`codex debug prompt-input` shows the profile's roots for every value);
    it is floored anyway. approval_policy is honored, and anything but never
    re-enables escalation out of the sandbox.
    """
    errors: list[str] = []
    for key, value in role.items():
        if key in AGENT_ROLE_KEYS:
            continue
        if key == "sandbox_mode":
            if value not in AGENT_ROLE_SANDBOX_MODES:
                errors.append(
                    f"sandbox_mode must be {spoken_choice(list(AGENT_ROLE_SANDBOX_MODES))}"
                )
        elif key == "approval_policy":
            if value != CODEX_APPROVAL_POLICY:
                errors.append(f"approval_policy must be absent or {CODEX_APPROVAL_POLICY}")
        else:
            errors.append(f"{key} must not be set in an agent role file")
    return errors


def agent_role_paths(config: dict[str, Any] | None) -> list[Path]:
    """Every role file Codex reads: the ones config.toml points at, and the
    *.toml files it discovers under ~/.codex/agents."""
    paths: set[Path] = set()
    agents = config.get("agents") if isinstance(config, dict) else None
    for role in agents.values() if isinstance(agents, dict) else []:
        config_file = role.get("config_file") if isinstance(role, dict) else None
        if isinstance(config_file, str):
            path = Path(config_file).expanduser()
            paths.add(path if path.is_absolute() else CODEX / path)
    root = CODEX / "agents"
    if root.is_dir():
        paths.update(path for path in root.rglob("*.toml") if path.is_file())
    return sorted(paths)


def rule_decision(call: ast.Call, name: str) -> object:
    """The literal decision of an execpolicy rule call; None when not a literal."""
    index, default, _ = EXECPOLICY_DECISION_BUILTINS[name]
    if any(isinstance(argument, ast.Starred) for argument in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return None
    decisions = [keyword.value for keyword in call.keywords if keyword.arg == "decision"]
    if len(call.args) > index:
        decisions.append(call.args[index])
    if not decisions:
        return default  # Codex's default: allow for prefix_rule, none for network_rule.
    if len(decisions) > 1 or not isinstance(decisions[0], ast.Constant):
        return None
    return decisions[0].value


def spoken_choice(options: list[str]) -> str:
    return options[0] if len(options) == 1 else f"{', '.join(options[:-1])} or {options[-1]}"


def rules_invariant_errors(text: str) -> list[str]:
    """No persisted allow rule: an allowed command runs unsandboxed on the
    host, an allowed network rule widens network access.

    The renderer strips top-level allow rules on their own lines; every other
    finding stays until a human edits the file, and the message says so.
    """
    try:
        document = ast.parse(text)
    except SyntaxError as exc:
        return [f"invalid exec policy: {exc}"]
    errors: list[str] = []
    calls = [
        node
        for node in ast.walk(document)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in EXECPOLICY_DECISION_BUILTINS
    ]
    # Any other use of the name (an alias, a container, an attribute) can
    # reach a rule whose decision this check cannot read: fail closed.
    direct = {id(call.func) for call in calls}
    for node in ast.walk(document):
        name = (
            node.id
            if isinstance(node, ast.Name) and id(node) not in direct
            else node.attr
            if isinstance(node, ast.Attribute)
            else None
        )
        if name in EXECPOLICY_DECISION_BUILTINS:
            errors.append(
                f"line {node.lineno}: {name} used other than as a direct call, so its "
                "decision cannot be read; a human must remove it"
            )
    # Top-level direct calls, by position, for the renderer's own-lines rule.
    top_level = {
        id(statement.value): position
        for position, statement in enumerate(document.body)
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    }
    for node in calls:
        func = node.func
        if not isinstance(func, ast.Name):  # guaranteed by the filter above
            continue
        name = func.id
        decision = rule_decision(node, name)
        safe = [
            option for option in EXECPOLICY_DECISION_BUILTINS[name][2] if option != "allow"
        ]
        if decision == "allow":
            position = top_level.get(id(node))
            if position is None:
                advice = (
                    " nested inside other code; the sync strips only top-level "
                    "rules, so a human must remove it"
                )
            elif (
                position > 0
                and (document.body[position - 1].end_lineno or 0) >= node.lineno
            ) or (
                position + 1 < len(document.body)
                and document.body[position + 1].lineno <= (node.end_lineno or node.lineno)
            ):
                advice = (
                    " shares a line with other code; the sync strips only rules on "
                    "their own lines, so a human must remove it"
                )
            else:
                advice = "; the next sync strips it"
            errors.append(f"line {node.lineno}: persisted allow {name}{advice}")
        elif decision not in safe:
            errors.append(
                f"line {node.lineno}: {name} decision must be a literal "
                f"{spoken_choice(safe)}; a human must fix it"
            )
    return errors


def report_invariant_errors(report: object) -> list[str]:
    if not isinstance(report, dict):
        return ["report must be an object"]
    mappings = report.get("mappings", {})
    if not isinstance(mappings, dict):
        return ["report mappings must be an object"]
    claim = mappings.get("skills.max_context_tokens")
    if claim == "active" or isinstance(claim, dict) and claim.get("status") == "active":
        return ["report must not claim skills.max_context_tokens is active"]
    return []


def validate_frontmatter(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return [f"{path}: missing frontmatter"]
    end = text.find("\n---", 4)
    if end < 0:
        return [f"{path}: unterminated frontmatter"]
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        try:
            value = ast.literal_eval(raw.strip())
        except Exception:
            value = raw.strip().strip('"')
        fields[key.strip()] = value
    errors: list[str] = []
    for key in ("name", "description"):
        if not fields.get(key):
            errors.append(f"{path}: missing {key}")
    if fields.get("description") in {">", "|", ">-", "|-", ">+", "|+"}:
        errors.append(f"{path}: invalid block-marker description")
    return errors


def validate_hooks_document(document: object, path: str | Path) -> list[str]:
    label = str(path)
    if not isinstance(document, dict):
        return [f"{label}: hook document must be an object"]

    errors: list[str] = []
    expected_keys = {"description", "hooks"}
    if set(document) != expected_keys:
        errors.append(f"{label}: top-level keys must be exactly description and hooks")
    if "description" not in document:
        errors.append(f"{label}: missing description")
    elif not isinstance(document["description"], str) or not document["description"].strip():
        errors.append(f"{label}: description must be a non-empty string")
    if "hooks" not in document:
        errors.append(f"{label}: missing hooks")
        return errors

    hooks = document["hooks"]
    if not isinstance(hooks, dict):
        errors.append(f"{label}: hooks must be an object")
        return errors

    for event, entries in hooks.items():
        if event in CODEX_UNSUPPORTED_HOOK_EVENTS:
            errors.append(f"{label}: unsupported event {event}")
        elif event not in CODEX_SUPPORTED_HOOK_EVENTS:
            errors.append(f"{label}: unknown Codex hook event {event}")
        if not isinstance(entries, list):
            errors.append(f"{label}: {event} must be an array")
            continue
        for entry_index, entry in enumerate(entries):
            entry_label = f"{label}: {event}[{entry_index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_label} must be an object")
                continue
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                errors.append(f"{entry_label}.matcher must be a string")
            elif isinstance(matcher, str) and SAFE_BASH_TOOL_PATTERN.search(matcher):
                errors.append(f"{entry_label}.matcher uses legacy tool spelling")
            actions = entry.get("hooks")
            if not isinstance(actions, list):
                errors.append(f"{entry_label}.hooks must be an array")
                continue
            for action_index, action in enumerate(actions):
                action_label = f"{entry_label}.hooks[{action_index}]"
                if not isinstance(action, dict):
                    errors.append(f"{action_label} must be an object")
                    continue
                action_type = action.get("type")
                if "type" not in action:
                    errors.append(f"{action_label}.type is required")
                elif not isinstance(action_type, str):
                    errors.append(f"{action_label}.type must be a string")
                elif action_type not in CODEX_HOOK_ACTION_TYPES:
                    errors.append(f"{action_label}.type must be command, prompt, or agent")
                elif action_type != "command":
                    errors.append(
                        f"{action_label}: unsupported hook action type {action_type}"
                    )
                command = action.get("command")
                if action_type == "command" and "command" not in action:
                    errors.append(f"{action_label}.command is required for command hooks")
                elif command is not None and not isinstance(command, str):
                    errors.append(f"{action_label}.command must be a string")
                elif isinstance(command, str):
                    if SAFE_BASH_TOOL_PATTERN.search(command):
                        errors.append(f"{action_label}.command uses legacy tool spelling")
                    if HOOK_DISABLE_GUARD in command:
                        errors.append(f"{action_label}.command contains the blanket disable guard")
                timeout = action.get("timeout")
                if timeout is not None:
                    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                        errors.append(f"{action_label}.timeout must be numeric")
                    elif timeout < 0:
                        errors.append(f"{action_label}.timeout must be non-negative")
    return errors


def read_text(path: Path, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(f"{path}: missing")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path}: unreadable: {describe_exception(exc)}")
    return None


def read_toml(path: Path, errors: list[str]) -> dict[str, Any] | None:
    text = read_text(path, errors)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{path}: invalid TOML: {exc}")
    return None


JSON_KINDS = {
    type(None): "null",
    list: "an array",
    str: "a string",
    int: "a number",
    float: "a number",
    bool: "a boolean",
}


def read_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    """The JSON object in path, or None with the reason in errors.

    Every document the check reads is an object. Anything else is an error
    here, not left to the caller: a literal null parses to None, which reads
    as "already reported" and would skip every floor on that file.
    """
    text = read_text(path, errors)
    if text is None:
        return None
    return json_object(path, text, errors)


def json_object(path: Path, text: str, errors: list[str]) -> dict[str, Any] | None:
    """read_json on text already read."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON: {exc}")
        return None
    if not isinstance(document, dict):
        kind = JSON_KINDS.get(type(document), type(document).__name__)
        errors.append(f"{path}: top-level JSON must be an object, not {kind}")
        return None
    return document


def absolute_paths(text: str) -> list[Path]:
    """The absolute paths a command names, normalized."""
    try:
        tokens = shlex.split(text, comments=True)
    except ValueError:
        tokens = text.split()
    paths = (Path(os.path.normpath(token)) for token in tokens)
    return [path for path in paths if path.is_absolute()]


def hook_generation_paths(text: str) -> list[Path]:
    """Absolute paths under the hook generation root that a command names."""
    return [path for path in absolute_paths(text) if HOOK_WRAPPER_ROOT in path.parents]


def hook_wrapper_invariant_errors(
    documents: list[tuple[Path, object]], errors: list[str]
) -> list[str]:
    """Command invariants on the text Codex actually runs for hooks documents.

    A rendered command only names a dispatcher; the Claude command text is in
    the wrappers its fan-out plan lists. Follow the chain from the document
    (command -> dispatcher -> plan -> wrappers) and scan each wrapper and
    plan. Earlier generations, retained for live sessions, are not reached.
    The chain is followed only under the generation root, so a command or a
    plan wrapper that leads anywhere else is itself a finding: what it runs
    would go unread. (A wrapper's own text is the Claude command, which may
    name any path; it is scanned as it stands.)
    """
    found: list[str] = []
    queue: list[Path] = []
    for document_path, document in documents:
        hooks = document.get("hooks") if isinstance(document, dict) else None
        for event, entries in hooks.items() if isinstance(hooks, dict) else []:
            for entry in entries if isinstance(entries, list) else []:
                actions = entry.get("hooks") if isinstance(entry, dict) else None
                for action in actions if isinstance(actions, list) else []:
                    command = action.get("command") if isinstance(action, dict) else None
                    if not isinstance(command, str):
                        continue
                    label = f"{document_path}: {event} hook command"
                    found.extend(
                        f"{label} runs {path}, outside {HOOK_WRAPPER_ROOT}"
                        for path in absolute_paths(command)
                        if HOOK_WRAPPER_ROOT not in path.parents
                    )
                    named = hook_generation_paths(command)
                    if not any(path.suffix == ".sh" for path in named):
                        found.append(f"{label} names no wrapper under {HOOK_WRAPPER_ROOT}")
                    queue.extend(named)
    seen: set[Path] = set()
    while queue:
        path = queue.pop(0)
        if path in seen or path.suffix not in {".sh", ".json"}:
            continue
        seen.add(path)
        text = read_text(path, errors)
        if text is None:
            continue
        if path.suffix == ".json":
            # A plan the fan-out cannot load runs no hook of its event (it
            # exits 64), the effect of the blanket disable guard. Its own
            # loader decides, so the check accepts exactly what runs; the
            # drift check holds the deployed runtime to this source.
            plan = json_object(path, text, errors)
            if plan is None:
                continue
            try:
                load_handlers(path)
            except ValueError as exc:
                errors.append(f"{path}: {exc}")
            handlers = plan.get("handlers")
            for index, handler in enumerate(handlers if isinstance(handlers, list) else []):
                wrapper = handler.get("wrapper") if isinstance(handler, dict) else None
                if not isinstance(wrapper, str):
                    continue
                named = hook_generation_paths(shlex.quote(wrapper))
                if not named:
                    found.append(
                        f"{path}: handlers[{index}].wrapper {wrapper} is outside "
                        f"{HOOK_WRAPPER_ROOT}"
                    )
                queue.extend(named)
        else:
            queue.extend(hook_generation_paths(text))
        label = "hook wrapper" if path.suffix == ".sh" else "hook fan-out plan"
        if SAFE_BASH_TOOL_PATTERN.search(text):
            found.append(f"{path}: {label} uses legacy tool spelling")
        if HOOK_DISABLE_GUARD in text:
            found.append(f"{path}: {label} contains the blanket disable guard")
    return found


def output_invariant_errors() -> list[str]:
    errors: list[str] = []
    config_path = CODEX / "config.toml"
    config = read_toml(config_path, errors)
    if config is not None:
        errors.extend(f"{config_path}: {error}" for error in config_invariant_errors(config))
    for role_path in agent_role_paths(config):
        role = read_toml(role_path, errors)
        if role is not None:
            errors.extend(
                f"{role_path}: {error}" for error in agent_role_invariant_errors(role)
            )
    plugin_mcp_path = PLUGIN_ROOT / ".mcp.json"
    plugin_mcp = read_json(plugin_mcp_path, errors)
    if plugin_mcp is not None:
        errors.extend(
            f"{plugin_mcp_path}: {error}" for error in plugin_mcp_invariant_errors(plugin_mcp)
        )

    # Codex loads every *.rules file in its rules directory, not only the one
    # the sync renders; scan them all, nested and hidden ones included.
    rules_root = CODEX / "rules"
    rules_paths = {rules_root / "default.rules"}
    if rules_root.is_dir():
        # A directory whose name ends in .rules is not a rules file, and the
        # same walk reaches the rules files inside it.
        rules_paths.update(
            path for path in rules_root.rglob("*.rules") if path.is_file()
        )
    for rules_path in sorted(rules_paths):
        rules = read_text(rules_path, errors)
        if rules is not None:
            errors.extend(f"{rules_path}: {error}" for error in rules_invariant_errors(rules))

    hook_documents: list[tuple[Path, object]] = []
    for path in (CODEX / "hooks.json", PLUGIN_ROOT / "hooks.json"):
        document = read_json(path, errors)
        if document is not None:
            errors.extend(validate_hooks_document(document, path))
            hook_documents.append((path, document))
    found = hook_wrapper_invariant_errors(hook_documents, errors)
    errors.extend(found)

    report_path = REPORT_DIR / "report.json"
    report = read_json(report_path, errors)
    if report is not None:
        errors.extend(f"{report_path}: {error}" for error in report_invariant_errors(report))

    # Codex reads skills from both roots; commands render as skills too.
    for root in (CODEX / "skills", PLUGIN_ROOT / "skills"):
        for skill in sorted(root.glob("*/SKILL.md")):
            if skill.parent.name == ".system":
                continue
            text = read_text(skill, errors)
            if text is not None and any(marker in text for marker in GENERATED_SKILL_MARKERS):
                errors.extend(validate_frontmatter(skill))
    return errors


def render_plan_errors(manifest: dict[str, Any]) -> list[str]:
    try:
        plan = prepare_render_plan(manifest, load_support())
        return render_plan_drift(plan)
    except Exception as exc:
        return [f"cannot compare with the render plan: {describe_exception(exc)}"]


def collect_errors() -> list[str]:
    errors: list[str] = []
    manifest = read_json(MANIFEST, errors)
    if manifest is not None and (not isinstance(manifest, dict) or not manifest):
        errors.append(f"{MANIFEST}: must be a non-empty object")
        manifest = None
    if manifest is None:
        errors.append(f"{MANIFEST}: unusable; run scripts/import_claude.py")
    else:
        settings = load_json(CLAUDE / "settings.json", {})
        if file_inventory(settings) != manifest.get("source_signature"):
            errors.append(
                "Claude source drift detected; run scripts/import_claude.py and "
                "scripts/render_codex.py"
            )
        errors.extend(validate_plugin_skills(manifest))
        errors.extend(render_plan_errors(manifest))
    errors.extend(output_invariant_errors())
    return errors


def main() -> int:
    try:
        errors = collect_errors()
    except Exception as exc:
        # A crash in the check itself must still block publication.
        traceback.print_exc()
        errors = [f"check crashed: {describe_exception(exc)}"]
    if errors:
        print("ai-client-config check FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("ai-client-config check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dedicated, contained Git operation broker over stdio MCP.

The server exposes one structured ``git`` tool. It never accepts shell text or
raw argv. Every operation is validated before the corresponding client runs
without a shell inside a broker-owned bubblewrap boundary. Repository-local
execution config is masked and network exists only for validated Git operations.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


class _InFlight:
    """Per-request state for the threaded dispatcher.

    `cancelled` flips to True when `notifications/cancelled` arrives
    for this id. The check + the actual response write live under the
    same `_write_lock` in `_send_response`, so cancellation arriving
    even one nanosecond before the response is reliably observed.

    `proc` is the `subprocess.Popen` of the contained Git child. The
    cancel-handler reads it to send SIGTERM to the process group; left None
    means cancellation arrived before Git started.
    """
    __slots__ = ("cancelled", "proc")

    def __init__(self) -> None:
        self.cancelled = False
        self.proc: Optional[subprocess.Popen] = None


# Stdout writes from main thread + N worker threads MUST be serialized,
# else JSON-RPC frames interleave and corrupt the protocol.
_write_lock = threading.Lock()
# `_in_flight` is mutated by main thread (insert/delete), worker threads
# (delete on completion), and cancel-handler reads. Guarded by its own
# lock to keep critical sections short — we explicitly do NOT hold
# `_in_flight_lock` while writing to stdout.
_in_flight_lock = threading.Lock()
_in_flight: dict[Any, _InFlight] = {}

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
OUTPUT_LIMIT_CHARS = 60_000

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
EMPTY_GIT_CONFIG = Path(__file__).resolve().parent / "empty-git-config"
EMPTY_GIT_CONFIG_TEXT = (
    "# Intentionally empty. git-broker bind-mounts this regular file over the\n"
    "# repository-local config inside its Codex Git sandbox.\n"
)

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}


def _load_config() -> dict:
    """Read policy file fresh on every call so edits take effect without restart."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "git-broker policy config is missing, unreadable, or malformed; "
            f"refusing execution: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise RuntimeError(
            "git-broker policy config must be a JSON object; refusing execution"
        )
    if set(config) != {"gitBroker"}:
        raise RuntimeError(
            "git-broker config must contain exactly one gitBroker policy object"
        )
    return config


_GIT_POLICY_KEYS = frozenset(
    {
        "version",
        "repositoryRoots",
        "metadataRoots",
        "protectedWorktrees",
        "worktreeCreationRoots",
        "allowedRemoteHosts",
        "allowedRemoteSchemes",
        "gitExecutable",
        "bubblewrapExecutable",
        "validatorExecutable",
        "credentialHelperExecutable",
        "githubHostTokenDir",
        "sshKnownHostsFile",
        "trustedHooksDir",
        "commitUserName",
        "commitUserEmail",
        "discoveryTimeoutMs",
        "maxTimeoutMs",
        "outputLimitChars",
    }
)
_GIT_POLICY_PATH_LISTS = (
    "repositoryRoots",
    "metadataRoots",
    "protectedWorktrees",
    "worktreeCreationRoots",
)
_GIT_POLICY_STRING_LISTS = ("allowedRemoteHosts", "allowedRemoteSchemes")
_GIT_POLICY_EXECUTABLES = (
    "gitExecutable",
    "bubblewrapExecutable",
    "validatorExecutable",
    "credentialHelperExecutable",
)
_GIT_POLICY_POSITIVE_INTS = (
    "discoveryTimeoutMs",
    "maxTimeoutMs",
    "outputLimitChars",
)


def _load_git_policy(config: dict) -> dict:
    """Validate and normalize the complete Git-broker policy.

    Unknown fields reject so a typo or future permissive default cannot be
    silently accepted by an older broker.
    """
    raw = config.get("gitBroker") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("git-broker gitBroker policy is missing or not an object")
    missing = sorted(_GIT_POLICY_KEYS - raw.keys())
    if missing:
        raise RuntimeError(
            "git-broker gitBroker policy is missing required keys: "
            + ", ".join(missing)
        )
    unknown = sorted(raw.keys() - _GIT_POLICY_KEYS)
    if unknown:
        raise RuntimeError(
            "git-broker gitBroker policy contains unknown keys: "
            + ", ".join(unknown)
        )
    if raw["version"] != 1:
        raise RuntimeError("git-broker gitBroker version must be exactly 1")

    policy = dict(raw)
    for key in _GIT_POLICY_PATH_LISTS:
        values = raw[key]
        if not isinstance(values, list) or not values:
            raise RuntimeError(f"git-broker gitBroker {key} must be a non-empty list")
        normalized: list[Path] = []
        for value in values:
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise RuntimeError(
                    f"git-broker gitBroker {key} entries must be absolute paths"
                )
            path = Path(os.path.normpath(value)).resolve(strict=False)
            if path in normalized:
                raise RuntimeError(f"git-broker gitBroker {key} contains duplicates")
            normalized.append(path)
        policy[key] = tuple(normalized)

    for key in _GIT_POLICY_STRING_LISTS:
        values = raw[key]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise RuntimeError(f"git-broker gitBroker {key} must be a non-empty string list")
        if len(values) != len(set(values)):
            raise RuntimeError(f"git-broker gitBroker {key} contains duplicates")
        policy[key] = tuple(values)

    for key in _GIT_POLICY_EXECUTABLES:
        if not isinstance(raw[key], str) or not raw[key]:
            raise RuntimeError(f"git-broker gitBroker {key} must be a non-empty string")
    for key in (
        "githubHostTokenDir",
        "sshKnownHostsFile",
        "trustedHooksDir",
    ):
        value = raw[key]
        # "%t" (systemd's runtime-dir specifier) as the first path component
        # means the caller's own /run/user/<uid>. Derived from getuid(), never
        # from the environment, so the policy stays non-injectable while one
        # config serves hosts where jonathan's uid differs.
        if isinstance(value, str) and (value == "%t" or value.startswith("%t/")):
            value = f"/run/user/{os.getuid()}" + value[2:]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise RuntimeError(f"git-broker gitBroker {key} must be an absolute path")
        policy[key] = Path(os.path.normpath(value)).resolve(strict=False)
    for key in ("commitUserName", "commitUserEmail"):
        value = raw[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or "\n" in value
            or len(value) > 320
        ):
            raise RuntimeError(f"git-broker gitBroker {key} must be a bounded string")
    for key in _GIT_POLICY_POSITIVE_INTS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"git-broker gitBroker {key} must be a positive integer")
    if raw["maxTimeoutMs"] > MAX_TIMEOUT_MS:
        raise RuntimeError(
            f"git-broker gitBroker maxTimeoutMs cannot exceed {MAX_TIMEOUT_MS}"
        )
    if raw["outputLimitChars"] > OUTPUT_LIMIT_CHARS:
        raise RuntimeError(
            f"git-broker gitBroker outputLimitChars cannot exceed {OUTPUT_LIMIT_CHARS}"
        )
    return policy


_GIT_OPERATION_FIELDS = {
    "status": frozenset(),
    "diff": frozenset({"paths", "staged"}),
    "log": frozenset({"max_count"}),
    "add": frozenset({"paths", "all"}),
    "unstage": frozenset({"paths"}),
    "commit": frozenset({"message", "amend"}),
    "stash": frozenset({"action", "index", "include_untracked", "message"}),
    "reset": frozenset({"mode", "target"}),
    "rebase": frozenset({"action", "onto"}),
    "merge": frozenset({"action", "ref", "ff_mode", "message"}),
    "cherry_pick": frozenset({"action", "commits"}),
    "tag": frozenset({"name", "target", "message"}),
    "clone": frozenset({"url"}),
    "fetch": frozenset({"remote"}),
    "pull": frozenset({"remote"}),
    "push": frozenset({"remote", "set_upstream"}),
    "remote_branch_delete": frozenset({"branch"}),
    "switch": frozenset({"branch", "create", "start_point"}),
    "worktree_add": frozenset({"worktree_path", "branch", "start_point"}),
    "worktree_remove": frozenset({"worktree_path"}),
}
_GIT_OPERATIONS = frozenset(_GIT_OPERATION_FIELDS)
_NETWORK_GIT_OPERATIONS = frozenset(
    {"clone", "fetch", "pull", "push", "remote_branch_delete"}
)
# Operations whose sandbox needs the current worktree writable.
_READ_ONLY_WORKTREE_OPERATIONS = frozenset(
    {"fetch", "worktree_add", "worktree_remove", "remote_branch_delete"}
)
# Operations that need no current branch and change nothing local, so they
# may run from a protected primary checkout.
_PROTECTED_WORKTREE_OPERATIONS = frozenset(
    {"status", "diff", "log", "fetch", "worktree_add", "worktree_remove"}
)
_SEQUENCER_ACTIONS = {
    "rebase": frozenset({"start", "continue", "abort", "skip"}),
    "merge": frozenset({"start", "continue", "abort"}),
    "cherry_pick": frozenset({"start", "continue", "abort", "skip"}),
}
_STASH_ACTIONS = frozenset({"push", "pop", "apply", "drop", "list", "show"})
_PROTECTED_REMOTE_BRANCHES = frozenset({"main", "master"})
_GIT_BASE_FIELDS = frozenset({"repo_path", "operation", "timeout"})
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?:[~^][0-9]{0,3})*$")


def _path_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _validate_branch(value: object, field: str = "branch") -> str:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value):
        raise RuntimeError(f"{field} must be a safe local Git branch name")
    if (
        value.startswith("-")
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise RuntimeError(f"{field} must be a safe local Git branch name")
    return value


def _validate_start_point(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise RuntimeError("start_point must be a bounded Git ref name")
    if (
        "\x00" in value
        or value.startswith("-")
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or "\\" in value
        or not _REVISION_RE.fullmatch(value)
    ):
        raise RuntimeError("start_point must be a bounded Git ref name")
    return value


def _validate_revision(value: object, field: str) -> str:
    """A ref name or object id, optionally followed by ~N / ^N ancestry steps."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or value.startswith("-")
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith((".", "/", ".lock"))
        or not _REVISION_RE.fullmatch(value)
    ):
        raise RuntimeError(f"{field} must be a bounded Git revision (ref or id, ~N/^N allowed)")
    return value


def _validate_text(value: object, field: str, limit: int, *, one_line: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > limit
        or (one_line and ("\n" in value or "\r" in value))
    ):
        shape = "one non-empty line" if one_line else "non-empty"
        raise RuntimeError(f"{field} must be {shape} and at most {limit} characters")
    if _redact_secrets(value) != value:
        raise RuntimeError(f"{field} contains a recognizable secret")
    return value


def _validate_bool(args: dict, field: str) -> bool:
    value = args.get(field, False)
    if not isinstance(value, bool):
        raise RuntimeError(f"{field} must be a boolean")
    return value


def _validate_int(args: dict, field: str, low: int, high: int, default: int | None = None) -> int:
    value = args.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RuntimeError(f"{field} must be an integer from {low} to {high}")
    return value


def _validate_choice(args: dict, field: str, choices: frozenset[str], default: str) -> str:
    value = args.get(field, default)
    if value not in choices:
        raise RuntimeError(f"{field} must be one of: " + ", ".join(sorted(choices)))
    return value


def _validate_worktree_path(value: object, policy: dict) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise RuntimeError("worktree_path must be an absolute path")
    lexical_target = Path(os.path.normpath(value))
    resolved_target = lexical_target.resolve(strict=False)
    if lexical_target != resolved_target or not _path_within(
        resolved_target, policy["worktreeCreationRoots"]
    ):
        raise RuntimeError("worktree_path must be canonical and under an allowed root")
    if resolved_target in policy["worktreeCreationRoots"]:
        raise RuntimeError("worktree_path must name a directory below an allowed root")
    return resolved_target


def _validate_relative_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise RuntimeError("paths must be a non-empty list with at most 500 entries")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
            raise RuntimeError("each path must be a bounded non-empty string")
        path = Path(raw)
        if (
            path.is_absolute()
            or raw.startswith(("-", ":"))
            or any(part in {"", ".", "..", ".git"} for part in path.parts)
        ):
            raise RuntimeError(f"unsafe Git path argument: {raw!r}")
        result.append(raw)
    if len(result) != len(set(result)):
        raise RuntimeError("paths must not contain duplicates")
    return tuple(result)


def _validate_git_request(args: object, policy: dict) -> dict:
    """Validate one typed broker request without touching repository state."""
    if not isinstance(args, dict):
        raise RuntimeError("Git broker arguments must be an object")
    operation = args.get("operation")
    if operation not in _GIT_OPERATIONS:
        raise RuntimeError(f"unsupported Git operation: {operation!r}")
    allowed_fields = _GIT_BASE_FIELDS | _GIT_OPERATION_FIELDS[operation]
    unexpected = sorted(set(args) - allowed_fields)
    if unexpected:
        accepted = sorted(_GIT_OPERATION_FIELDS[operation]) or ["(none)"]
        raise RuntimeError(
            f"unexpected fields for {operation}: "
            + ", ".join(unexpected)
            + "; it accepts: "
            + ", ".join(accepted)
        )

    raw_repo = args.get("repo_path")
    if (
        not isinstance(raw_repo, str)
        or not raw_repo
        or "\x00" in raw_repo
        or not Path(raw_repo).is_absolute()
    ):
        raise RuntimeError("repo_path must be an absolute path")
    request: dict[str, Any] = {
        "repo_path": Path(os.path.normpath(raw_repo)),
        "operation": operation,
    }

    timeout = args.get("timeout", min(DEFAULT_TIMEOUT_MS, policy["maxTimeoutMs"]))
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout < 1
        or timeout > policy["maxTimeoutMs"]
    ):
        raise RuntimeError(
            f"timeout must be an integer from 1 to {policy['maxTimeoutMs']}"
        )
    request["timeout"] = timeout

    if operation == "diff":
        _validate_diff_request(args, request)
    elif operation == "log":
        count = args.get("max_count", 20)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
            raise RuntimeError("max_count must be an integer from 1 to 200")
        request["max_count"] = count
    elif operation == "add":
        _validate_add_request(args, request)
    elif operation == "unstage":
        request["paths"] = _validate_relative_paths(args.get("paths"))
    elif operation == "commit":
        _validate_commit_request(args, request)
    elif operation == "stash":
        _validate_stash_request(args, request)
    elif operation == "reset":
        request["mode"] = _validate_choice(
            args, "mode", frozenset({"soft", "mixed"}), "mixed"
        )
        request["target"] = _validate_revision(args.get("target"), "target")
    elif operation in _SEQUENCER_ACTIONS:
        _validate_sequencer_request(args, request, operation)
    elif operation == "tag":
        _validate_tag_request(args, request)
    elif operation == "clone":
        request["url"] = _validate_remote_url(args.get("url"), policy)
    elif operation in {"fetch", "pull", "push"}:
        _validate_network_request(args, request, operation)
    elif operation == "remote_branch_delete":
        branch = _validate_branch(args.get("branch"))
        if branch in _PROTECTED_REMOTE_BRANCHES:
            raise RuntimeError(f"refusing to delete protected remote branch {branch!r}")
        request["branch"] = branch
    elif operation == "worktree_remove":
        request["worktree_path"] = _validate_worktree_path(
            args.get("worktree_path"), policy
        )
    elif operation == "switch":
        _validate_switch_request(args, request)
    elif operation == "worktree_add":
        _validate_worktree_add_request(args, request, policy)
    return request


def _validate_diff_request(args: dict, request: dict) -> None:
    staged = args.get("staged", False)
    if not isinstance(staged, bool):
        raise RuntimeError("staged must be a boolean")
    request["staged"] = staged
    request["paths"] = _validate_relative_paths(args["paths"]) if "paths" in args else ()


def _validate_add_request(args: dict, request: dict) -> None:
    all_files = args.get("all", False)
    if not isinstance(all_files, bool):
        raise RuntimeError("add all must be a boolean")
    has_paths = "paths" in args
    if has_paths == all_files:
        raise RuntimeError("add requires exactly one of non-empty paths or all=true")
    request["all"] = all_files
    request["paths"] = _validate_relative_paths(args["paths"]) if has_paths else ()


def _validate_commit_request(args: dict, request: dict) -> None:
    message = args.get("message")
    if (
        not isinstance(message, str)
        or not message.strip()
        or "\x00" in message
        or len(message) > 10_000
    ):
        raise RuntimeError("commit message must be non-empty and at most 10000 characters")
    request["message"] = message
    request["amend"] = _validate_bool(args, "amend")


def _validate_stash_request(args: dict, request: dict) -> None:
    action = _validate_choice(args, "action", _STASH_ACTIONS, "push")
    request["action"] = action
    if action == "push":
        if "index" in args:
            raise RuntimeError("stash push does not take index")
        request["include_untracked"] = _validate_bool(args, "include_untracked")
        if "message" in args:
            request["message"] = _validate_text(
                args["message"], "stash message", 500, one_line=True
            )
    else:
        if "include_untracked" in args or "message" in args:
            raise RuntimeError(
                f"stash {action} takes only index; include_untracked and message are for push"
            )
        if action == "list":
            if "index" in args:
                raise RuntimeError("stash list does not take index")
        else:
            request["index"] = _validate_int(args, "index", 0, 999, 0)


def _validate_sequencer_request(args: dict, request: dict, operation: str) -> None:
    action = _validate_choice(args, "action", _SEQUENCER_ACTIONS[operation], "start")
    request["action"] = action
    start_fields = _GIT_OPERATION_FIELDS[operation] - {"action"}
    if action != "start":
        extra = sorted(start_fields & set(args))
        if extra:
            raise RuntimeError(f"{operation} {action} takes no " + ", ".join(extra))
    elif operation == "rebase":
        request["onto"] = _validate_revision(args.get("onto"), "onto")
    elif operation == "merge":
        _validate_merge_request(args, request)
    else:
        _validate_cherry_pick_request(args, request)


def _validate_merge_request(args: dict, request: dict) -> None:
    request["ref"] = _validate_revision(args.get("ref"), "ref")
    request["ff_mode"] = _validate_choice(
        args, "ff_mode", frozenset({"default", "ff_only", "no_ff"}), "default"
    )
    if "message" in args:
        if request["ff_mode"] == "ff_only":
            raise RuntimeError("a fast-forward-only merge takes no message")
        request["message"] = _validate_text(args["message"], "merge message", 10_000)


def _validate_cherry_pick_request(args: dict, request: dict) -> None:
    commits = args.get("commits")
    if not isinstance(commits, list) or not 1 <= len(commits) <= 50:
        raise RuntimeError("commits must be a list of 1 to 50 revisions")
    request["commits"] = tuple(
        _validate_revision(commit, "commit") for commit in commits
    )


def _validate_tag_request(args: dict, request: dict) -> None:
    request["name"] = _validate_branch(args.get("name"), "name")
    if "target" in args:
        request["target"] = _validate_revision(args["target"], "target")
    if "message" in args:
        request["message"] = _validate_text(args["message"], "tag message", 10_000)


def _validate_network_request(args: dict, request: dict, operation: str) -> None:
    remote = args.get("remote", "origin")
    if remote != "origin":
        raise RuntimeError("network Git operations allow only the origin remote")
    request["remote"] = remote
    if operation == "push":
        request["set_upstream"] = _validate_bool(args, "set_upstream")


def _validate_switch_request(args: dict, request: dict) -> None:
    request["branch"] = _validate_branch(args.get("branch"))
    create = args.get("create", False)
    if not isinstance(create, bool):
        raise RuntimeError("create must be a boolean")
    request["create"] = create
    if "start_point" in args:
        if not create:
            raise RuntimeError("start_point requires create=true")
        request["start_point"] = _validate_start_point(args["start_point"])


def _validate_worktree_add_request(args: dict, request: dict, policy: dict) -> None:
    request["worktree_path"] = _validate_worktree_path(args.get("worktree_path"), policy)
    request["branch"] = _validate_branch(args.get("branch"))
    if "start_point" in args:
        request["start_point"] = _validate_start_point(args["start_point"])


@dataclass(frozen=True)
class _GitIdentity:
    worktree: Path
    git_dir: Path
    common_dir: Path
    linked: bool


def _sanitized_git_env(*, optional_locks: bool = False) -> dict[str, str]:
    path = os.environ.get("PATH") or "/run/current-system/sw/bin:/usr/bin:/bin"
    env = {
        "PATH": path,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }
    if not optional_locks:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _resolve_required_executable(policy: dict, key: str) -> str:
    configured = policy[key]
    resolved = shutil.which(configured)
    if resolved is None or not os.access(resolved, os.X_OK):
        raise RuntimeError(
            f"BROKEN safety boundary: required {key} executable {configured!r} is unavailable"
        )
    return str(Path(resolved).resolve())


def _run_git_discovery(argv: list[str], policy: dict) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=_sanitized_git_env(),
            timeout=policy["discoveryTimeoutMs"] / 1000.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git repository discovery timed out; refusing execution") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Git repository discovery failed closed: {type(exc).__name__}: {exc}"
        ) from exc


def _discover_git_identity(repo_path: Path, policy: dict) -> _GitIdentity:
    """Resolve and authenticate a normal or linked Git worktree."""
    if not repo_path.is_absolute():
        raise RuntimeError("repo_path must be absolute")
    lexical = Path(os.path.normpath(str(repo_path)))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"repo_path does not resolve to a directory: {lexical}") from exc
    if lexical != resolved:
        raise RuntimeError("repo_path must be canonical and must not traverse symlinks")
    if not resolved.is_dir() or not _path_within(resolved, policy["repositoryRoots"]):
        raise RuntimeError("repo_path is outside approved repository roots")

    git = _resolve_required_executable(policy, "gitExecutable")
    proc = _run_git_discovery(
        [
            git,
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(resolved),
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        policy,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "repo_path is not a trustworthy Git worktree: "
            + (proc.stderr or "git rev-parse failed").strip()[:300]
        )
    lines = (proc.stdout or "").splitlines()
    if len(lines) != 3 or any(not line.startswith("/") for line in lines):
        raise RuntimeError("Git repository discovery returned malformed paths")
    top, git_dir, common_dir = (Path(line).resolve(strict=False) for line in lines)
    if top != resolved:
        raise RuntimeError("repo_path must name the repository top-level exactly")
    if not _path_within(git_dir, policy["metadataRoots"]) or not _path_within(
        common_dir, policy["metadataRoots"]
    ):
        raise RuntimeError("Git metadata resolves outside approved metadata roots")
    if not git_dir.exists() or not common_dir.exists():
        raise RuntimeError("Git metadata path does not exist")
    local_config = common_dir / "config"
    if not local_config.is_file() or local_config.is_symlink():
        raise RuntimeError("Git repository config must be a regular non-symlink file")

    linked = git_dir != common_dir
    dot_git = resolved / ".git"
    if linked:
        if not dot_git.is_file():
            raise RuntimeError("linked worktree .git pointer is missing or not a file")
        try:
            pointer = dot_git.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("linked worktree .git pointer is unreadable") from exc
        prefix = "gitdir: "
        if not pointer.startswith(prefix):
            raise RuntimeError("linked worktree .git pointer is malformed")
        pointed = Path(pointer[len(prefix) :])
        if not pointed.is_absolute():
            pointed = (resolved / pointed).resolve(strict=False)
        else:
            pointed = pointed.resolve(strict=False)
        if pointed != git_dir:
            raise RuntimeError("linked worktree .git pointer disagrees with Git discovery")

        listing = _run_git_discovery(
            [
                git,
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(resolved),
                "worktree",
                "list",
                "--porcelain",
            ],
            policy,
        )
        if listing.returncode != 0:
            raise RuntimeError("could not verify linked worktree membership")
        listed = {
            Path(line.removeprefix("worktree ")).resolve(strict=False)
            for line in (listing.stdout or "").splitlines()
            if line.startswith("worktree ")
        }
        if resolved not in listed:
            raise RuntimeError("linked worktree is absent from common-dir registry")
    elif (
        dot_git.is_symlink()
        or not dot_git.is_dir()
        or git_dir != dot_git.resolve(strict=False)
    ):
        raise RuntimeError(
            "primary worktree .git must be a real non-symlink directory"
        )

    return _GitIdentity(resolved, git_dir, common_dir, linked)


def _authorize_git_operation(
    identity: _GitIdentity, operation: str, policy: dict
) -> None:
    if (
        identity.worktree in policy["protectedWorktrees"]
        and operation not in _PROTECTED_WORKTREE_OPERATIONS
    ):
        raise RuntimeError(
            f"Git mutation denied in protected primary worktree: {identity.worktree}"
        )


def _validate_remote_url(url: object, policy: dict) -> str:
    if not isinstance(url, str) or not url or len(url) > 2048 or "\x00" in url:
        raise RuntimeError("remote URL is missing or malformed")

    scp = re.fullmatch(
        r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?)",
        url,
    )
    if scp is not None:
        host = scp.group(1).lower()
        if "ssh" not in policy["allowedRemoteSchemes"]:
            raise RuntimeError("SSH remotes are not allowed by Git broker policy")
        if host not in policy["allowedRemoteHosts"]:
            raise RuntimeError(f"remote host is not allowed: {host}")
        return url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in policy["allowedRemoteSchemes"]:
        raise RuntimeError(f"remote scheme is not allowed: {scheme or 'missing'}")
    if parsed.hostname is None or parsed.hostname.lower() not in policy["allowedRemoteHosts"]:
        raise RuntimeError(f"remote host is not allowed: {parsed.hostname!r}")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise RuntimeError("remote URL credentials, query strings, and fragments are denied")
    if scheme == "https":
        if parsed.username is not None or parsed.port not in {None, 443}:
            raise RuntimeError("HTTPS remote must not contain userinfo or a custom port")
    elif scheme == "ssh":
        if parsed.username != "git" or parsed.port not in {None, 22}:
            raise RuntimeError("SSH remote must use the git user and standard port")
    else:
        raise RuntimeError(f"remote scheme is not supported: {scheme}")
    if not re.fullmatch(
        r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path
    ):
        raise RuntimeError("remote URL must name one owner/repository path")
    return url


def _fixed_git_config(policy: dict) -> list[str]:
    credential_helper = _resolve_required_executable(
        policy, "credentialHelperExecutable"
    )
    values = (
        f"core.hooksPath={policy['trustedHooksDir']}",
        "core.fsmonitor=false",
        "core.pager=cat",
        "pager.status=false",
        "pager.diff=false",
        "commit.gpgSign=false",
        "tag.gpgSign=false",
        "credential.helper=",
        f"credential.https://github.com.helper=!{credential_helper} auth git-credential",
        "protocol.allow=never",
        "protocol.ext.allow=never",
        "protocol.file.allow=never",
        "protocol.https.allow=always",
        "protocol.ssh.allow=always",
        "fetch.recurseSubmodules=false",
        "push.recurseSubmodules=no",
        "submodule.recurse=false",
        "remote.origin.uploadpack=git-upload-pack",
        "remote.origin.receivepack=git-receive-pack",
        (
            "core.sshCommand=/run/current-system/sw/bin/ssh -F /dev/null -oBatchMode=yes "
            f"-oUserKnownHostsFile={policy['sshKnownHostsFile']} "
            "-oGlobalKnownHostsFile=/etc/ssh/ssh_known_hosts"
        ),
        "http.proxy=",
        f"user.name={policy['commitUserName']}",
        f"user.email={policy['commitUserEmail']}",
    )
    argv: list[str] = []
    for value in values:
        argv.extend(("-c", value))
    return argv


def _stash_args(request: dict) -> list[str]:
    action = request["action"]
    args = ["stash", action]
    if action == "push":
        if request["include_untracked"]:
            args.append("--include-untracked")
        if request.get("message"):
            args.extend(("--message", request["message"]))
    elif action == "show":
        args.extend(("--patch", f"stash@{{{request['index']}}}"))
    elif action != "list":
        args.append(f"stash@{{{request['index']}}}")
    return args


def _sequencer_args(request: dict) -> list[str]:
    """rebase / merge / cherry-pick: start, or drive an in-progress sequence.

    Never interactive: no --interactive, --exec, or strategy options exist,
    and the sandbox sets GIT_EDITOR=true so --continue cannot block on an
    editor.
    """
    operation = request["operation"]
    command = "cherry-pick" if operation == "cherry_pick" else operation
    action = request["action"]
    if action != "start":
        return [command, f"--{action}"]
    if operation == "rebase":
        return [command, request["onto"]]
    if operation == "merge":
        return _merge_sequencer_args(command, request)
    return [command, *request["commits"]]


def _merge_sequencer_args(command: str, request: dict) -> list[str]:
    args = [command]
    if request["ff_mode"] == "ff_only":
        args.append("--ff-only")
    elif request["ff_mode"] == "no_ff":
        args.append("--no-ff")
    if request.get("message"):
        args.extend(("--message", request["message"]))
    else:
        args.append("--no-edit")
    args.append(request["ref"])
    return args


def _tag_args(request: dict) -> list[str]:
    args = ["tag"]
    if request.get("message"):
        args.extend(("--annotate", "--message", request["message"]))
    args.append(request["name"])
    if request.get("target"):
        args.append(request["target"])
    return args


def _build_git_argv(
    request: dict,
    identity: _GitIdentity,
    policy: dict,
    *,
    remote_url: str | None = None,
    current_branch: str | None = None,
) -> list[str]:
    git = _resolve_required_executable(policy, "gitExecutable")
    argv = [git, *_fixed_git_config(policy)]
    operation = request["operation"]
    if operation in _NETWORK_GIT_OPERATIONS - {"clone"}:
        _append_remote_config(argv, operation, remote_url, policy)
    if operation == "clone":
        _append_clone_argv(argv, request, policy)
        return argv
    argv.extend(("-C", str(identity.worktree)))

    if operation in {"pull", "push"} and current_branch is None:
        raise RuntimeError(f"{operation} requires a validated current branch")

    if operation == "status":
        argv.extend(("status", "--short", "--branch"))
    elif operation == "diff":
        _append_diff_argv(argv, request)
    elif operation == "log":
        argv.extend(("log", "--oneline", "--decorate=no", f"--max-count={request['max_count']}"))
    elif operation == "add":
        _append_add_argv(argv, request)
    elif operation == "unstage":
        argv.extend(("restore", "--staged", "--", *request["paths"]))
    elif operation == "commit":
        argv.append("commit")
        if request["amend"]:
            argv.append("--amend")
        argv.extend(("--message", request["message"]))
    elif operation == "stash":
        argv.extend(_stash_args(request))
    elif operation == "reset":
        argv.extend(("reset", f"--{request['mode']}", request["target"], "--"))
    elif operation in _SEQUENCER_ACTIONS:
        argv.extend(_sequencer_args(request))
    elif operation == "tag":
        argv.extend(_tag_args(request))
    elif operation == "pull":
        _append_pull_argv(argv, current_branch)
    elif operation == "remote_branch_delete":
        argv.extend(
            ("push", "--porcelain", "origin", f":refs/heads/{request['branch']}")
        )
    elif operation == "worktree_remove":
        argv.extend(("worktree", "remove", str(request["worktree_path"])))
    elif operation == "fetch":
        _append_fetch_argv(argv)
    elif operation == "push":
        branch = _validate_branch(current_branch, "current branch")
        argv.extend(("push", "--porcelain"))
        argv.extend(("origin", f"refs/heads/{branch}:refs/heads/{branch}"))
    elif operation == "switch":
        _append_switch_argv(argv, request)
    elif operation == "worktree_add":
        _append_worktree_add_argv(argv, request)
    else:  # Defensive: validation should make this unreachable.
        raise RuntimeError(f"unsupported Git operation: {operation!r}")
    return argv


def _append_remote_config(
    argv: list[str], operation: str, remote_url: str | None, policy: dict
) -> None:
    if remote_url is None:
        raise RuntimeError(f"{operation} requires a validated remote URL")
    _validate_remote_url(remote_url, policy)
    argv.extend(("-c", f"remote.origin.url={remote_url}"))
    argv.extend(("-c", f"remote.origin.pushurl={remote_url}"))
    # Repository config is hidden, so supply origin's standard fetch refspec.
    argv.extend(("-c", "remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*"))


def _append_clone_argv(argv: list[str], request: dict, policy: dict) -> None:
    url = _validate_remote_url(request["url"], policy)
    argv.extend(
        (
            "clone",
            "--no-recurse-submodules",
            "--origin",
            "origin",
            "--",
            url,
            str(request["repo_path"]),
        )
    )


def _append_diff_argv(argv: list[str], request: dict) -> None:
    argv.append("diff")
    if request["staged"]:
        argv.append("--cached")
    if request["paths"]:
        argv.append("--")
        argv.extend(request["paths"])


def _append_add_argv(argv: list[str], request: dict) -> None:
    argv.append("add")
    if request["all"]:
        argv.append("--all")
    else:
        argv.append("--")
        argv.extend(request["paths"])


def _append_pull_argv(argv: list[str], current_branch: str | None) -> None:
    branch = _validate_branch(current_branch, "current branch")
    argv.extend(
        (
            "pull",
            "--ff-only",
            "--no-rebase",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            f"refs/heads/{branch}",
        )
    )


def _append_fetch_argv(argv: list[str]) -> None:
    argv.extend(
        (
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
        )
    )


def _append_switch_argv(argv: list[str], request: dict) -> None:
    argv.append("switch")
    if request["create"]:
        argv.append("--no-track")
        argv.extend(("--create", request["branch"]))
        if request.get("start_point"):
            argv.append(request["start_point"])
    else:
        argv.append(request["branch"])


def _append_worktree_add_argv(argv: list[str], request: dict) -> None:
    argv.extend(
        (
            "worktree",
            "add",
            "--no-track",
            "-b",
            request["branch"],
            str(request["worktree_path"]),
        )
    )
    if request.get("start_point"):
        argv.append(request["start_point"])


def _open_bind_dir(path: Path) -> int:
    """Open a directory without following symlinks and prove it is ``path``."""
    try:
        fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RuntimeError(f"could not open sandbox target {path}: {exc}") from exc
    try:
        actual = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"could not verify sandbox target {path}: {exc}") from exc
    if actual != str(path):
        os.close(fd)
        raise RuntimeError(f"sandbox target {path} resolved to {actual}; refusing")
    return fd


def _sandbox_parent_dirs(path: Path, hidden_roots: tuple[Path, ...]) -> list[Path]:
    for root in hidden_roots:
        if path == root or root in path.parents:
            relative = path.relative_to(root)
            parts = relative.parts[:-1]
            result: list[Path] = []
            current = root
            for part in parts:
                current /= part
                result.append(current)
            return result
    return []


def _build_git_sandbox(
    request: dict,
    identity: _GitIdentity | None,
    policy: dict,
    git_argv: list[str],
    *,
    existing_only: bool = True,
    ssh_auth_sock: str | None = None,
    remote_url: str | None = None,
    bind_fds: list[int] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build a bubblewrap command with only selected Git state writable.

    Directories the broker creates or removes for the caller (clone and
    worktree targets) sit in trees the agent can write, so they are bound by
    an O_NOFOLLOW descriptor verified to name the canonical path: swapping
    the path for a symlink before bubblewrap starts cannot redirect the
    writable mount. The descriptors are appended to ``bind_fds`` for the
    caller to pass to the child and close.
    """
    bwrap = _resolve_required_executable(policy, "bubblewrapExecutable")
    operation = request["operation"]
    networked = operation in _NETWORK_GIT_OPERATIONS
    home = Path.home().resolve()
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    hidden_roots = (home, Path("/tmp"), runtime_dir)
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if networked:
        argv.append("--share-net")
    argv.extend(
        (
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            str(home),
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            str(runtime_dir),
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        )
    )

    binds: list[tuple[str, Path]] = []
    target_binds: list[Path] = []
    if operation == "clone":
        # The only writable path is the empty, broker-created clone target.
        target_binds.append(request["repo_path"])
    else:
        _append_repository_binds(binds, identity, operation)
    if operation in {"worktree_add", "worktree_remove"}:
        target_binds.append(request["worktree_path"])
    trusted_hooks: Path = policy["trustedHooksDir"]
    if existing_only and (not trusted_hooks.is_dir() or trusted_hooks.is_symlink()):
        raise RuntimeError(
            f"BROKEN safety boundary: trusted hooks directory is unavailable: {trusted_hooks}"
        )
    binds.append(("--ro-bind", trusted_hooks))
    if existing_only:
        try:
            empty_config_text = EMPTY_GIT_CONFIG.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "BROKEN safety boundary: broker-owned empty Git config is unavailable"
            ) from exc
        if EMPTY_GIT_CONFIG.is_symlink() or empty_config_text != EMPTY_GIT_CONFIG_TEXT:
            raise RuntimeError(
                "BROKEN safety boundary: broker-owned empty Git config is invalid"
            )

    env = _sanitized_git_env(optional_locks=True)
    env["HOME"] = str(home)
    # --continue and merge commits must never wait on an interactive editor.
    env["GIT_EDITOR"] = "true"
    env["GIT_SEQUENCE_EDITOR"] = "true"
    if operation == "stash":
        # stash takes no caller pathspecs, and its --include-untracked path
        # relies on git's own ":/" pathspec magic, which literal mode disables.
        del env["GIT_LITERAL_PATHSPECS"]
    if networked:
        if remote_url is None:
            raise RuntimeError("network Git operation requires a validated remote URL")
        _validate_remote_url(remote_url, policy)
        is_https = remote_url.startswith("https://")
        # HTTPS Git authenticates only with the host credential-helper wrapper
        # and its token directory. No user config or secret-service bus is mounted.
        credential_paths = (
            (policy["githubHostTokenDir"],)
            if is_https
            else (policy["sshKnownHostsFile"],)
        )
        for path in credential_paths:
            if existing_only and not path.exists():
                raise RuntimeError(
                    f"BROKEN safety boundary: required credential path is unavailable: {path}"
                )
            binds.append(("--ro-bind", path))
        if not is_https:
            socket_value = (
                ssh_auth_sock if ssh_auth_sock is not None else os.environ.get("SSH_AUTH_SOCK")
            )
            if not isinstance(socket_value, str) or not socket_value.startswith("/"):
                raise RuntimeError("BROKEN safety boundary: SSH_AUTH_SOCK is unavailable")
            socket_path = Path(socket_value).resolve(strict=False)
            expected_socket_root = Path(f"/run/user/{os.getuid()}")
            if not _path_within(socket_path, (expected_socket_root,)):
                raise RuntimeError(
                    "BROKEN safety boundary: SSH_AUTH_SOCK is outside the user runtime dir"
                )
            if existing_only and not socket_path.exists():
                raise RuntimeError("BROKEN safety boundary: SSH_AUTH_SOCK does not exist")
            binds.append(("--ro-bind", socket_path))
            env["SSH_AUTH_SOCK"] = str(socket_path)

    seen_dirs: set[Path] = set()
    for _mode, path in [*binds, *(("--bind", target) for target in target_binds)]:
        for parent in _sandbox_parent_dirs(path, hidden_roots):
            if parent not in seen_dirs:
                argv.extend(("--dir", str(parent)))
                seen_dirs.add(parent)
    seen_binds: set[tuple[str, Path]] = set()
    for mode, path in binds:
        entry = (mode, path)
        if entry in seen_binds:
            continue
        argv.extend((mode, str(path), str(path)))
        seen_binds.add(entry)
    for target in target_binds:
        if not existing_only:
            argv.extend(("--bind", str(target), str(target)))
            continue
        if bind_fds is None:
            raise RuntimeError("BROKEN safety boundary: target bind needs a descriptor sink")
        fd = _open_bind_dir(target)
        bind_fds.append(fd)
        argv.extend(("--bind-fd", str(fd), str(target)))
    # A repository's local config can name arbitrary filter/diff commands and
    # URL rewrites. Overlay it only inside the sandbox after mounting Git
    # metadata, so structured operations cannot become arbitrary execution or
    # silently select a different transport. Command-line config above supplies
    # the complete broker-owned runtime policy.
    if identity is None:
        # A fresh clone has no repository-local config yet; git writes it.
        argv.extend(("--chdir", str(request["repo_path"]), "--", *git_argv))
        return argv, env
    argv.extend(
        ("--ro-bind", str(EMPTY_GIT_CONFIG), str(identity.common_dir / "config"))
    )
    argv.extend(("--chdir", str(identity.worktree), "--", *git_argv))
    return argv, env


def _append_repository_binds(
    binds: list[tuple[str, Path]], identity: _GitIdentity | None, operation: str
) -> None:
    if identity is None:
        raise RuntimeError(f"{operation} requires a discovered Git identity")
    worktree_mode = "--ro-bind" if operation in _READ_ONLY_WORKTREE_OPERATIONS else "--bind"
    binds.append((worktree_mode, identity.worktree))
    binds.append(("--bind", identity.common_dir))
    if not _path_within(identity.git_dir, (identity.common_dir,)):
        binds.append(("--bind", identity.git_dir))


def _network_git_context(
    request: dict, identity: _GitIdentity, policy: dict
) -> tuple[str | None, str | None]:
    operation = request["operation"]
    if operation not in _NETWORK_GIT_OPERATIONS - {"clone"}:
        return None, None
    git = _resolve_required_executable(policy, "gitExecutable")
    url_argv = [
        git,
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(identity.worktree),
        "remote",
        "get-url",
    ]
    if operation in {"push", "remote_branch_delete"}:
        url_argv.append("--push")
    url_argv.extend(("--all", "origin"))
    remote = _run_git_discovery(url_argv, policy)
    if remote.returncode != 0:
        raise RuntimeError(
            "could not resolve origin remote: "
            + (remote.stderr or "git remote get-url failed").strip()[:300]
        )
    urls = [line.strip() for line in (remote.stdout or "").splitlines() if line.strip()]
    if len(urls) != 1:
        raise RuntimeError("origin must resolve to exactly one remote URL")
    remote_url = _validate_remote_url(urls[0], policy)

    branch: str | None = None
    if operation in {"pull", "push"}:
        branch = _discover_current_branch(git, identity, policy, operation)
    return remote_url, branch


def _discover_current_branch(
    git: str, identity: _GitIdentity, policy: dict, operation: str
) -> str:
    current = _run_git_discovery(
        [
            git,
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(identity.worktree),
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ],
        policy,
    )
    if current.returncode != 0:
        raise RuntimeError(f"{operation} requires a non-detached current branch")
    return _validate_branch((current.stdout or "").strip(), "current branch")


def _dcg_command_for_git_request(request: dict) -> str:
    """Render the typed request as an equivalent command for dcg analysis."""
    argv = ["git", "-C", str(request["repo_path"])]
    operation = request["operation"]
    if operation == "status":
        argv.append("status")
    elif operation == "diff":
        _append_dcg_diff_args(argv, request)
    elif operation == "log":
        argv.extend(("log", f"--max-count={request['max_count']}"))
    elif operation == "add":
        argv.append("add")
        argv.extend(("--all",) if request["all"] else ("--", *request["paths"]))
    elif operation == "commit":
        argv.append("commit")
        if request["amend"]:
            argv.append("--amend")
        argv.extend(("--message", request["message"]))
    elif operation == "fetch":
        argv.extend(("fetch", "origin"))
    elif operation == "push":
        argv.extend(("push", "origin"))
    elif operation == "switch":
        _append_dcg_switch_args(argv, request)
    elif operation == "worktree_add":
        _append_dcg_worktree_add_args(argv, request)
    elif operation == "unstage":
        argv.extend(("restore", "--staged", "--", *request["paths"]))
    elif operation == "stash":
        argv.extend(_stash_args(request))
    elif operation == "reset":
        argv.extend(("reset", f"--{request['mode']}", request["target"]))
    elif operation in _SEQUENCER_ACTIONS:
        argv.extend(_sequencer_args(request))
    elif operation == "tag":
        argv.extend(_tag_args(request))
    elif operation == "clone":
        argv = ["git", "clone", request["url"], str(request["repo_path"])]
    elif operation == "pull":
        argv.extend(("pull", "--ff-only", "origin"))
    elif operation == "remote_branch_delete":
        argv.extend(("push", "origin", "--delete", request["branch"]))
    elif operation == "worktree_remove":
        argv.extend(("worktree", "remove", str(request["worktree_path"])))
    else:
        raise RuntimeError(f"unsupported Git operation: {operation!r}")
    return shlex.join(argv)


def _append_dcg_diff_args(argv: list[str], request: dict) -> None:
    argv.append("diff")
    if request["staged"]:
        argv.append("--cached")
    if request["paths"]:
        argv.extend(("--", *request["paths"]))


def _append_dcg_switch_args(argv: list[str], request: dict) -> None:
    argv.append("switch")
    if request["create"]:
        argv.extend(("--create", request["branch"]))
        if request.get("start_point"):
            argv.append(request["start_point"])
    else:
        argv.append(request["branch"])


def _append_dcg_worktree_add_args(argv: list[str], request: dict) -> None:
    argv.extend(
        (
            "worktree",
            "add",
            "-b",
            request["branch"],
            str(request["worktree_path"]),
        )
    )
    if request.get("start_point"):
        argv.append(request["start_point"])


def _prepare_worktree_target(request: dict, policy: dict) -> Path | None:
    if request["operation"] != "worktree_add":
        return None
    _reject_nested_target(request["worktree_path"], policy["worktreeCreationRoots"], policy)
    return _create_empty_target(request["worktree_path"], "worktree_path")


def _prepare_clone_target(request: dict, policy: dict) -> Path:
    target: Path = request["repo_path"]
    if (
        not _path_within(target, policy["repositoryRoots"])
        or target in policy["repositoryRoots"]
        or target in policy["protectedWorktrees"]
    ):
        raise RuntimeError("clone repo_path must be a new directory below an approved repository root")
    _reject_nested_target(target, policy["repositoryRoots"], policy)
    return _create_empty_target(target, "repo_path")


def _check_removable_worktree(identity: _GitIdentity, target: Path, policy: dict) -> None:
    """worktree_remove may only remove another linked worktree of this repository."""
    if target == identity.worktree:
        raise RuntimeError("worktree_remove must run from a different worktree than the one removed")
    if target in policy["protectedWorktrees"]:
        raise RuntimeError("worktree_remove refuses a protected worktree")
    entries = _registered_worktrees(identity, policy)
    # The first entry is the primary worktree, which git refuses to remove.
    if target not in entries[1:]:
        raise RuntimeError("worktree_path is not a linked worktree of this repository")


def _registered_worktrees(identity: _GitIdentity, policy: dict) -> list[Path]:
    git = _resolve_required_executable(policy, "gitExecutable")
    listing = _run_git_discovery(
        [git, "-c", "core.hooksPath=/dev/null", "-C", str(identity.worktree),
         "worktree", "list", "--porcelain"],
        policy,
    )
    if listing.returncode != 0:
        raise RuntimeError("could not list worktrees of this repository")
    return [
        Path(line.removeprefix("worktree ")).resolve(strict=False)
        for line in (listing.stdout or "").splitlines()
        if line.startswith("worktree ")
    ]


def _finish_worktree_remove(
    identity: _GitIdentity, target: Path, policy: dict, rc: int, stderr: str
) -> tuple[int, str]:
    """Remove the emptied mount point git could not rmdir inside the sandbox.

    The sandbox bind-mounts the target, so git deletes its contents and
    administrative entry but fails on the final rmdir. Success is judged from
    state, not from git's message: the target must be an empty real directory
    that the repository no longer lists. A refusal (dirty worktree, locked)
    leaves files or the registration in place and stays a failure.
    """
    if rc == 0 or not target.is_dir() or target.is_symlink():
        return rc, stderr
    try:
        if any(target.iterdir()) or target in _registered_worktrees(identity, policy):
            return rc, stderr
        target.rmdir()
    except (OSError, RuntimeError):
        return rc, stderr
    return 0, "[git-broker] worktree removed"


def _set_upstream(identity: _GitIdentity, branch: str | None, policy: dict) -> str:
    """Record origin/<branch> as upstream after a successful push.

    Runs outside the sandbox because the sandbox overlays the repository
    config read-only. Only the two fixed tracking keys for an already
    validated branch name are written.
    """
    branch = _validate_branch(branch, "current branch")
    git = _resolve_required_executable(policy, "gitExecutable")
    for key, value in (
        (f"branch.{branch}.remote", "origin"),
        (f"branch.{branch}.merge", f"refs/heads/{branch}"),
    ):
        proc = _run_git_discovery(
            [git, "-c", "core.hooksPath=/dev/null", "-C", str(identity.worktree),
             "config", "--local", "--replace-all", key, value],
            policy,
        )
        if proc.returncode != 0:
            return (
                "[git-broker] push succeeded but recording upstream failed: "
                + (proc.stderr or "git config failed").strip()[:300]
            )
    return f"[git-broker] upstream set: {branch} -> origin/{branch}"


def _reject_nested_target(target: Path, roots: tuple[Path, ...], policy: dict) -> None:
    """A new checkout must not land inside Git metadata or another checkout."""
    if ".git" in target.parts or _path_within(target, policy["protectedWorktrees"]):
        raise RuntimeError("target must not be inside Git metadata or a protected worktree")
    for ancestor in target.parents:
        if ancestor in roots or not _path_within(ancestor, roots):
            break
        if (ancestor / ".git").exists() or (ancestor / ".git").is_symlink():
            raise RuntimeError(f"target must not be nested inside the repository at {ancestor}")


def _create_empty_target(target: Path, field: str) -> Path:
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"{field} already exists; refusing to overwrite it")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{field} parent must already exist") from exc
    if parent != target.parent:
        raise RuntimeError(f"{field} parent must not traverse symlinks")
    try:
        target.mkdir(mode=0o700)
    except OSError as exc:
        raise RuntimeError(f"could not create worktree target: {exc}") from exc
    if target.resolve(strict=True) != target or target.is_symlink():
        try:
            target.rmdir()
        except OSError as exc:
            raise RuntimeError(
                "created noncanonical worktree target and could not remove unsafe worktree target"
            ) from exc
        raise RuntimeError("created worktree target did not remain canonical")
    return target


def _pipeline_subcommands(cmd: str) -> list[str]:
    """Split a full command into operator-separated subcommands for matching.

    Split on shell operators `&&`, `||`, `;`, `|`, `&`, newlines (but not `||`
    inside quotes). This approximates Claude Code's bashPermissions pipeline
    match — any subcommand matching a deny pattern rejects the whole command.
    """
    # Simple split on any of the operators. Respects quoting via shlex.
    parts: list[str] = []
    current: list[str] = []
    i = 0
    s = cmd
    in_single = False
    in_double = False
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and not in_single:
            current.append(s[i : i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            # check 2-char operators first
            if s[i : i + 2] in ("&&", "||"):
                parts.append("".join(current).strip())
                current = []
                i += 2
                continue
            if ch in (";", "|", "&", "\n"):
                parts.append("".join(current).strip())
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


LEGACY_DCG_PATH = "/home/jonathan/.local/bin/dcg"
DCG_PATH = os.environ.get("DCG_PATH") or shutil.which("dcg") or LEGACY_DCG_PATH
DCG_TIMEOUT_S = 5.0
DCG_CONFIG_TIMEOUT_S = 5.0

_DCG_CONFIG_OUTCOME = re.compile(
    r"^\s*-\s*(system|user|automatic project)\s*:\s*(.*?)\s*"
    r"\[\s*([^\];]+?)\s*;[^\]]*\]\s*(.*)$"
)


def _broken_boundary(detail: str) -> str:
    return (
        "BROKEN safety boundary: git-broker could not obtain a trustworthy "
        f"policy verdict, so it refused execution. Fault: {detail}"
    )


def _check_dcg_config(path: str) -> Optional[str]:
    """Return a refusal unless dcg's user policy is provably loaded."""
    try:
        proc = subprocess.run(
            [path, "config"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DCG_CONFIG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _broken_boundary(
            f"`dcg config` did not answer within {DCG_CONFIG_TIMEOUT_S}s"
        )
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _broken_boundary(
            f"`dcg config` could not run: {type(exc).__name__}: {exc}"
        )
    if proc.returncode != 0:
        return _broken_boundary(
            f"`dcg config` exited {proc.returncode}: {(proc.stderr or '').strip()[:160]}"
        )

    outcomes: list[tuple[str, str, str, str]] = []
    for line in (proc.stdout or "").splitlines():
        match = _DCG_CONFIG_OUTCOME.match(line)
        if match:
            outcomes.append(
                (
                    match.group(1),
                    match.group(2).strip(),
                    match.group(3).strip().lower(),
                    match.group(4).strip(),
                )
            )
    if not outcomes:
        return _broken_boundary(
            "`dcg config` did not report config-file outcomes; user policy load "
            "state is unknown"
        )
    invalid = [outcome for outcome in outcomes if outcome[2] == "invalid"]
    if invalid:
        scope, config_path, _state, detail = invalid[0]
        return _broken_boundary(
            f"dcg's {scope} config at {config_path!r} failed to parse: "
            f"{detail[:200] or 'see `dcg config`'}"
        )
    user = [outcome for outcome in outcomes if outcome[0] == "user"]
    if not user:
        return _broken_boundary("`dcg config` reported no user config outcome")
    _scope, config_path, state, _detail = user[0]
    if state != "loaded":
        return _broken_boundary(
            f"dcg's user config at {config_path!r} is {state.upper()}, not loaded"
        )
    return None


def _check_denied_by_dcg(command: str, dcg_path: str | None = None) -> Optional[str]:
    """Ask dcg (destructive-command guard) whether the command should be blocked.

    dcg speaks Claude-Code hook JSON: reads tool-invocation payload from stdin,
    writes a JSON object with `hookSpecificOutput.permissionDecision` to stdout
    (preceded by decorative text when blocking). Returns a refusal on deny or
    on any unavailable, malformed, timed-out, or indeterminate policy state.
    Only a successful allow with the user config proven loaded returns None.
    """
    path = dcg_path or DCG_PATH
    if not os.path.exists(path) or not os.access(path, os.X_OK):
        return _broken_boundary(f"dcg is missing or not executable at {path!r}")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    try:
        proc = subprocess.run(
            [path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=DCG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _broken_boundary(f"dcg did not answer within {DCG_TIMEOUT_S}s")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _broken_boundary(f"dcg could not run: {type(exc).__name__}: {exc}")

    deny_reason: Optional[str] = None
    ask_reason: Optional[str] = None
    saw_allow = False
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return _broken_boundary(f"dcg emitted malformed JSON: {line[:120]!r}")
        hook_out = (data.get("hookSpecificOutput") or {}) if isinstance(data, dict) else {}
        decision = hook_out.get("permissionDecision")
        if decision == "deny":
            rule = hook_out.get("ruleId") or "unknown"
            reason = (hook_out.get("permissionDecisionReason") or "").strip()
            deny_reason = f"dcg: blocked by rule {rule}" + (f": {reason}" if reason else "")
        elif decision == "ask":
            ask_reason = _broken_boundary(
                "dcg returned `ask`, meaning it did not clear the command"
            )
        elif decision == "allow":
            saw_allow = True
        else:
            return _broken_boundary(
                f"dcg emitted unknown permissionDecision {decision!r}"
            )
    if deny_reason is not None:
        return deny_reason
    if ask_reason is not None:
        return ask_reason
    if (proc.stdout or "").strip() and not saw_allow:
        return _broken_boundary(
            f"dcg output was not a readable decision: {(proc.stdout or '').strip()[:160]!r}"
        )
    if proc.returncode != 0:
        return _broken_boundary(
            f"dcg exited {proc.returncode} without a deny: {(proc.stderr or '').strip()[:160]}"
        )
    config_fault = _check_dcg_config(path)
    if config_fault is not None:
        return config_fault
    return None


# nixos-config worktree push gate.
#
# Two recent PRs deployed broken changes to dellan despite the SessionStart
# hook's HARD RULE and the nixos-config-dev / nixos-agent-testing skills:
#
#   PR #61 (research-agent → microvm): agent ran `nix run .#feature-vm` and
#     SSH-smoked it, but stubbed `modules/nixos/feature-vm.nix` to make boot
#     succeed (missing virtiofsd share-source dirs). Smoke ran against a
#     mutated artefact, not the prod path. Inner microvm `networking.useDHCP`
#     defaulted false → no resolver → sshd never started → research-agent
#     unreachable post-deploy.
#
#   PR #57 (kitty ctrl+shift+c strip newline): agent ran only the automated
#     gate (`nix build .#checks.x86_64-linux.dellan-vm`) whose assertion was
#     grep-the-rendered-config — never launched kitty, never pressed the key.
#     `pass_selection_to_program sh -c '… kitten clipboard'` fails silently
#     at runtime because the child has no controlling tty for OSC52.
#
# The shared failure mode is the agent equating "build green" with "feature
# works" — and the SessionStart prose (which both sessions had in context)
# did not gate behaviour. This MCP gate is the enforcement layer that the
# prose isn't.
#
# Design:
#   - Fires on `git push` from a `~/Repos/nixos-config-worktrees/<slug>` cwd
#     (or `cd <worktree> && git push`).
#   - Requires a `Pre-push checklist:` block in the HEAD commit message.
#   - For `Type: pure-data` → diff verified empty of risky markers.
#   - For `Type: risky` → all required fields filled AND the
#     `feature-vm.nix modified: <yes/no>` claim is mechanically cross-checked
#     against the actual diff (catches incident 1's hollow smoke), AND the
#     `Rebased on origin/main: yes` claim is verified via merge-base.
#   - Override: `Override: [skip-vm-gate] <reason>` line in the commit message.
#     Visible in `git log` post-merge for retrospective review.

NIXOS_WORKTREES_ROOT = "/home/jonathan/Repos/nixos-config-worktrees"

# The gate detects a "nixos-config repo" by remote URL, not by filesystem
# path — so the same gate fires whether the worktree lives at the canonical
# `~/Repos/nixos-config-worktrees/<slug>/` location or anywhere else
# (scratch clone in `/tmp`, secondary worktree under `~/projects`, etc.).
# Patterns match the tail `[:/]owner/repo(.git)?` of a remote URL.
_NIXOS_REPO_REMOTE_PATTERNS: list["re.Pattern[str]"] = [
    re.compile(r"[:/]jonathanmoregard/nixos-config(?:\.git)?/?$"),
]

# Heuristic "this diff has branching / runtime / multistep behaviour" markers.
# Order matters only for the rejection-message hit list (first-match labels).
_NIXOS_RISKY_MARKERS = re.compile(
    r"\bmkIf\b"
    r"|\boptionals\b"
    r"|\bExecStart"
    r"|writeShellApplication"
    r"|writeShellScriptBin"
    r"|pass_selection_to_program"
    r"|\bmicrovm\b"
    r"|\bvirtiofsd\b"
    r"|networking\.useDHCP"
    r"|activationScripts?"
    r"|systemd\.(?:services|timers|user\.services)\."
    r"|\bsh\s+-c\s+"
    r"|\bbash\s+-c\s+"
)

_NIXOS_CHECKLIST_HEADER = re.compile(r"^Pre-push checklist:\s*$", re.MULTILINE)
# Field lines are top-level only (anchored `^-\s`, NOT `^\s*-\s`). Indented
# sub-bullets inside a multi-line `Behavioural evidence` value must not
# overwrite the parent field map.
_NIXOS_CHECKLIST_FIELD = re.compile(r"^-\s*([^:\n]+?)\s*:\s*(.*)$")
_NIXOS_OVERRIDE = re.compile(r"^Override:\s*\[skip-vm-gate\]\s*(\S.+)$", re.MULTILINE)
# `<placeholder>` template hints (`<list, or 'none'>`, `<cmd + output>`) left
# unfilled by the agent are treated as empty. The angle-brackets are the
# template's hint marker; real values never start with `<` AND end with `>`.
_NIXOS_TEMPLATE_PLACEHOLDER = re.compile(r"^<.*>$")
_NIXOS_FEATURE_VM_FILE_IN_DIFF = re.compile(
    r"^diff --git .*modules/nixos/feature-vm\.nix\b", re.MULTILINE
)

_NIXOS_GATE_HELP = (
    "nixos-config push gate refused this push (remote points at "
    "jonathanmoregard/nixos-config).\n\n"
    "HEAD commit message must contain a 'Pre-push checklist:' block.\n"
    "Template (copy verbatim, then fill values):\n\n"
    "  Pre-push checklist:\n"
    "  - Type: pure-data           # OR 'risky' for branching/multistep/runtime\n"
    "  - Rebased on origin/main: yes\n"
    "  - Local gate: nix build .#checks.x86_64-linux.dellan-vm rc=0\n"
    "  - Interactive smoke (nixos-agent-testing): <yes — cmd + observed | N/A — reason>\n"
    "  - Advisor review (advice-refine-test-loop): <yes — rounds + verdict | N/A — reason>\n"
    "  - feature-vm.nix modified: no    # MUST match actual diff\n"
    "  - Risky markers in diff: <list, or 'none'>\n"
    "  - Behavioural evidence: <what you actually exercised — command + observed output>\n\n"
    "For 'Type: pure-data' (package add, version bump, comment), only Type +\n"
    "brief justification are required; diff is verified to have no risky markers.\n\n"
    "Bypass (audited): add 'Override: [skip-vm-gate] <reason>' to commit message.\n"
)


def _nixos_pipeline_subcommands(command: str) -> list[str]:
    """Pipeline split that also strips a leading '(' from subshell forms.

    The existing `_pipeline_subcommands` correctly splits on `&&`/`;`/etc.
    but doesn't strip `(` from `(cd X && git push)`. We need the inside.
    """
    out: list[str] = []
    for sub in _pipeline_subcommands(command):
        s = sub.strip()
        # strip leading "(" and trailing ")" from subshell wraps
        while s.startswith("("):
            s = s[1:].lstrip()
        while s.endswith(")"):
            s = s[:-1].rstrip()
        if s:
            out.append(s)
    return out


def _nixos_extract_cd_target(command: str) -> Optional[str]:
    """Last `cd <path>` target encountered in the pipeline chain.

    Returns absolute path (with ~ expansion) or None. Quotes around the path
    are stripped. Only `cd` as the leading token of a subcommand counts —
    a `cd` mid-command (rare) is ignored.
    """
    last: Optional[str] = None
    for sub in _nixos_pipeline_subcommands(command):
        m = re.match(r'^cd\s+(.+?)\s*$', sub)
        if not m:
            continue
        raw = m.group(1).strip()
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1]
        try:
            last = os.path.abspath(os.path.expanduser(raw))
        except (ValueError, OSError):
            continue
    return last


def _remote_is_nixos_config(url: str) -> bool:
    """True if a git remote URL points at the nixos-config repo.

    Tail-match against `_NIXOS_REPO_REMOTE_PATTERNS` so the same predicate
    works for `git@github.com:owner/repo.git`, `https://github.com/owner/repo`,
    `ssh://git@github.com/owner/repo`, etc.
    """
    url = (url or "").strip()
    if not url:
        return False
    return any(p.search(url) for p in _NIXOS_REPO_REMOTE_PATTERNS)


def _repo_has_nixos_remote(repo_root: str) -> bool:
    """True if the repo at `repo_root` has any remote whose URL matches nixos-config.

    Checks ALL configured remotes (fetch + push entries), not just origin,
    so forks / mirrors with a different push target still gate correctly.
    """
    rc, out, _ = _nixos_git(repo_root, ["remote", "-v"])
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _remote_is_nixos_config(parts[1]):
            return True
    return False


def _candidate_repo_paths(command: str) -> list[str]:
    """Candidate working-dir paths to probe for a git repo.

    Order:
      1. Last `cd <path>` target in the command's pipeline chain
      2. `git -C <path>` flag
      3. MCP server's own cwd (Claude Code launched from inside the repo)
    """
    candidates: list[str] = []
    cd_target = _nixos_extract_cd_target(command)
    if cd_target:
        candidates.append(cd_target)
    m = re.search(r"\bgit\s+(?:[^\n;|&]*?\s+)?-C\s+(\S+)", command)
    if m:
        raw = m.group(1).strip("\"'")
        try:
            candidate = os.path.abspath(os.path.expanduser(raw))
        except (ValueError, OSError):
            candidate = None
        if candidate is not None:
            candidates.append(candidate)
    try:
        current_directory = os.getcwd()
    except OSError:
        current_directory = None
    if current_directory is not None:
        candidates.append(current_directory)
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _nixos_resolve_target_repo(command: str) -> Optional[str]:
    """Return the working-tree top-level if any candidate path resolves to a
    nixos-config clone (identified by remote URL match).

    Detection is by remote URL, NOT by filesystem path — so a worktree at
    `~/scratch/foo/`, `/tmp/clones/nixos-config/`, or anywhere outside the
    canonical `~/Repos/nixos-config-worktrees/` tree still gates as long as
    one of its configured remotes points at jonathanmoregard/nixos-config.
    """
    for path in _candidate_repo_paths(command):
        rc, toplevel, _ = _nixos_git(path, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            continue
        repo_root = toplevel.strip()
        if not repo_root:
            continue
        if _repo_has_nixos_remote(repo_root):
            return repo_root
    return None


def _nixos_command_pushes(command: str) -> bool:
    """True if a `git push` is present (excluding --dry-run)."""
    for sub in _nixos_pipeline_subcommands(command):
        if not re.search(r"\bgit\s+(?:-C\s+\S+\s+)?(?:[\w-]+\s+)*push\b", sub):
            continue
        if re.search(r"(?:^|\s)--dry-run(?:\s|$)", sub):
            continue
        return True
    return False


def _nixos_git(worktree: str, args: list[str], timeout_s: float = 10.0) -> tuple[int, str, str]:
    # Capture bytes, decode permissively. `text=True` raises UnicodeDecodeError
    # on `git diff` output for commits that add/modify binary blobs (e.g. .age
    # files), which would otherwise crash the whole gate before it can run.
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                worktree,
                *args,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=timeout_s,
            env=_sanitized_git_env(),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, stdout, stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return -1, "", str(e)


def _nixos_parse_checklist(commit_msg: str) -> Optional[dict[str, str]]:
    """Extract `Pre-push checklist:` field map. None if header missing.

    Block ends at the first non-field, non-blank, non-comment line after the
    header, or at end-of-message. Keys lowercased + stripped.
    """
    if not _NIXOS_CHECKLIST_HEADER.search(commit_msg):
        return None
    fields: dict[str, str] = {}
    in_block = False
    for line in commit_msg.splitlines():
        if _NIXOS_CHECKLIST_HEADER.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        m = _NIXOS_CHECKLIST_FIELD.match(line)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            # Drop inline `# comment` tails so the template's hint text doesn't
            # poison values when the agent left it untrimmed.
            val = re.sub(r"\s+#.*$", "", val).strip()
            fields[key] = val
            continue
        # First unrelated line ends the block.
        break
    return fields


def _check_nixos_push(command: str) -> Optional[str]:
    """Gate `git push` of a nixos-config branch.

    Detection: any candidate path (from `cd`/`-C`/cwd) that resolves to a
    git working tree whose remotes include a URL matching nixos-config.
    Worktree location is NOT part of the check.

    Returns rejection text or None. Pure-data pushes with a minimal trailer
    are accepted. Risky pushes require the full trailer AND the mechanical
    cross-checks to agree with the diff.
    """
    if not _nixos_command_pushes(command):
        return None
    worktree = _nixos_resolve_target_repo(command)
    if not worktree:
        return None
    rc, branch, _ = _nixos_git(worktree, ["branch", "--show-current"])
    if rc == 0 and branch.strip() == "main":
        # K01: the broker always pushes the current branch to its same name,
        # so this lands on main — which auto-deploys as root, and whose
        # upstream protection does not bind admins. No checklist or
        # Override applies; main changes go through a pull request.
        return (
            "nixos push gate: this push would update nixos-config `main` "
            "directly. main auto-deploys as root and its upstream protection "
            "does not bind admins. Push a feature branch and open a pull "
            "request instead."
        )

    rc, msg, _ = _nixos_git(worktree, ["log", "-1", "--format=%B"])
    if rc != 0:
        return None  # No HEAD — let git itself fail with a clear error.

    override = _NIXOS_OVERRIDE.search(msg)
    if override:
        sys.stderr.write(
            f"[git-broker] nixos push gate: bypass via Override: [skip-vm-gate] "
            f"{override.group(1).strip()[:120]!r}\n"
        )
        return None

    fields = _nixos_parse_checklist(msg)
    if fields is None:
        return _NIXOS_GATE_HELP

    # Diff against origin/main (falls back to local main, then empty).
    rc, diff, _ = _nixos_git(
        worktree,
        ["diff", "--no-ext-diff", "--no-textconv", "origin/main...HEAD"],
        timeout_s=15.0,
    )
    if rc != 0 or not diff:
        rc2, diff2, _ = _nixos_git(
            worktree,
            ["diff", "--no-ext-diff", "--no-textconv", "main...HEAD"],
            timeout_s=15.0,
        )
        if rc2 == 0:
            diff = diff2
    diff_has_risky = bool(_NIXOS_RISKY_MARKERS.search(diff)) if diff else False
    diff_touches_fv = bool(_NIXOS_FEATURE_VM_FILE_IN_DIFF.search(diff)) if diff else False

    type_ = fields.get("type", "").lower()

    if type_ == "pure-data":
        # Report both incompatibilities if they coexist — the markers-only
        # message hides the structural feature-vm.nix signal which is the
        # PR #61 anti-pattern tell.
        reasons: list[str] = []
        if diff_has_risky:
            hits = sorted({m.group(0) for m in _NIXOS_RISKY_MARKERS.finditer(diff)})
            reasons.append(
                "diff contains risky markers: " + ", ".join(hits[:8])
            )
        if diff_touches_fv:
            reasons.append(
                "diff touches modules/nixos/feature-vm.nix (always 'Type: risky')"
            )
        if reasons:
            return (
                "nixos push gate: checklist claims 'Type: pure-data' "
                "but " + "; ".join(reasons) + ".\n"
                "Update the checklist to 'Type: risky' and fill remaining fields, "
                "OR remove the runtime/branching change from this PR."
            )
        return None

    if type_ != "risky":
        return (
            f"nixos push gate: checklist 'Type:' must be "
            f"'pure-data' or 'risky' (got: {type_!r}).\n\n{_NIXOS_GATE_HELP}"
        )

    required = [
        "rebased on origin/main",
        "local gate",
        "interactive smoke (nixos-agent-testing)",
        "advisor review (advice-refine-test-loop)",
        "feature-vm.nix modified",
        "risky markers in diff",
        "behavioural evidence",
    ]
    # Field counts as empty when blank OR still holding the template
    # `<...>` placeholder. The latter slips past truthy checks but signals
    # the agent left the template untouched.
    def _empty_or_placeholder(v: str) -> bool:
        return (not v) or bool(_NIXOS_TEMPLATE_PLACEHOLDER.match(v.strip()))
    missing = [k for k in required if _empty_or_placeholder(fields.get(k, ""))]
    if missing:
        return (
            "nixos push gate: missing, empty, or template-placeholder "
            "checklist fields: " + ", ".join(missing) + ".\n\n" + _NIXOS_GATE_HELP
        )

    fv_claim = fields["feature-vm.nix modified"].lower()
    fv_claims_no = fv_claim.startswith("no") or fv_claim in ("n", "false")
    if fv_claims_no and diff_touches_fv:
        return (
            "nixos push gate: checklist claims 'feature-vm.nix "
            "modified: no' but the diff DOES touch modules/nixos/feature-vm.nix. "
            "This is the load-bearing tell for hollow smoke evidence (cf. PR #61: "
            "agent stubbed virtiofsd shares to make boot succeed, smoke ran against "
            "a mutated artefact, not the prod path).\n"
            "If the edit was needed to make the smoke test pass, state 'yes' + "
            "explain why the prod code path was still exercised. If the edit is "
            "unrelated, state 'yes (unrelated: <reason>)'."
        )

    rebased_claim = fields["rebased on origin/main"].lower()
    if rebased_claim.startswith("yes"):
        rc_mb, merge_base, _ = _nixos_git(worktree, ["merge-base", "HEAD", "origin/main"])
        rc_om, origin_main_sha, _ = _nixos_git(worktree, ["rev-parse", "origin/main"])
        if rc_mb == 0 and rc_om == 0:
            if merge_base.strip() != origin_main_sha.strip():
                return (
                    "nixos push gate: checklist claims 'Rebased on "
                    "origin/main: yes' but `git merge-base HEAD origin/main` "
                    f"({merge_base.strip()[:12]}) != `origin/main` "
                    f"({origin_main_sha.strip()[:12]}). Branch is behind main; "
                    "rebase before pushing."
                )

    return None


def _run_argv(
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    rec: Optional[_InFlight] = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, str, str]:
    """Run a prevalidated argv directly; never invoke a shell."""
    timeout_s = max(1, timeout_ms) / 1000.0
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        pass_fds=pass_fds,
    )
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    def signal_group(sig: int) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except ProcessLookupError:
                return
            except (PermissionError, OSError):
                # Fall back to Python's direct child-process signal path.
                group_signal_failed = True
            else:
                group_signal_failed = False
        else:
            group_signal_failed = False
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return
        except (PermissionError, OSError) as exc:
            detail = " after process-group signaling failed" if group_signal_failed else ""
            raise RuntimeError(f"could not terminate Git subprocess{detail}") from exc

    if rec is not None:
        with _in_flight_lock:
            rec.proc = proc
            cancelled_already = rec.cancelled
        if cancelled_already:
            signal_group(signal.SIGTERM)

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return proc.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

    signal_group(signal.SIGTERM)
    try:
        out, err = proc.communicate(timeout=2.0)
        stdout, stderr = out or stdout, err or stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or stdout
        stderr = exc.stderr or stderr
        signal_group(signal.SIGKILL)
        try:
            out, err = proc.communicate(timeout=2.0)
            stdout, stderr = out or stdout, err or stderr
        except subprocess.TimeoutExpired as final_exc:
            stdout = final_exc.stdout or stdout
            stderr = final_exc.stderr or stderr
            sys.stderr.write(
                "[git-broker] Git timeout fallback: pipes remain open; "
                "giving up reaping to keep MCP responsive\n"
            )
            sys.stderr.flush()
    warning = (
        f"[git-broker] Git operation exceeded timeout of {timeout_ms} ms. "
        "Timeout or cancellation is not rollback; inspect repository state before retrying."
    )
    stderr = ((stderr or "") + "\n" + warning).strip()
    return 124, stdout or "", stderr


# High-entropy secret prefixes. If a command's output contains a string
# matching any of these patterns, the matched substring is replaced with
# `[REDACTED:<label>:<len>chars]` before the body is returned to the
# model. Triggered after a real incident where `cat /run/agenix/...`
# placed an Anthropic API key into the assistant's context window.
# Prefixes only — generic high-entropy matching is too false-positive
# prone (hashes, store paths, base64 blobs).
_SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("anthropic-api", re.compile(r"sk-ant-(?:api|oat)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("openai-project", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai-legacy", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\bgh[psoure]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("slack-token", re.compile(r"\bxox[boaprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws-access", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("tavily-key", re.compile(r"\btvly-[A-Za-z0-9_\-]{20,}\b")),
]


_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")
_KEY_END_RE = re.compile(r"-----END (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----")


def _redact_private_keys(body: str) -> str:
    """Redact complete and one-sided armored private-key fragments linearly."""
    out: list[str] = []
    pos = 0
    begin = _KEY_BEGIN_RE.search(body, pos)
    while True:
        end = _KEY_END_RE.search(body, pos)
        if end is not None and (begin is None or end.start() < begin.start()):
            out.append(f"[REDACTED:private-key:{end.end() - pos}chars]")
            pos = end.end()
            if begin is not None and begin.start() < pos:
                begin = _KEY_BEGIN_RE.search(body, pos)
            continue
        if begin is None:
            out.append(body[pos:])
            return "".join(out)
        out.append(body[pos : begin.start()])
        if end is not None and end.start() < begin.end():
            end = _KEY_END_RE.search(body, begin.end())
        stop = end.end() if end else len(body)
        out.append(f"[REDACTED:private-key:{stop - begin.start()}chars]")
        pos = stop
        begin = _KEY_BEGIN_RE.search(body, pos)


def _redact_secrets(body: str) -> str:
    """Replace known-shape secret values with `[REDACTED:<label>:<len>chars]`.

    Runs on every tool output before return. The model sees only the
    redaction marker — never the bytes. False positives are acceptable
    (the model knows what it asked for; the marker tells it the call
    succeeded but the value is gated). False negatives are not (each
    missed pattern is a future incident waiting to happen).
    """
    body = _redact_private_keys(body)
    for label, pat in _SECRET_PATTERNS:
        body = pat.sub(
            lambda m, _label=label: f"[REDACTED:{_label}:{len(m.group(0))}chars]",
            body,
        )
    return body


def _compose_output(rc: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append("--- stderr ---\n" + stderr.rstrip())
    parts.append(f"--- exit {rc} ---")
    combined = "\n".join(parts)
    if len(combined) > OUTPUT_LIMIT_CHARS:
        combined = combined[:OUTPUT_LIMIT_CHARS] + "\n[git-broker] output truncated"
    return _redact_secrets(combined)


def _operation_guide() -> str:
    lines = [
        f"- {operation}: " + (", ".join(sorted(fields)) or "(no extra fields)")
        for operation, fields in _GIT_OPERATION_FIELDS.items()
    ]
    return "\n".join(lines)


TOOLS = [
    {
        "name": "git",
        "description": (
            "Runs one policy-approved Git operation in a validated repository. "
            "The tool accepts structured fields only, never shell text or raw argv. "
            "Every call takes repo_path, operation and optional timeout; each operation "
            "accepts only the extra fields listed for it:\n"
            + _operation_guide()
            + "\nNotes: clone's repo_path is the new checkout directory. push and pull "
            "act on the current branch; push set_upstream records origin "
            "tracking. stash action: push|pop|apply|drop|list|show (index = stash@{n}). "
            "rebase/merge/cherry_pick action: start|continue|abort (and skip for "
            "rebase/cherry_pick); resolve conflicts by editing files, add, then "
            "action=continue. merge ff_mode: default|ff_only|no_ff. reset mode: "
            "soft|mixed (no hard)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the selected Git worktree "
                    "(for clone: the new checkout directory)",
                },
                "operation": {
                    "type": "string",
                    "enum": list(_GIT_OPERATION_FIELDS),
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative paths for add, unstage or diff",
                },
                "all": {"type": "boolean"},
                "message": {"type": "string"},
                "amend": {"type": "boolean"},
                "remote": {"type": "string"},
                "set_upstream": {"type": "boolean"},
                "branch": {"type": "string"},
                "create": {"type": "boolean"},
                "start_point": {"type": "string"},
                "worktree_path": {"type": "string"},
                "action": {"type": "string"},
                "index": {"type": "integer", "minimum": 0},
                "include_untracked": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["soft", "mixed"]},
                "target": {"type": "string"},
                "onto": {"type": "string"},
                "ref": {"type": "string"},
                "ff_mode": {"type": "string", "enum": ["default", "ff_only", "no_ff"]},
                "commits": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
                "url": {"type": "string"},
                "staged": {"type": "boolean"},
                "max_count": {"type": "integer", "minimum": 1, "maximum": 200},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT_MS,
                    "description": "Milliseconds (not seconds); default 120000",
                },
            },
            "required": ["repo_path", "operation"],
            "additionalProperties": False,
        },
    }
]


SERVER_INFO = {"name": "git-broker", "version": "1.2.0"}
CAPABILITIES = {"tools": {"listChanged": False}}


def _write_msg(msg: dict) -> None:
    """Serialized write of one JSON-RPC frame to stdout.

    The lock is essential: under threaded dispatch, two worker threads
    could otherwise interleave JSON bytes mid-frame, corrupting the
    protocol. The cost is one mutex per response. Initialize/tools-list
    on the main thread go through the same lock for the same reason.
    """
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    """Send a response on the synchronous (main-thread) path.

    Init, tools/list, method-not-found, and tools/call dispatch errors
    use this — none of them are tracked in `_in_flight`, so no
    cancellation check applies. Goes through the serialized writer so
    the frame is still atomic relative to worker-thread responses.
    """
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    _write_msg(out)


def _send_response(msg_id, result=None, error=None) -> None:
    """Send the response for an in-flight tools/call, OR silently drop
    it if the request was cancelled.

    Per MCP cancellation spec (2025-06-18 §Cancellation):
        > A receiver SHOULD stop processing the cancelled request and
        > free associated resources. A receiver SHOULD NOT send a
        > response for the cancelled request.

    The cancelled-flag check and the response write must be atomic
    relative to the cancellation handler — if they aren't, the
    handler can flip `cancelled=True` between our check and our
    write, and we'd emit the spurious response that triggers Claude
    Code's transport drop ("Received a response for an unknown
    message ID"). Hold both `_write_lock` (write atomicity) and
    `_in_flight_lock` (registration atomicity) across the decision.

    The `_in_flight` entry is popped here on the success path; on the
    cancel path it's already popped by the cancellation handler.
    """
    with _write_lock:
        with _in_flight_lock:
            rec = _in_flight.pop(msg_id, None)
            if rec is not None and rec.cancelled:
                return
        # NOTE: writing inline rather than calling `_write_msg(...)`
        # is deliberate — `_write_lock` is a non-reentrant
        # `threading.Lock`. Routing through `_write_msg` would
        # self-acquire and deadlock. If a future refactor wants the
        # single chokepoint, switch to `threading.RLock` first.
        out: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _on_cancelled(req_id) -> None:
    """Handle `notifications/cancelled` for an in-flight tool call.

    The cancelled-flag assignment MUST happen under `_in_flight_lock`,
    same lock `_send_response` holds while it decides whether to write
    the response. If the flag flip happens outside the lock, the
    worker can interleave between (cancel-handler's lookup, releasing
    lock) and (cancel-handler's flag-set) — popping the rec, reading
    cancelled=False, and shipping the spurious response that triggers
    Claude Code's transport drop. The kill, by contrast, can stay
    outside the lock — killpg is the syscall-heavy part, and we only
    need it to follow flag-flip in happens-before order, which it does
    by sequenced statement order.

    Three race-safe outcomes (all covered under the same lock):
      1. Request not yet dispatched (handler thread has not started
         Git): `rec.proc is None`, mark `cancelled=True`; the
         handler will observe and exit early via `_send_response`.
      2. Request running bash: SIGTERM the process group (set up by
         `_run` via `start_new_session=True`); `_run`'s normal
         SIGTERM-then-SIGKILL escalation reaps any stragglers.
      3. Request already responded (very late cancel): the id was
         popped by `_send_response`; `rec is None`, silent no-op per
         spec ("MAY ignore if the request is unknown or already
         completed").
    """
    with _in_flight_lock:
        rec = _in_flight.get(req_id)
        if rec is None:
            return
        rec.cancelled = True
        proc = rec.proc
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        except (PermissionError, OSError) as group_exc:
            try:
                proc.terminate()
            except (ProcessLookupError, PermissionError, OSError) as child_exc:
                sys.stderr.write(
                    "[git-broker] cancellation could not terminate Git: "
                    f"group={type(group_exc).__name__}; "
                    f"child={type(child_exc).__name__}\n"
                )
                sys.stderr.flush()


def _dispatch_tools_call(msg_id, params, rec) -> None:
    """Worker-thread body for a tools/call request.

    Runs the typed Git tool synchronously in this thread; on completion,
    funnels through `_send_response`, which suppresses cancelled responses.
    """
    try:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "git":
            _send_response(msg_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
            return
        text = _tool_git(arguments, rec)
        _send_response(msg_id, result={"content": [{"type": "text", "text": text}]})
    except Exception as e:
        _send_response(
            msg_id,
            result={"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True},
        )


def _tool_git(args: dict, rec: Optional[_InFlight] = None) -> str:
    """Authorize and run one structured Git operation inside bubblewrap."""
    config = _load_config()
    policy = _load_git_policy(config)
    request = _validate_git_request(args, policy)

    validator = _resolve_required_executable(policy, "validatorExecutable")
    verdict_command = _dcg_command_for_git_request(request)
    validator_reason = _check_denied_by_dcg(verdict_command, validator)
    if validator_reason is not None:
        return f"[git-broker] {validator_reason}"

    operation = request["operation"]
    if operation == "clone":
        identity = None
        remote_url, current_branch = request["url"], None
    else:
        identity, remote_url, current_branch, denial = _prepare_existing_operation(request, policy)
        if denial is not None:
            return denial

    if operation == "clone":
        created_target = _prepare_clone_target(request, policy)
    else:
        created_target = _prepare_worktree_target(request, policy)
    completed = False
    bind_fds: list[int] = []
    try:
        git_argv = _build_git_argv(
            request,
            identity,
            policy,
            remote_url=remote_url,
            current_branch=current_branch,
        )
        sandbox_argv, env = _build_git_sandbox(
            request,
            identity,
            policy,
            git_argv,
            remote_url=remote_url,
            bind_fds=bind_fds,
        )
        rc, stdout, stderr = _run_argv(
            sandbox_argv, env, request["timeout"], rec, pass_fds=tuple(bind_fds)
        )
        if operation == "worktree_remove":
            rc, stderr = _finish_worktree_remove(identity, request["worktree_path"], policy, rc, stderr)
        completed = rc == 0
        if completed and operation == "push" and request["set_upstream"]:
            stderr = (stderr + "\n" + _set_upstream(identity, current_branch, policy)).strip()
        return _compose_output(rc, stdout, stderr)
    finally:
        for fd in bind_fds:
            os.close(fd)
        if created_target is not None and not completed:
            try:
                created_target.rmdir()
            except OSError:
                # A failed Git operation may have partially populated its
                # target. Never recursively clean it: timeout is not rollback.
                pass


def _prepare_existing_operation(
    request: dict, policy: dict
) -> tuple[_GitIdentity, str | None, str | None, str | None]:
    operation = request["operation"]
    identity = _discover_git_identity(request["repo_path"], policy)
    _authorize_git_operation(identity, operation, policy)
    denial = _check_nixos_push_operation(identity, operation)
    if denial is not None:
        return identity, None, None, f"[git-broker] {denial}"
    if operation == "worktree_remove":
        _check_removable_worktree(identity, request["worktree_path"], policy)
    remote_url, current_branch = _network_git_context(request, identity, policy)
    return identity, remote_url, current_branch, None


def _check_nixos_push_operation(identity: _GitIdentity, operation: str) -> str | None:
    if operation != "push":
        return None
    return _check_nixos_push(
        "git -C " + shlex.quote(str(identity.worktree)) + " push origin"
    )


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _respond(
            msg_id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        # JSON-RPC notification — no response. params.requestId is the
        # id of the in-flight request the peer wants stopped.
        req_id = params.get("requestId")
        if req_id is not None:
            _on_cancelled(req_id)
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        # JSON-RPC requires an `id` on requests. A `tools/call`
        # without one is malformed and would leak an `_InFlight`
        # entry at key None (no later cancel could pop it). Reject
        # before registering. The peer cannot recover a response —
        # they sent a notification-shaped request — but at least we
        # log the protocol violation and don't grow memory.
        if msg_id is None:
            sys.stderr.write("[git-broker] tools/call missing id; dropping\n")
            sys.stderr.flush()
            return
        # Reject id collision so a re-used id can't orphan a prior
        # in-flight rec. JSON-RPC 2.0 §5: "An identifier established
        # by the Client that MUST contain a String, Number, or NULL".
        # Sane clients don't reuse, but a defensive guard is cheap.
        with _in_flight_lock:
            if msg_id in _in_flight:
                already_in_flight = True
            else:
                already_in_flight = False
        if already_in_flight:
            _respond(
                msg_id,
                error={
                    "code": -32600,
                    "message": f"Request id {msg_id} already in flight",
                },
            )
            return
        # Dispatch to a worker thread so the read loop stays
        # responsive — without this, a long Git operation blocks the main
        # `for line in sys.stdin` loop and we cannot receive
        # `notifications/cancelled` for the very request we're
        # running. Register the in-flight record BEFORE starting
        # the worker so a cancel that arrives during thread startup
        # has a record to attach to.
        rec = _InFlight()
        with _in_flight_lock:
            _in_flight[msg_id] = rec
        t = threading.Thread(
            target=_dispatch_tools_call,
            args=(msg_id, params, rec),
            daemon=True,
            name=f"git-broker-tool-{msg_id}",
        )
        t.start()
        return
    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def _drain_in_flight(timeout_s: float = 30.0) -> None:
    """Wait for in-flight tool-call workers to complete after the
    main read loop sees EOF on stdin.

    Workers are daemon threads so a runaway Git operation cannot keep the
    process alive forever, but exiting immediately on EOF orphans
    fast-path callers that send `init + tools/call` then close stdin
    (`subprocess.run(input=...)` shape — heavily used by the test
    harness). Without this drain, the worker is killed mid-operation and
    the response never lands on stdout. Cap remains below MAX_TIMEOUT_MS.
    """
    # Take a snapshot of the threads under the lock — main() never
    # writes to _in_flight again after the read loop exits.
    with _in_flight_lock:
        if not _in_flight:
            return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _in_flight_lock:
            if not _in_flight:
                return
        time.sleep(0.05)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:
            sys.stderr.write(f"[git-broker] unhandled error: {e}\n")
            sys.stderr.flush()
    # Stdin closed. Workers may still be running. Drain so any
    # already-dispatched tool call gets its response on stdout before
    # the process exits.
    _drain_in_flight(timeout_s=30.0)


if __name__ == "__main__":
    main()

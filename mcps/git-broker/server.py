#!/usr/bin/env python3
"""Dedicated, contained Git mutation broker over stdio MCP.

The server exposes one structured ``git`` tool. It never accepts shell text or
raw argv. Every operation is validated before Git runs without a shell inside
a broker-owned bubblewrap boundary. Repository-local execution config is
masked and network exists only for validated fetch and push operations.

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
        "githubConfigDir",
        "sshKnownHostsFile",
        "trustedHooksDir",
        "userBusSocket",
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
        "githubConfigDir",
        "sshKnownHostsFile",
        "trustedHooksDir",
        "userBusSocket",
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


_GIT_OPERATIONS = frozenset(
    {
        "status",
        "diff",
        "log",
        "add",
        "commit",
        "fetch",
        "push",
        "switch",
        "worktree_add",
    }
)
_GIT_OPERATION_FIELDS = {
    "status": frozenset(),
    "diff": frozenset({"paths", "staged"}),
    "log": frozenset({"max_count"}),
    "add": frozenset({"paths", "all"}),
    "commit": frozenset({"message"}),
    "fetch": frozenset({"remote"}),
    "push": frozenset({"remote"}),
    "switch": frozenset({"branch", "create", "start_point"}),
    "worktree_add": frozenset({"worktree_path", "branch", "start_point"}),
}
_GIT_BASE_FIELDS = frozenset({"repo_path", "operation", "timeout"})
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


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
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
    ):
        raise RuntimeError("start_point must be a bounded Git ref name")
    return value


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
        raise RuntimeError(
            f"unexpected fields for {operation}: " + ", ".join(unexpected)
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
        staged = args.get("staged", False)
        if not isinstance(staged, bool):
            raise RuntimeError("staged must be a boolean")
        request["staged"] = staged
        request["paths"] = (
            _validate_relative_paths(args["paths"]) if "paths" in args else ()
        )
    elif operation == "log":
        count = args.get("max_count", 20)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
            raise RuntimeError("max_count must be an integer from 1 to 200")
        request["max_count"] = count
    elif operation == "add":
        all_files = args.get("all", False)
        if not isinstance(all_files, bool):
            raise RuntimeError("add all must be a boolean")
        has_paths = "paths" in args
        if has_paths == all_files:
            raise RuntimeError("add requires exactly one of non-empty paths or all=true")
        request["all"] = all_files
        request["paths"] = _validate_relative_paths(args["paths"]) if has_paths else ()
    elif operation == "commit":
        message = args.get("message")
        if (
            not isinstance(message, str)
            or not message.strip()
            or "\x00" in message
            or len(message) > 10_000
        ):
            raise RuntimeError("commit message must be non-empty and at most 10000 characters")
        request["message"] = message
    elif operation in {"fetch", "push"}:
        remote = args.get("remote", "origin")
        if remote != "origin":
            raise RuntimeError("network Git operations allow only the origin remote")
        request["remote"] = remote
    elif operation == "switch":
        request["branch"] = _validate_branch(args.get("branch"))
        create = args.get("create", False)
        if not isinstance(create, bool):
            raise RuntimeError("create must be a boolean")
        request["create"] = create
        if "start_point" in args:
            if not create:
                raise RuntimeError("start_point requires create=true")
            request["start_point"] = _validate_start_point(args["start_point"])
    elif operation == "worktree_add":
        raw_target = args.get("worktree_path")
        if (
            not isinstance(raw_target, str)
            or not raw_target
            or "\x00" in raw_target
            or not Path(raw_target).is_absolute()
        ):
            raise RuntimeError("worktree_path must be an absolute path")
        lexical_target = Path(os.path.normpath(raw_target))
        resolved_target = lexical_target.resolve(strict=False)
        if lexical_target != resolved_target or not _path_within(
            resolved_target, policy["worktreeCreationRoots"]
        ):
            raise RuntimeError("worktree_path must be canonical and under an allowed root")
        request["worktree_path"] = resolved_target
        request["branch"] = _validate_branch(args.get("branch"))
        if "start_point" in args:
            request["start_point"] = _validate_start_point(args["start_point"])
    return request


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
    if identity.worktree in policy["protectedWorktrees"] and operation not in {
        "status",
        "diff",
        "log",
        "fetch",
        "worktree_add",
    }:
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
    if operation in {"fetch", "push"}:
        if remote_url is None:
            raise RuntimeError(f"{operation} requires a validated remote URL")
        _validate_remote_url(remote_url, policy)
        argv.extend(("-c", f"remote.origin.url={remote_url}"))
        argv.extend(("-c", f"remote.origin.pushurl={remote_url}"))
    argv.extend(("-C", str(identity.worktree)))

    if operation == "status":
        argv.extend(("status", "--short", "--branch"))
    elif operation == "diff":
        argv.append("diff")
        if request["staged"]:
            argv.append("--cached")
        if request["paths"]:
            argv.append("--")
            argv.extend(request["paths"])
    elif operation == "log":
        argv.extend(("log", "--oneline", "--decorate=no", f"--max-count={request['max_count']}"))
    elif operation == "add":
        argv.append("add")
        if request["all"]:
            argv.append("--all")
        else:
            argv.append("--")
            argv.extend(request["paths"])
    elif operation == "commit":
        argv.extend(("commit", "--message", request["message"]))
    elif operation == "fetch":
        argv.extend(
            (
                "fetch",
                "--prune",
                "--no-tags",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
            )
        )
    elif operation == "push":
        if current_branch is None:
            raise RuntimeError("push requires a validated current branch")
        branch = _validate_branch(current_branch, "current branch")
        argv.extend(("push", "--porcelain"))
        argv.extend(("origin", f"refs/heads/{branch}:refs/heads/{branch}"))
    elif operation == "switch":
        argv.append("switch")
        if request["create"]:
            argv.append("--no-track")
            argv.extend(("--create", request["branch"]))
            if request.get("start_point"):
                argv.append(request["start_point"])
        else:
            argv.append(request["branch"])
    elif operation == "worktree_add":
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
    else:  # Defensive: validation should make this unreachable.
        raise RuntimeError(f"unsupported Git operation: {operation!r}")
    return argv


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
    identity: _GitIdentity,
    policy: dict,
    git_argv: list[str],
    *,
    existing_only: bool = True,
    ssh_auth_sock: str | None = None,
    remote_url: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build a bubblewrap command with only selected Git state writable."""
    bwrap = _resolve_required_executable(policy, "bubblewrapExecutable")
    networked = request["operation"] in {"fetch", "push"}
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
    worktree_mode = (
        "--ro-bind"
        if request["operation"] in {"fetch", "worktree_add"}
        else "--bind"
    )
    binds.append((worktree_mode, identity.worktree))
    binds.append(("--bind", identity.common_dir))
    if not _path_within(identity.git_dir, (identity.common_dir,)):
        binds.append(("--bind", identity.git_dir))
    if request["operation"] == "worktree_add":
        binds.append(("--bind", request["worktree_path"]))
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
    if networked:
        if remote_url is None:
            raise RuntimeError("network Git operation requires a validated remote URL")
        _validate_remote_url(remote_url, policy)
        is_https = remote_url.startswith("https://")
        credential_paths = (
            (policy["githubConfigDir"], policy["userBusSocket"])
            if is_https
            else (policy["sshKnownHostsFile"],)
        )
        for path in credential_paths:
            if existing_only and not path.exists():
                raise RuntimeError(
                    f"BROKEN safety boundary: required credential path is unavailable: {path}"
                )
            binds.append(("--ro-bind", path))
        if is_https:
            env["DBUS_SESSION_BUS_ADDRESS"] = (
                "unix:path=" + str(policy["userBusSocket"])
            )
        else:
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
    for _mode, path in binds:
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
    # A repository's local config can name arbitrary filter/diff commands and
    # URL rewrites. Overlay it only inside the sandbox after mounting Git
    # metadata, so structured operations cannot become arbitrary execution or
    # silently select a different transport. Command-line config above supplies
    # the complete broker-owned runtime policy.
    argv.extend(
        ("--ro-bind", str(EMPTY_GIT_CONFIG), str(identity.common_dir / "config"))
    )
    argv.extend(("--chdir", str(identity.worktree), "--", *git_argv))
    return argv, env


def _network_git_context(
    request: dict, identity: _GitIdentity, policy: dict
) -> tuple[str | None, str | None]:
    operation = request["operation"]
    if operation not in {"fetch", "push"}:
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
    if operation == "push":
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
    if operation == "push":
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
            raise RuntimeError("push requires a non-detached current branch")
        branch = _validate_branch((current.stdout or "").strip(), "current branch")
    return remote_url, branch


def _dcg_command_for_git_request(request: dict) -> str:
    """Render the typed request as an equivalent command for dcg analysis."""
    argv = ["git", "-C", str(request["repo_path"])]
    operation = request["operation"]
    if operation == "status":
        argv.append("status")
    elif operation == "diff":
        argv.append("diff")
        if request["staged"]:
            argv.append("--cached")
        if request["paths"]:
            argv.extend(("--", *request["paths"]))
    elif operation == "log":
        argv.extend(("log", f"--max-count={request['max_count']}"))
    elif operation == "add":
        argv.append("add")
        argv.extend(("--all",) if request["all"] else ("--", *request["paths"]))
    elif operation == "commit":
        argv.extend(("commit", "--message", request["message"]))
    elif operation == "fetch":
        argv.extend(("fetch", "origin"))
    elif operation == "push":
        argv.extend(("push", "origin"))
    elif operation == "switch":
        argv.append("switch")
        if request["create"]:
            argv.extend(("--create", request["branch"]))
            if request.get("start_point"):
                argv.append(request["start_point"])
        else:
            argv.append(request["branch"])
    elif operation == "worktree_add":
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
    else:
        raise RuntimeError(f"unsupported Git operation: {operation!r}")
    return shlex.join(argv)


def _prepare_worktree_target(request: dict) -> Path | None:
    if request["operation"] != "worktree_add":
        return None
    target: Path = request["worktree_path"]
    if target.exists() or target.is_symlink():
        raise RuntimeError("worktree_path already exists; refusing to overwrite it")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("worktree_path parent must already exist") from exc
    if parent != target.parent:
        raise RuntimeError("worktree_path parent must not traverse symlinks")
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


TOOLS = [
    {
        "name": "git",
        "description": (
            "Runs one policy-approved Git operation in a validated repository. "
            "The tool accepts structured fields only, never shell text or raw argv."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the selected Git worktree",
                },
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "diff",
                        "log",
                        "add",
                        "commit",
                        "fetch",
                        "push",
                        "switch",
                        "worktree_add",
                    ],
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative paths for add or diff",
                },
                "all": {"type": "boolean"},
                "message": {"type": "string"},
                "remote": {"type": "string"},
                "branch": {"type": "string"},
                "create": {"type": "boolean"},
                "start_point": {"type": "string"},
                "worktree_path": {"type": "string"},
                "staged": {"type": "boolean"},
                "max_count": {"type": "integer", "minimum": 1, "maximum": 200},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT_MS,
                },
            },
            "required": ["repo_path", "operation"],
            "additionalProperties": False,
        },
    }
]


SERVER_INFO = {"name": "git-broker", "version": "1.0.0"}
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

    identity = _discover_git_identity(request["repo_path"], policy)
    _authorize_git_operation(identity, request["operation"], policy)
    if request["operation"] == "push":
        nixos_reason = _check_nixos_push(
            "git -C " + shlex.quote(str(identity.worktree)) + " push origin"
        )
        if nixos_reason is not None:
            return f"[git-broker] {nixos_reason}"

    remote_url, current_branch = _network_git_context(request, identity, policy)
    created_target = _prepare_worktree_target(request)
    completed = False
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
        )
        rc, stdout, stderr = _run_argv(
            sandbox_argv, env, request["timeout"], rec
        )
        completed = rc == 0
        return _compose_output(rc, stdout, stderr)
    finally:
        if created_target is not None and not completed:
            try:
                created_target.rmdir()
            except OSError:
                # A failed Git operation may have partially populated its
                # target. Never recursively clean it: timeout is not rollback.
                pass


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

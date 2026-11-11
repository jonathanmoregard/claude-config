# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp==1.30.0"]
# ///
"""github-mux: one `github` MCP server in front of several GitHub App installations.

The GitHub MCP server binds one token to one App installation, so reaching both
the personal account and an organisation used to mean registering the same
server twice, which put every tool name and the server instructions in each
session twice. This server spawns one child per installation (the existing
github-app-mcp-server wrapper invocations), advertises the default child's tool
list and instructions once, and forwards each tool call to the child that owns
the repository named in its arguments.

Routing, per call:
  * `owner` / `org` arguments and positive `org:` `user:` `owner:` `repo:`
    qualifiers in a `query` argument name owners.
  * An owner listed in `ownerChildren` selects that child; one in
    `defaultOwners` selects the default child; any other owner (a public repo
    elsewhere) constrains nothing.
  * One child selected -> that child. None -> the default child (this covers
    tools with no owner at all: get_me, get_teams, ...). Owners of different
    children in one call -> a tool error; the mux never guesses.

Security: the mux never reads, mints or sees tokens or the App key; each child
wrapper does that for itself, exactly as when it was registered directly. Only
tool names the default child advertises are forwarded, and a child whose tool
set differs from the default child's is fenced off (its calls return an
error), so a child launched without the shared `--exclude-tools` list cannot
expose a tool the others hide.

Config (JSON, path given with --config):
  command         child executable
  sharedArgs      argv tail every child gets (toolsets, exclusions)
  defaultOwners   logins the default child's installation covers
  ownerChildren   {login: argv placed before sharedArgs for that child}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "github"
CHILD_START_TIMEOUT_S = 60
LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OWNER_ARGUMENTS = ("owner", "org")
OWNER_QUALIFIERS = ("org", "user", "owner", "repo")
# Scanned left to right, a term is either a "quoted phrase" (search text, so a
# qualifier inside it is not one) or a qualifier. A qualifier starts a search
# term (not preceded by a word character or `-` other than its own negation);
# its value is a "quoted" string or runs to the next space or parenthesis.
QUALIFIER_RE = re.compile(
    r'(?P<phrase>"[^"]*")'
    r"|(?<![\w-])(?P<negated>-?)(?P<key>" + "|".join(OWNER_QUALIFIERS) + r"):"
    r'(?P<value>"[^"]*"|[^\s()]+)',
    re.IGNORECASE,
)
CONFIG_KEYS = frozenset({"command", "sharedArgs", "defaultOwners", "ownerChildren"})
# Tools that choose or change a pull request's base branch. `create_pull_request`
# requires `base` -- GitHub's REST API does, so it cannot simply be stripped and
# left to default -- and `update_pull_request` can retarget an open one.
BASE_ARGUMENT_TOOLS = frozenset({"create_pull_request", "update_pull_request"})
# How the default branch is discovered: `search_repositories` with a `repo:`
# qualifier returns the full repository object, which carries `default_branch`.
# Asking costs one call through the child that is already routed, and means a
# new repository needs no setup -- there is no list to keep.
REPO_LOOKUP_TOOL = "search_repositories"


class ConfigError(ValueError):
    pass


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class MuxConfig:
    command: str
    shared_args: tuple[str, ...]
    default_owners: frozenset[str]
    owner_children: dict[str, tuple[str, ...]]


def _login(value: object, where: str) -> str:
    if not isinstance(value, str) or not LOGIN_RE.match(value.lower()):
        raise ConfigError(f"{where}: {value!r} is not a GitHub login")
    return value.lower()


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be a list of strings")
    return tuple(value)


def parse_config(raw: object) -> MuxConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(raw) - CONFIG_KEYS
    missing = CONFIG_KEYS - set(raw)
    if unknown or missing:
        raise ConfigError(f"config keys: unknown {sorted(unknown)}, missing {sorted(missing)}")
    command = raw["command"]
    if not isinstance(command, str) or not command:
        raise ConfigError("command must be a non-empty string")
    default_owners = frozenset(
        _login(owner, "defaultOwners") for owner in _string_list(raw["defaultOwners"], "defaultOwners")
    )
    children = raw["ownerChildren"]
    if not isinstance(children, dict):
        raise ConfigError("ownerChildren must be an object")
    owner_children = {
        _login(owner, "ownerChildren"): _string_list(args, f"ownerChildren[{owner}]")
        for owner, args in children.items()
    }
    if len(owner_children) != len(children):
        raise ConfigError("ownerChildren lists an owner twice (case-insensitively)")
    overlap = default_owners & set(owner_children)
    if overlap:
        raise ConfigError(f"owners both default and mapped: {sorted(overlap)}")
    return MuxConfig(
        command=command,
        shared_args=_string_list(raw["sharedArgs"], "sharedArgs"),
        default_owners=default_owners,
        owner_children=owner_children,
    )


def load_config(path: Path) -> MuxConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc
    return parse_config(raw)


def child_argvs(config: MuxConfig) -> dict[str | None, list[str]]:
    """argv per child; key None is the default child."""
    argvs: dict[str | None, list[str]] = {None: [config.command, *config.shared_args]}
    for owner, args in config.owner_children.items():
        argvs[owner] = [config.command, *args, *config.shared_args]
    return argvs


def _query_owners(query: str) -> set[str]:
    # Matched over the raw query, not a tokenisation of it: free text may hold
    # apostrophes, parentheses and quotes that no tokenizer reads the way
    # GitHub does. Only a balanced "quoted phrase" hides what it contains.
    owners = set()
    for match in QUALIFIER_RE.finditer(query):
        if match["phrase"] or match["negated"]:
            continue
        value = match["value"].strip('"')
        owner = value.split("/", 1)[0] if match["key"].lower() == "repo" else value
        if owner:
            owners.add(owner.lower())
    return owners


def named_owners(arguments: dict[str, Any]) -> set[str]:
    owners = {
        arguments[key].lower()
        for key in OWNER_ARGUMENTS
        if isinstance(arguments.get(key), str) and arguments[key]
    }
    query = arguments.get("query")
    if isinstance(query, str):
        owners |= _query_owners(query)
    return owners


def route(arguments: dict[str, Any], config: MuxConfig) -> str | None:
    """The child (owner key, None = default) that must serve these arguments."""
    selected: dict[str | None, set[str]] = {}
    for owner in named_owners(arguments):
        if owner in config.owner_children:
            selected.setdefault(owner, set()).add(owner)
        elif owner in config.default_owners:
            selected.setdefault(None, set()).add(owner)
    if len(selected) > 1:
        groups = "; ".join(
            f"{'default installation' if child is None else child + ' installation'}: "
            + ", ".join(sorted(owners))
            for child, owners in sorted(selected.items(), key=lambda item: item[0] or "")
        )
        raise RoutingError(
            "this call names owners served by different GitHub App installations "
            f"({groups}); split it into one call per owner"
        )
    return next(iter(selected), None)


def tool_set_mismatch(reference: Iterable[str], other: Iterable[str]) -> str | None:
    reference_set, other_set = set(reference), set(other)
    if reference_set == other_set:
        return None
    return (
        f"tool set differs from the default child: missing {sorted(reference_set - other_set)}, "
        f"extra {sorted(other_set - reference_set)}"
    )


@dataclass
class Child:
    """One installation's child server. `ready` is set once it either serves
    (`session` set) or is fenced off (`unavailable` set); it is never left
    unset after its task ends."""

    label: str
    ready: anyio.Event = field(default_factory=anyio.Event)
    session: ClientSession | None = None
    unavailable: str | None = None
    init: types.InitializeResult | None = None
    tools: list[types.Tool] = field(default_factory=list)
    start_scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)

    def fence(self, reason: str) -> None:
        self.session = None
        self.unavailable = reason
        _log(f"fencing off {self.label}: {reason}")
        self.ready.set()


def _label(owner: str | None) -> str:
    return "default" if owner is None else owner


def _log(message: str) -> None:
    print(f"github-mux: {message}", file=sys.stderr, flush=True)


async def _list_all_tools(session: ClientSession) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


async def run_child(
    child: Child, argv: list[str], reference: Child | None, stop: anyio.Event
) -> None:
    """Spawn, initialise and hold one child open until `stop` is set.

    Runs as its own task so the SDK's task groups are entered and exited in
    one task. A start failure, or a tool set differing from `reference` (the
    default child), fences the child off; it never raises into the mux.
    """
    try:
        # Cancelled only while the child is still starting (see stop_children).
        with child.start_scope:
            async with AsyncExitStack() as stack:
                params = StdioServerParameters(command=argv[0], args=argv[1:])
                read, write = await stack.enter_async_context(
                    stdio_client(params, errlog=sys.stderr)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                # The timeout scope may only wrap requests: the contexts above
                # open task groups that outlive it.
                with anyio.fail_after(CHILD_START_TIMEOUT_S):
                    init = await session.initialize()
                    tools = await _list_all_tools(session)
                if reference is not None:
                    await reference.ready.wait()
                    mismatch = tool_set_mismatch(
                        (tool.name for tool in reference.tools), (tool.name for tool in tools)
                    )
                    if mismatch:
                        child.fence(mismatch)
                        return
                child.session, child.init, child.tools = session, init, tools
                child.ready.set()
                # Leaving the stack uncancelled runs the SDK's stdio shutdown:
                # close stdin, wait, then terminate the child's process group.
                await stop.wait()
    except Exception as exc:  # noqa: BLE001 - any start failure fences the child
        if not child.ready.is_set():
            child.fence(f"failed to start: {type(exc).__name__}: {exc}")
    finally:
        if not child.ready.is_set():
            # Start cancelled by mux shutdown: release any waiter.
            child.session, child.unavailable = None, "mux shutting down"
            child.ready.set()


def stop_children(children: dict[str | None, Child], stop: anyio.Event) -> None:
    """Ready children close gracefully on `stop`; a child still starting has
    no session to close, so its start is cancelled (the SDK then kills it)
    rather than letting a hung start hold up the mux's exit."""
    stop.set()
    for child in children.values():
        if not child.ready.is_set():
            child.start_scope.cancel()


def _default_branch_of(payload: str) -> str | None:
    """The `default_branch` of the single repository in a search result, or None.

    None means "could not tell", and every caller treats that as permission.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        return None
    branch = items[0].get("default_branch") if isinstance(items[0], dict) else None
    return branch if isinstance(branch, str) and branch else None


async def lookup_default_branch(
    owner: str,
    repo: str,
    session: Any,
    cache: dict[str, str | None],
    force: bool = False,
) -> str | None:
    """Ask the child for `owner/repo`'s default branch. None if it cannot be told.

    Successful answers are cached for the life of the process -- they only
    change when someone renames a default branch. Failures are *not* cached, so
    a transient one does not leave the rule switched off for that repository
    until the mux restarts.
    """
    key = f"{owner}/{repo}".lower()
    if key in cache and not force:
        return cache[key]
    branch: str | None = None
    try:
        result = await session.call_tool(
            REPO_LOOKUP_TOOL,
            {"query": f"repo:{owner}/{repo}", "minimal_output": False, "perPage": 1},
        )
        if result.isError:
            # How the child reports a rate limit, a 4xx, or a tool it does not
            # have. This is the *common* failure, far more likely than a
            # transport exception, and it used to pass silently -- so a rule
            # that had quietly stopped working looked identical to one with
            # nothing to say.
            _log(f"default-branch lookup for {key} returned an error result")
        else:
            for block in result.content:
                branch = _default_branch_of(getattr(block, "text", "") or "")
                if branch:
                    break
            if branch is None:
                _log(f"default-branch lookup for {key} returned no usable branch")
    except Exception as exc:  # noqa: BLE001 - a failed lookup must not block the call
        _log(f"default-branch lookup for {key} failed: {type(exc).__name__}: {exc}")
    # Only successes are cached. A failure is usually transient -- a rate limit,
    # a child still waking, a network blip -- and caching it would make one bad
    # moment permit every non-default base in that repository for the rest of
    # the process. Re-asking costs one call on a path that opens pull requests.
    if branch:
        cache[key] = branch
    return branch


def base_refusal(name: str, arguments: dict[str, Any], default: str | None) -> str | None:
    """Why this call may not set a pull request's base; None when it may.

    This is a housekeeping rule, not a security control. Pull requests aimed
    somewhere other than the default branch are noise Jonathan does not want to
    manage, so the mux declines to open them and says where to aim instead.

    It is worth being explicit that it guards nothing: anyone who can reach this
    tool can also `git push` to any unprotected branch directly, so routing
    commits through a pull request aimed at one grants no access they lack. The
    branch that matters is protected by its ruleset -- required review, required
    checks, no bypass actors, and approval dismissed on any later push -- and
    that, not this function, is what keeps unreviewed work out.

    "Default branch" means *this* repository's own, asked for at call time --
    `claude-config` defaults to `master` and `nixos-config` to `main`, so a
    shared list of common names would admit whichever of the two also existed.
    The comparison is case-sensitive because git refs are: `Main` and `main` can
    both exist and are different branches.

    `default` of None means the lookup could not tell, and this returns None --
    permission. Failing open is the right default for a convenience rule: the
    cost of a wrong allow is one pull request aimed somewhere untidy, while the
    cost of a wrong refusal is blocking ordinary work in a repository that did
    nothing but be new.
    """
    if name not in BASE_ARGUMENT_TOOLS or "base" not in arguments:
        return None
    base = arguments["base"]
    if not isinstance(base, str) or not base:
        return (
            f"{name}: base must be a non-empty string, got {base!r}. "
            "A base that cannot be read cannot be checked."
        )
    if default is None or base == default:
        return None
    return (
        f"{name}: base {base!r} is not this repository's default branch "
        f"({default!r}). Open it against {default!r} instead. Pull requests "
        "aimed at other branches are not wanted here; if one is genuinely "
        "needed, retarget it in GitHub's UI."
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"github-mux: {message}")], isError=True
    )


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    config: MuxConfig,
    tool_names: frozenset[str],
    children: dict[str | None, Child],
    branch_cache: dict[str, str | None] | None = None,
) -> types.CallToolResult:
    if name not in tool_names:
        return _error(f"unknown tool {name!r}")
    try:
        owner = route(arguments, config)
    except RoutingError as exc:
        return _error(str(exc))
    child = children[owner]
    # A child still starting is waited for; run_child bounds its start.
    await child.ready.wait()
    if child.session is None:
        return _error(f"{child.label} installation unavailable: {child.unavailable}")
    # The base check runs after routing because it asks the routed child for the
    # repository's default branch rather than reading it from a list.
    if name in BASE_ARGUMENT_TOOLS and "base" in arguments:
        repo_owner, repo = arguments.get("owner"), arguments.get("repo")
        cache = branch_cache if branch_cache is not None else {}
        default = None
        if isinstance(repo_owner, str) and isinstance(repo, str) and repo_owner and repo:
            default = await lookup_default_branch(
                repo_owner, repo, child.session, cache
            )
        refusal = base_refusal(name, arguments, default)
        if refusal is not None and default is not None:
            # Never refuse on a remembered answer without checking it first: a
            # default branch that was renamed since it was cached would other-
            # wise refuse every pull request in that repository, naming the old
            # branch, until the mux restarts. Costs one call, on the rare path.
            default = await lookup_default_branch(
                repo_owner, repo, child.session, cache, force=True
            )
            refusal = base_refusal(name, arguments, default)
        if refusal is not None:
            return _error(refusal)
    try:
        return await child.session.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - a dead child is a tool error
        return _error(f"{child.label} installation call failed: {type(exc).__name__}: {exc}")


def routing_instructions(config: MuxConfig) -> str | None:
    """How a search reaches a non-default installation; None if there is none."""
    if not config.owner_children:
        return None
    owners = ", ".join(sorted(config.owner_children))
    return (
        f"Searches reach {owners} only when the query names the owner with an "
        f"org:, repo:, user: or owner: qualifier (e.g. org:{min(config.owner_children)}); "
        "other searches run on the default installation."
    )


def build_server(
    config: MuxConfig,
    tools: list[types.Tool],
    instructions: str | None,
    children: dict[str | None, Child],
) -> Server:
    routing = routing_instructions(config)
    if routing:
        instructions = f"{instructions}\n\n{routing}" if instructions else routing
    server: Server = Server(SERVER_NAME, instructions=instructions)
    tool_names = frozenset(tool.name for tool in tools)
    # One cache for the process, so a repository is looked up once per run.
    branch_cache: dict[str, str | None] = {}

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    # Children validate their own input; validating here too would only add
    # a second, possibly divergent, error message.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await dispatch(name, arguments, config, tool_names, children, branch_cache)

    return server


async def serve(config: MuxConfig) -> int:
    """Start every child at once; answer the client as soon as the default
    child is up. Other children finish starting in the background, so a slow
    or hung installation delays only the calls routed to it."""
    children = {owner: Child(_label(owner)) for owner in child_argvs(config)}
    default = children[None]
    stop = anyio.Event()
    async with anyio.create_task_group() as tg:
        for owner, argv in child_argvs(config).items():
            reference = None if owner is None else default
            tg.start_soon(run_child, children[owner], argv, reference, stop)
        await default.ready.wait()
        if default.session is None or default.init is None:
            _log("default child is required; exiting")
            stop_children(children, stop)
            return 1
        server = build_server(config, default.tools, default.init.instructions, children)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
        stop_children(children, stop)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _log(str(exc))
        return 2
    return anyio.run(serve, config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

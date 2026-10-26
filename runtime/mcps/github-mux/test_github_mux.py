# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp==1.30.0"]
# ///
"""Contract tests for the github-mux MCP server.

Run: uv run --script mcps/github-mux/test_github_mux.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

if __name__ != "__main__" and importlib.util.find_spec("mcp") is None:
    # Collected by a repo-wide pytest whose interpreter lacks the script's
    # dependencies; `uv run --script` (the documented runner) provides them.
    import pytest

    pytest.skip("needs mcp: run with uv run --script", allow_module_level=True)

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
FAKE_CHILD = HERE / "fake_child.py"
TRACKED_CONFIG = HERE / "config.json"

# Write tools no agent may reach: merging, review approval, branch updates,
# workflow dispatch, direct content writes. Dropping any of these from a child
# lets an agent merge, deploy or act as the account owner.
FORBIDDEN_TOOLS = frozenset(
    {
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
    }
)


def load_server():
    name = f"github_mux_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load github-mux server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mux = load_server()


def config(**overrides):
    raw = {
        "command": "/bin/true",
        "sharedArgs": ["--toolsets", "repos"],
        "defaultOwners": ["jonathanmoregard"],
        "ownerChildren": {"klaffat": ["--owner", "klaffat"]},
    }
    raw.update(overrides)
    return mux.parse_config(raw)


class BaseGuardTest(unittest.TestCase):
    """`base` is required on create, so the gate is its value per repository."""

    def refusal(self, name, arguments, default="master"):
        return mux.base_refusal(name, arguments, default)

    def test_the_repositorys_own_default_branch_is_allowed(self) -> None:
        self.assertIsNone(self.refusal("create_pull_request", {"base": "master"}))
        self.assertIsNone(
            self.refusal("create_pull_request", {"base": "main"}, default="main")
        )

    def test_another_repositorys_default_is_not_this_ones(self) -> None:
        # Why a shared {main, master} list will not do: `main` often also exists
        # in a master-default repository, so a list of common names would admit
        # a pull request the maintainer who expected master never wanted.
        message = self.refusal("create_pull_request", {"base": "main"})
        self.assertIsNotNone(message)
        self.assertIn("master", message)
        self.assertIsNotNone(
            self.refusal("create_pull_request", {"base": "master"}, default="main")
        )

    def test_the_match_is_case_sensitive_because_git_refs_are(self) -> None:
        # `Main` and `main` can both exist and are different branches.
        self.assertIsNotNone(self.refusal("create_pull_request", {"base": "Master"}))

    def test_feature_branch_base_is_refused(self) -> None:
        message = self.refusal("create_pull_request", {"base": "feat/something"})
        self.assertIsNotNone(message)
        self.assertIn("feat/something", message)

    def test_retargeting_an_existing_pr_is_refused_too(self) -> None:
        # update_pull_request takes a new base, so leaving it out would let a
        # pull request drift off the default branch right after being opened.
        message = self.refusal(
            "update_pull_request", {"pullNumber": 1, "base": "release/1.x"}
        )
        self.assertIsNotNone(message)
        self.assertIn("release/1.x", message)

    def test_update_without_a_base_is_untouched(self) -> None:
        self.assertIsNone(
            self.refusal("update_pull_request", {"pullNumber": 1, "title": "x"})
        )

    def test_a_non_string_base_is_refused_rather_than_coerced(self) -> None:
        self.assertIsNotNone(self.refusal("create_pull_request", {"base": ["main"]}))

    def test_an_empty_base_is_refused(self) -> None:
        self.assertIsNotNone(self.refusal("create_pull_request", {"base": ""}))

    def test_an_unresolved_default_branch_allows_the_call(self) -> None:
        # A convenience rule fails open: a brand-new repository, a rate limit or
        # an offline moment must not block ordinary work. This is the arm that
        # replaced "refuse anything not in the list".
        self.assertIsNone(
            self.refusal("create_pull_request", {"base": "feat/x"}, default=None)
        )

    def test_other_tools_are_not_touched(self) -> None:
        self.assertIsNone(self.refusal("list_pull_requests", {"base": "feat/x"}))



class DefaultBranchCacheTest(unittest.IsolatedAsyncioTestCase):
    """What the lookup remembers, and what it refuses to remember."""

    class Session:
        """A child that fails its first lookup and answers every one after."""

        def __init__(self, fail_first: int = 1, branches: list[str] | None = None) -> None:
            self.calls = 0
            self.fail_first = fail_first
            self.seen: list[tuple[str, dict]] = []
            self.branches = branches or []

        async def call_tool(self, name: str, arguments: dict):
            self.calls += 1
            self.seen.append((name, arguments))
            if self.calls <= self.fail_first:
                raise RuntimeError("rate limited")
            branch = (
                self.branches[min(len(self.branches) - 1, self.calls - 1)]
                if self.branches
                else "main"
            )
            payload = json.dumps(
                {"items": [{"full_name": "o/r", "default_branch": branch}]}
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=payload)]
            )

    async def test_a_transient_failure_is_not_remembered(self) -> None:
        # The bug this replaces: one failed lookup cached None, so every later
        # call in that repository skipped the check for the whole process.
        session, cache = self.Session(), {}
        self.assertIsNone(await mux.lookup_default_branch("o", "r", session, cache))
        self.assertEqual(cache, {})
        self.assertEqual(
            await mux.lookup_default_branch("o", "r", session, cache), "main"
        )
        self.assertEqual(session.calls, 2)

    async def test_a_success_is_remembered_and_not_re_asked(self) -> None:
        session, cache = self.Session(fail_first=0), {}
        for _ in range(3):
            self.assertEqual(
                await mux.lookup_default_branch("o", "r", session, cache), "main"
            )
        self.assertEqual(session.calls, 1)

    async def test_the_cache_key_ignores_owner_and_repo_case(self) -> None:
        session, cache = self.Session(fail_first=0), {}
        await mux.lookup_default_branch("O", "R", session, cache)
        await mux.lookup_default_branch("o", "r", session, cache)
        self.assertEqual(session.calls, 1)

    async def test_the_lookup_query_names_exactly_one_repository(self) -> None:
        # The shape the real child is asked for. Without this nothing pins it,
        # so a wrong query would resolve nothing, fail open, and still pass.
        session, cache = self.Session(fail_first=0), {}
        await mux.lookup_default_branch("Owner", "Repo", session, cache)
        name, arguments = session.seen[0]
        self.assertEqual(name, "search_repositories")
        self.assertEqual(arguments["query"], "repo:Owner/Repo")
        self.assertIs(arguments["minimal_output"], False)
        self.assertEqual(arguments["perPage"], 1)

    async def test_force_re_asks_and_replaces_a_remembered_answer(self) -> None:
        # A renamed default branch: cached "master", now really "main".
        session = self.Session(fail_first=0, branches=["master", "main"])
        cache: dict = {}
        self.assertEqual(
            await mux.lookup_default_branch("o", "r", session, cache), "master"
        )
        self.assertEqual(
            await mux.lookup_default_branch("o", "r", session, cache, force=True),
            "main",
        )
        self.assertEqual(cache["o/r"], "main")
        self.assertEqual(session.calls, 2)


class DefaultBranchLookupTest(unittest.TestCase):
    """Reading `default_branch` out of what the child returns."""

    def test_a_single_repository_result_yields_its_default_branch(self) -> None:
        payload = json.dumps(
            {"total_count": 1, "items": [{"full_name": "o/r", "default_branch": "master"}]}
        )
        self.assertEqual(mux._default_branch_of(payload), "master")

    def test_anything_ambiguous_or_malformed_yields_none(self) -> None:
        for payload in (
            "",
            "not json",
            json.dumps({"items": []}),
            json.dumps({"items": [{"default_branch": "a"}, {"default_branch": "b"}]}),
            json.dumps({"items": [{"full_name": "o/r"}]}),
            json.dumps({"items": [{"default_branch": ""}]}),
            json.dumps({"items": "nope"}),
            json.dumps(["items"]),
        ):
            with self.subTest(payload=payload[:40]):
                self.assertIsNone(mux._default_branch_of(payload))


class RouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def route(self, arguments: dict):
        return mux.route(arguments, self.cfg)

    def test_owner_arg_naming_mapped_owner_goes_to_its_child(self) -> None:
        self.assertEqual(self.route({"owner": "klaffat", "repo": "x"}), "klaffat")

    def test_owner_arg_is_case_insensitive(self) -> None:
        self.assertEqual(self.route({"owner": "KlAffat", "repo": "x"}), "klaffat")

    def test_default_owner_goes_to_default_child(self) -> None:
        self.assertIsNone(self.route({"owner": "jonathanmoregard", "repo": ".claude"}))

    def test_unknown_owner_goes_to_default_child(self) -> None:
        self.assertIsNone(self.route({"owner": "torvalds", "repo": "linux"}))

    def test_org_arg_routes_like_owner(self) -> None:
        self.assertEqual(self.route({"org": "klaffat", "team_slug": "core"}), "klaffat")

    def test_tool_without_owner_goes_to_default_child(self) -> None:
        self.assertIsNone(self.route({}))
        self.assertIsNone(self.route({"user": "klaffat"}))

    def test_query_repo_qualifier_names_the_owner(self) -> None:
        self.assertEqual(self.route({"query": "repo:klaffat/app is:open bug"}), "klaffat")

    def test_query_org_user_owner_qualifiers_name_the_owner(self) -> None:
        for query in ("org:klaffat bug", "user:Klaffat bug", "owner:klaffat bug", "ORG:klaffat"):
            with self.subTest(query=query):
                self.assertEqual(self.route({"query": query}), "klaffat")

    def test_quoted_qualifier_value(self) -> None:
        self.assertEqual(self.route({"query": 'org:"klaffat" "two words"'}), "klaffat")

    def test_query_without_qualifiers_goes_to_default_child(self) -> None:
        self.assertIsNone(self.route({"query": "flaky test is:open"}))

    def test_negated_qualifier_does_not_select_a_child(self) -> None:
        self.assertIsNone(self.route({"query": "-org:klaffat bug"}))

    def test_unknown_owner_does_not_constrain_a_mapped_one(self) -> None:
        self.assertEqual(
            self.route({"query": "repo:klaffat/app repo:torvalds/linux"}), "klaffat"
        )

    def test_owners_of_different_children_conflict(self) -> None:
        with self.assertRaises(mux.RoutingError) as ctx:
            self.route({"query": "repo:klaffat/app repo:jonathanmoregard/.claude"})
        self.assertIn("klaffat", str(ctx.exception))
        self.assertIn("jonathanmoregard", str(ctx.exception))

    def test_owner_arg_and_query_qualifier_conflict(self) -> None:
        with self.assertRaises(mux.RoutingError):
            self.route({"owner": "jonathanmoregard", "query": "repo:klaffat/app"})

    def test_owner_arg_and_matching_query_qualifier_agree(self) -> None:
        self.assertEqual(
            self.route({"owner": "klaffat", "query": "org:klaffat is:open"}), "klaffat"
        )

    def test_unbalanced_quote_still_routes(self) -> None:
        self.assertEqual(self.route({"query": 'repo:klaffat/app "unterminated'}), "klaffat")

    def test_non_string_owner_is_ignored(self) -> None:
        self.assertIsNone(self.route({"owner": 42, "query": None}))

    def test_apostrophes_in_free_text_do_not_hide_qualifiers(self) -> None:
        # GitHub search quotes with " only; an apostrophe is plain text.
        for query in (
            "doesn't repo:klaffat/app can't",
            "it's org:klaffat",
            "user:klaffat won't start",
            "o'brien's 'quoted' owner:klaffat",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.route({"query": query}), "klaffat")

    def test_double_quoted_phrases_do_not_hide_qualifiers(self) -> None:
        for query in (
            '"crash on start" repo:klaffat/app',
            'repo:klaffat/app "it\'s broken"',
            '"two" "phrases" org:klaffat',
        ):
            with self.subTest(query=query):
                self.assertEqual(self.route({"query": query}), "klaffat")

    def test_qualifier_inside_a_quoted_phrase_is_search_text(self) -> None:
        for query in (
            '"repo:klaffat/app"',
            'bug "org:klaffat is broken"',
            '"see owner:klaffat" "and user:klaffat"',
        ):
            with self.subTest(query=query):
                self.assertIsNone(self.route({"query": query}))

    def test_quoted_phrase_does_not_conflict_with_a_real_qualifier(self) -> None:
        self.assertIsNone(
            self.route({"query": 'repo:jonathanmoregard/.claude "repo:klaffat/app"'})
        )
        self.assertEqual(
            self.route({"query": '"repo:jonathanmoregard/x" repo:klaffat/app'}), "klaffat"
        )

    def test_quoted_qualifier_value_next_to_quoted_phrase(self) -> None:
        self.assertEqual(
            self.route({"query": '"user:jonathanmoregard" org:"klaffat" "x"'}), "klaffat"
        )

    def test_parenthesised_qualifiers_name_the_owner(self) -> None:
        for query in (
            "(repo:klaffat/app OR repo:klaffat/web) bug",
            "(org:klaffat)",
            "bug (user:klaffat)",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.route({"query": query}), "klaffat")

    def test_mixed_case_qualifier_and_owner(self) -> None:
        for query in ("Repo:KLAFFAT/App doesn't", "oRg:KlAffat", 'Owner:"KLAFFAT"'):
            with self.subTest(query=query):
                self.assertEqual(self.route({"query": query}), "klaffat")

    def test_negated_qualifier_is_ignored_next_to_apostrophes_and_parens(self) -> None:
        for query in ("can't -org:klaffat", "(-repo:klaffat/app) isn't"):
            with self.subTest(query=query):
                self.assertIsNone(self.route({"query": query}))

    def test_qualifier_word_inside_another_term_is_not_a_qualifier(self) -> None:
        for query in ("subrepo:klaffat/app", "my-org:klaffat", "foo_user:klaffat"):
            with self.subTest(query=query):
                self.assertIsNone(self.route({"query": query}))

    def test_apostrophe_does_not_hide_a_conflict(self) -> None:
        with self.assertRaises(mux.RoutingError):
            self.route({"query": "don't repo:klaffat/a repo:jonathanmoregard/b"})


class ConfigTest(unittest.TestCase):
    def test_every_child_gets_the_shared_args_after_its_own(self) -> None:
        cfg = config()
        argvs = mux.child_argvs(cfg)
        self.assertEqual(argvs[None], ["/bin/true", "--toolsets", "repos"])
        self.assertEqual(
            argvs["klaffat"], ["/bin/true", "--owner", "klaffat", "--toolsets", "repos"]
        )

    def test_owner_keys_are_normalised_to_lowercase(self) -> None:
        cfg = config(ownerChildren={"Klaffat": ["--owner", "klaffat"]})
        self.assertEqual(mux.route({"owner": "klaffat"}, cfg), "klaffat")

    def test_owner_both_default_and_mapped_is_rejected(self) -> None:
        with self.assertRaises(mux.ConfigError):
            config(defaultOwners=["klaffat"])

    def test_invalid_owner_login_is_rejected(self) -> None:
        with self.assertRaises(mux.ConfigError):
            config(ownerChildren={"not a login": []})

    def test_unknown_config_key_is_rejected(self) -> None:
        with self.assertRaises(mux.ConfigError):
            config(extra=True)

    def test_tracked_config_excludes_forbidden_tools_in_every_child(self) -> None:
        cfg = mux.load_config(TRACKED_CONFIG)
        argvs = mux.child_argvs(cfg)
        self.assertIn("klaffat", argvs)
        for owner, argv in argvs.items():
            with self.subTest(owner=owner):
                self.assertIn("--exclude-tools", argv)
                excluded = set(argv[argv.index("--exclude-tools") + 1].split(","))
                self.assertLessEqual(FORBIDDEN_TOOLS, excluded)

    def test_tracked_config_klaffat_child_selects_the_klaffat_installation(self) -> None:
        argv = mux.child_argvs(mux.load_config(TRACKED_CONFIG))["klaffat"]
        self.assertEqual(argv[1:3], ["--owner", "klaffat"])


class ToolSetTest(unittest.TestCase):
    def test_identical_sets_have_no_difference(self) -> None:
        self.assertIsNone(mux.tool_set_mismatch(["a", "b"], ["b", "a"]))

    def test_difference_names_missing_and_extra_tools(self) -> None:
        message = mux.tool_set_mismatch(["a", "b"], ["a", "merge_pull_request"])
        self.assertIsNotNone(message)
        self.assertIn("b", message)
        self.assertIn("merge_pull_request", message)


def write_launcher(directory: Path) -> Path:
    launcher = directory / "fake-child.sh"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CHILD}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


FAKE_TOOLS = (
    "get_me,list_pull_requests,search_issues,create_pull_request,"
    "search_repositories,crash"
)


class MuxIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Drive the real mux over stdio against fake child servers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = write_launcher(Path(self.tmp.name))
        self.errlog = open(Path(self.tmp.name) / "mux.stderr", "w", encoding="utf-8")  # noqa: SIM115 - closed by cleanup
        self.addCleanup(self.errlog.close)

    @asynccontextmanager
    async def open_mux(self, klaffat_args: list[str], shared_extra: list[str] = []):
        # The SDK's task groups must be entered and exited in the test's own
        # task; enterAsyncContext would close them from a cleanup task.
        config_path = Path(self.tmp.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "command": str(self.launcher),
                    "sharedArgs": [
                        "--tools",
                        FAKE_TOOLS,
                        "--instructions",
                        "fake guidance",
                        *shared_extra,
                    ],
                    "defaultOwners": ["jonathanmoregard"],
                    "ownerChildren": {"klaffat": klaffat_args},
                }
            ),
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER), "--config", str(config_path)]
        )
        async with (
            stdio_client(params, errlog=self.errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            self.init = await session.initialize()
            yield session

    async def call(self, session: ClientSession, name: str, arguments: dict):
        result = await session.call_tool(name, arguments)
        text = result.content[0].text if result.content else ""
        return result, text

    async def answered_by(self, session: ClientSession, name: str, arguments: dict) -> str:
        result, text = await self.call(session, name, arguments)
        self.assertFalse(result.isError, text)
        return json.loads(text)["label"]

    async def test_lists_default_child_tools_and_instructions_once(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
        self.assertEqual(sorted(names), sorted(FAKE_TOOLS.split(",")))
        self.assertTrue(self.init.instructions.startswith("fake guidance"))
        self.assertEqual(self.init.serverInfo.name, "github")

    async def test_instructions_tell_how_searches_reach_mapped_owners(self) -> None:
        async with self.open_mux(["--label", "klaffat"]):
            pass
        added = self.init.instructions.removeprefix("fake guidance")
        self.assertIn("klaffat", added)
        for qualifier in ("org:", "repo:", "user:", "owner:"):
            self.assertIn(qualifier, added)
        self.assertNotIn("jonathanmoregard", added)

    async def test_slow_child_starts_in_background_and_serves_its_first_call(self) -> None:
        started = time.monotonic()
        async with self.open_mux(["--label", "klaffat", "--start-delay", "6"]) as session:
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(
                await self.answered_by(
                    session, "list_pull_requests", {"owner": "klaffat", "repo": "app"}
                ),
                "klaffat",
            )

    async def test_hung_child_blocks_neither_initialize_nor_shutdown(self) -> None:
        pid_file = Path(self.tmp.name) / "klaffat.pid"
        started = time.monotonic()
        async with self.open_mux(
            ["--label", "klaffat", "--start-delay", "3600", "--pid-file", str(pid_file)]
        ) as session:
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")
            self.assertLess(time.monotonic() - started, 5)
            # Shut down only once the child is up and stuck before its handshake.
            while not pid_file.exists():
                await asyncio.sleep(0.05)
        self.assertLess(time.monotonic() - started, 15)
        self.assertProcessGone(int(pid_file.read_text(encoding="utf-8")))

    async def test_ready_child_gets_a_graceful_stdio_shutdown(self) -> None:
        exit_file = Path(self.tmp.name) / "klaffat.exit"
        async with self.open_mux(
            ["--label", "klaffat", "--exit-file", str(exit_file)]
        ) as session:
            self.assertEqual(
                await self.answered_by(session, "get_me", {"owner": "klaffat"}), "klaffat"
            )
        # Written only when the child saw its stdin close and returned, not
        # when it was killed.
        self.assertTrue(exit_file.exists(), "klaffat child was killed, not shut down")

    def assertProcessGone(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail(f"child {pid} outlived the mux")

    async def test_routes_calls_to_the_owning_child(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")
            self.assertEqual(
                await self.answered_by(
                    session, "list_pull_requests", {"owner": "klaffat", "repo": "app"}
                ),
                "klaffat",
            )
            self.assertEqual(
                await self.answered_by(
                    session,
                    "list_pull_requests",
                    {"owner": "jonathanmoregard", "repo": ".claude"},
                ),
                "personal",
            )
            self.assertEqual(
                await self.answered_by(
                    session, "search_issues", {"query": "repo:klaffat/app bug"}
                ),
                "klaffat",
            )

    async def test_arguments_reach_the_child_unchanged(self) -> None:
        arguments = {"owner": "klaffat", "repo": "app", "perPage": 5}
        async with self.open_mux(["--label", "klaffat"]) as session:
            _, text = await self.call(session, "list_pull_requests", arguments)
        self.assertEqual(json.loads(text)["arguments"], arguments)

    async def test_conflicting_owners_return_a_tool_error_and_mux_survives(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            result, text = await self.call(
                session, "search_issues", {"query": "repo:klaffat/a repo:jonathanmoregard/b"}
            )
            self.assertTrue(result.isError)
            self.assertIn("klaffat", text)
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")

    async def test_unlisted_tool_is_refused_not_forwarded(self) -> None:
        async with self.open_mux(
            ["--label", "klaffat", "--extra-tools", "merge_pull_request"]
        ) as session:
            # The default child would echo any name it is sent, so only the
            # mux's own allow-list can stop this call.
            result, text = await self.call(
                session, "merge_pull_request", {"owner": "jonathanmoregard", "repo": "x"}
            )
        self.assertTrue(result.isError)
        self.assertNotIn('"label"', text)

    async def test_a_non_default_base_never_reaches_the_child(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            # The default child echoes whatever it is sent, so a forwarded call
            # is visible in the reply. Nothing but the mux can stop this one.
            result, text = await self.call(
                session,
                "create_pull_request",
                {"owner": "jonathanmoregard", "repo": "x", "base": "feat/sneaky"},
            )
        self.assertTrue(result.isError)
        self.assertIn("feat/sneaky", text)
        self.assertNotIn('"label"', text)

    async def test_an_unresolvable_default_branch_forwards_the_call(self) -> None:
        # A child that cannot answer the lookup is what a brand-new repository,
        # a rate limit or an offline moment looks like from here. The call must
        # go through: this rule is convenience,
        # and it is also the control proving the arm above refused for the right
        # reason rather than because the lookup happened to fail.
        async with self.open_mux(
            ["--label", "klaffat"], shared_extra=["--no-repo-search"]
        ) as session:
            text = await self.answered_by(
                session,
                "create_pull_request",
                {"owner": "jonathanmoregard", "repo": "brand-new", "base": "feat/x"},
            )
        self.assertEqual(text, "personal")

    async def test_the_default_base_is_forwarded_as_before(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            text = await self.answered_by(
                session,
                "create_pull_request",
                {"owner": "jonathanmoregard", "repo": "x", "base": "main"},
            )
        # The default child answers with its own label, so the call was forwarded.
        self.assertEqual(text, "personal")

    async def test_child_with_different_tool_set_is_fenced_off(self) -> None:
        async with self.open_mux(
            ["--label", "klaffat", "--extra-tools", "merge_pull_request"]
        ) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            self.assertNotIn("merge_pull_request", names)
            result, text = await self.call(
                session, "list_pull_requests", {"owner": "klaffat", "repo": "app"}
            )
            self.assertTrue(result.isError)
            self.assertIn("merge_pull_request", text)
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")

    async def test_child_that_fails_to_start_yields_errors_not_a_dead_mux(self) -> None:
        async with self.open_mux(["--fail-start"]) as session:
            result, text = await self.call(
                session, "list_pull_requests", {"owner": "klaffat", "repo": "app"}
            )
            self.assertTrue(result.isError)
            self.assertIn("klaffat", text)
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")

    async def test_child_crash_mid_call_yields_error_not_a_dead_mux(self) -> None:
        async with self.open_mux(["--label", "klaffat"]) as session:
            result, _ = await self.call(session, "crash", {"owner": "klaffat"})
            self.assertTrue(result.isError)
            self.assertEqual(await self.answered_by(session, "get_me", {}), "personal")
            result, _ = await self.call(session, "get_me", {"owner": "klaffat"})
            self.assertTrue(result.isError)


if __name__ == "__main__":
    unittest.main()

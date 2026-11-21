#!/usr/bin/env python3
"""Contract tests for the jobs broker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


SERVER = Path(__file__).resolve().parents[3] / "runtime" / "mcps" / "jobs-broker" / "server.py"


def load_server():
    name = f"jobs_broker_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ident(path: Path) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def values(props: list[str], key: str) -> list[str]:
    return [p.split("=", 1)[1] for p in props if p.split("=", 1)[0] == key]


SANDBOX = {
    "enabled": True, "autoAllowBashIfSandboxed": True,
    "network": {"allowedDomains": ["github.com", "cache.nixos.org"], "allowAllUnixSockets": True,
                "tlsTerminate": {}},
    "filesystem": {"allowWrite": ["~/Repos", "~/worktrees", "~/.cache/nix", "/srv/other"],
                   "denyRead": ["/run/dbus", "/tmp/kitty.sock-*"],
                   "denyWrite": ["~/Repos/main/release.key"]},
}


def deny_rules(deny: object) -> dict:
    # arftl-allow: permission-widened (deny rules only: what a job must not touch)
    return {"permissions": {"deny": deny}}


class Home(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        for d in ("Repos/main/.git", "worktrees/wt", ".ssh", ".config/git", ".cache/nix", ".claude"):
            (self.home / d).mkdir(parents=True)
        self.repo = self.home / "Repos" / "main"
        self.project = self.home / "Repos" / "session"
        self.project.mkdir()
        (self.repo / ".git" / "config").write_text("")
        self.write_settings({"sandbox": SANDBOX})
        self.sources = [(self.home / ".claude" / "settings.json", True)]
        self.server.settings_sources = lambda home: self.sources

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_settings(self, settings: dict, path: Path | None = None) -> None:
        (path or self.home / ".claude" / "settings.json").write_text(json.dumps(settings))

    def start_args(self, **over) -> dict:
        return {"repo": str(self.repo), "argv": ["make", "check"], "slug": "check", **over}

    def job(self, **over):
        return self.server.validate_start(self.start_args(**over), self.home, self.project)


class SlugTests(Home):
    def test_bad_slugs_refused(self) -> None:
        for slug in ["", "../x", "a/b", "x y", "-x", "a.b", "x" * 49, "a\nb", 5, None]:
            with self.subTest(slug=slug), self.assertRaises(RuntimeError):
                self.server.unit_name(slug)

    def test_unit_carries_broker_prefix(self) -> None:
        self.assertEqual(self.server.unit_name("nix-build_1"), "claude-job-nix-build_1")


class StartValidationTests(Home):
    def test_unknown_fields_refused(self) -> None:
        for extra in ({"unit": "claude-pull"}, {"env": {"A": "1"}}, {"properties": ["X=1"]}):
            with self.subTest(extra=extra), self.assertRaisesRegex(RuntimeError, "unexpected"):
                self.job(**extra)

    def test_argv_bounds(self) -> None:
        for argv in [[], "make", [1], ["a\0b"], ["x"] * 65, ["x" * 4097]]:
            with self.subTest(argv=str(argv)[:40]), self.assertRaises(RuntimeError):
                self.job(argv=argv)

    def test_runtime_has_ceiling(self) -> None:
        cap = self.server.MAX_RUNTIME_SEC
        for bad in (0, 9, cap + 1, True, "600"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.job(max_runtime_sec=bad)
        self.assertEqual(self.job().runtime, self.server.DEFAULT_RUNTIME_SEC)
        self.assertEqual(self.job(max_runtime_sec=cap).runtime, cap)

    def test_profile_must_be_known(self) -> None:
        with self.assertRaises(RuntimeError):
            self.job(profile="host")


class RepoPathTests(Home):
    def refused(self, raw) -> None:
        with self.assertRaises(RuntimeError):
            self.job(repo=raw)

    def test_refuses_home_roots_and_outside(self) -> None:
        for raw in [str(self.home), str(self.home / "Repos"), str(self.home / "worktrees"),
                    str(self.home / ".ssh"), "/etc", "Repos/main", "",
                    str(self.home / "Repos" / "main" / ".." / ".." / ".ssh"),
                    str(self.home / "Repos" / "absent")]:
            with self.subTest(raw=raw):
                self.refused(raw)

    def test_refuses_file(self) -> None:
        (self.home / "Repos" / "file").write_text("x")
        self.refused(str(self.home / "Repos" / "file"))

    def test_refuses_symlinked_last_component_even_into_a_root(self) -> None:
        (self.home / "worktrees" / "link").symlink_to(self.repo)
        self.refused(str(self.home / "worktrees" / "link"))

    def test_refuses_symlinked_parent_escaping_the_roots(self) -> None:
        (self.home / "worktrees" / "esc").symlink_to(self.home)
        self.refused(str(self.home / "worktrees" / "esc" / ".ssh"))

    def test_symlinked_dot_git_refused(self) -> None:
        wt = self.home / "worktrees" / "wt"
        (wt / ".git").symlink_to(self.repo / ".git")
        self.refused(str(wt))

    def test_worktree_git_dir_outside_roots_refused(self) -> None:
        wt = self.home / "worktrees" / "wt"
        (wt / ".git").write_text(f"gitdir: {self.home / '.ssh'}\n")
        self.refused(str(wt))

    def test_refuses_characters_systemd_parses_in_a_path_list(self) -> None:
        for char in (" ", "\t", "\n", '"', "'", "\\", ":", "%", ";", "$"):
            repo = self.home / "Repos" / f"a{char}b"
            repo.mkdir()
            with self.subTest(char=char):
                self.refused(str(repo))

    def test_refuses_a_clean_argument_whose_real_path_is_not(self) -> None:
        spaced = self.home / "Repos" / "sp ace"
        (spaced / "r").mkdir(parents=True)
        (self.home / "Repos" / "clean").symlink_to(spaced)
        self.refused(str(self.home / "Repos" / "clean" / "r"))

    def make_git_dir(self, path: Path) -> Path:
        for d in ("objects", "refs", "hooks"):
            (path / d).mkdir(parents=True, exist_ok=True)
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        return path

    def test_refuses_any_path_inside_a_git_dir(self) -> None:
        bare = self.make_git_dir(self.home / "Repos" / "bare")
        self.make_git_dir(self.repo / ".git")
        (self.home / "Repos" / "into-git").symlink_to(self.repo / ".git")
        for raw in [self.repo / ".git", self.repo / ".git" / "hooks", bare, bare / "hooks",
                    self.home / "Repos" / "into-git" / "hooks"]:
            with self.subTest(raw=raw):
                self.refused(str(raw))

    def test_repo_must_be_the_top_level_of_a_work_tree(self) -> None:
        (self.repo / "src").mkdir()
        (self.home / "Repos" / "plain").mkdir()
        for raw in (self.repo / "src", self.home / "Repos" / "plain"):
            with self.subTest(raw=raw), self.assertRaisesRegex(RuntimeError, "work tree"):
                self.job(repo=str(raw))

    def test_symlinked_nested_dot_git_refused(self) -> None:
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / ".git").symlink_to(self.repo / ".git")
        self.refused(str(self.repo))


class SrtPolicyTests(Home):
    def policy(self, **over) -> dict:
        return self.job(**over).policy

    def test_network_is_the_sessions(self) -> None:
        self.assertEqual(self.policy()["network"], {"deniedDomains": [], **SANDBOX["network"]})

    def test_reads_are_denied_as_in_the_session(self) -> None:
        self.assertEqual(self.policy()["filesystem"]["denyRead"],
                         SANDBOX["filesystem"]["denyRead"])

    def test_writes_reach_only_the_repo_and_job_caches_in_every_profile(self) -> None:
        caches = [str(p) for p in self.server.job_caches(self.home).values()]
        for profile in self.server.PROFILES:
            with self.subTest(profile=profile):
                self.assertEqual(self.policy(profile=profile)["filesystem"]["allowWrite"],
                                 [str(self.repo), *caches])

    def test_job_caches_include_an_xdg_cache_home_of_their_own(self) -> None:
        caches = self.server.job_caches(self.home)
        self.assertEqual(caches["XDG_CACHE_HOME"].parent, self.home / ".cache/claude-jobs")

    def test_job_caches_are_never_the_hosts_or_the_sessions(self) -> None:
        fs = {**SANDBOX["filesystem"], "allowWrite": [*SANDBOX["filesystem"]["allowWrite"],
                                                      "~/.cargo", "~/.cache/uv"]}
        self.write_settings({"sandbox": {**SANDBOX, "filesystem": fs}})
        session = [Path(self.server._expand_home(p, self.home)) for p in fs["allowWrite"]]
        for cache in self.server.job_caches(self.home).values():
            with self.subTest(cache=cache):
                self.assertFalse([s for s in session if s == cache or s in cache.parents])
                self.assertFalse([s for s in session if cache in s.parents])

    def test_project_claude_config_and_every_git_dir_are_write_denied(self) -> None:
        nested = self.make_git_dir(self.repo / "vendor" / "inner" / ".git")
        bare = self.make_git_dir(self.repo / "tools" / "x.git")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
        deny = self.policy()["filesystem"]["denyWrite"]
        for path in (self.repo / ".claude", self.repo / ".git", nested, bare,
                     self.repo / "sub" / ".git"):
            with self.subTest(path=path):
                self.assertIn(str(path), deny)
        self.assertIn("~/Repos/main/release.key", deny)

    def make_git_dir(self, path: Path) -> Path:
        for d in ("objects", "refs", "hooks"):
            (path / d).mkdir(parents=True, exist_ok=True)
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        return path

    def with_deny(self, *rules: str, extra: dict | None = None) -> dict:
        self.write_settings({"sandbox": SANDBOX, **deny_rules(list(rules)), **(extra or {})})
        return self.policy()

    def test_read_deny_rules_are_denied_reads_as_claude_resolves_them(self) -> None:
        fs = self.with_deny("Read(//srv/secret)", "Read(~/.aws/**)", "Read(.env)",
                            "Read(/private/x)", "Read(**/*.pem)")["filesystem"]
        for path in ("/srv/secret", f"{self.home}/.aws/**", f"{self.repo}/.env",
                     f"{self.home}/.claude/private/x", f"{self.repo}/**/*.pem"):
            with self.subTest(path=path):
                self.assertIn(path, fs["denyRead"])
        self.assertNotIn("/srv/secret", fs["denyWrite"])

    def test_edit_and_write_deny_rules_are_denied_writes(self) -> None:
        fs = self.with_deny("Edit(//srv/locked)", "Write(~/.zshrc)", "Edit(build/out.txt)")["filesystem"]
        for path in ("/srv/locked", f"{self.home}/.zshrc", f"{self.repo}/build/out.txt"):
            with self.subTest(path=path):
                self.assertIn(path, fs["denyWrite"])
        self.assertNotIn("/srv/locked", fs["denyRead"])

    def test_other_tools_rules_do_not_reach_the_filesystem_policy(self) -> None:
        fs = self.with_deny("Bash(rm *)", "Grep(//srv/g)", "mcp__x__y", "WebSearch")["filesystem"]
        self.assertEqual(fs["denyRead"], SANDBOX["filesystem"]["denyRead"])
        self.assertNotIn("/srv/g", fs["denyWrite"])

    def test_webfetch_domain_deny_rules_are_denied_domains(self) -> None:
        net = self.with_deny("WebFetch(domain:evil.example)", "WebFetch")["network"]
        self.assertIn("evil.example", net["deniedDomains"])

    def test_deny_write_globs_become_the_paths_they_match_in_the_repo(self) -> None:
        for d in ("a/held", "b/c/held", "b/keep"):
            (self.repo / d).mkdir(parents=True)
        (self.repo / "b/keep/x.lock").write_text("")
        deny = self.with_deny("Edit(//**/held/**)", "Edit(**/*.lock)")["filesystem"]["denyWrite"]
        for path in (self.repo / "a/held", self.repo / "b/c/held", self.repo / "b/keep/x.lock"):
            with self.subTest(path=path):
                self.assertIn(str(path), deny)
        self.assertNotIn(str(self.repo / "b/keep"), deny)
        self.assertFalse([p for p in deny if any(c in p for c in "*?[")], deny)

    def test_read_deny_globs_srt_cannot_walk_are_denied_where_they_match_in_the_repo(self) -> None:
        (self.repo / "a/held").mkdir(parents=True)
        (self.repo / "a/keep").mkdir(parents=True)
        deny = self.with_deny("Read(//**/held/**)", "Read(//**/.git/**)")["filesystem"]["denyRead"]
        self.assertIn(str(self.repo / "a/held"), deny)
        self.assertNotIn(str(self.repo / "a/keep"), deny)
        self.assertFalse([p for p in deny if p.startswith(f"{self.repo}/.git")], deny)

    def test_read_deny_globs_srt_drops_are_named_not_silently_passed(self) -> None:
        self.write_settings({"sandbox": SANDBOX, **deny_rules(["Read(//**/.git/**)", "Read(**/*.pem)"])})
        self.assertEqual(self.job().unenforced, ["/**/.git/**"])

    def test_managed_settings_denies_are_merged(self) -> None:
        managed = self.home / "managed-settings.json"
        self.write_settings({**deny_rules(["Read(//srv/managed)"]),
                             "sandbox": {"filesystem": {"denyWrite": ["/srv/mw"]}}}, managed)
        self.sources = [*self.sources, (managed, False), (self.home / "absent.json", False)]
        fs = self.policy()["filesystem"]
        self.assertIn("/srv/managed", fs["denyRead"])
        self.assertIn("/srv/mw", fs["denyWrite"])

    def test_project_deny_rules_are_merged_and_project_allows_never_widen(self) -> None:
        (self.repo / ".claude").mkdir()
        # arftl-allow: permission-widened (a project allow the broker must ignore)
        self.write_settings({"permissions": {
            "deny": ["Read(./reports/held/**)", "Edit(build/locked)", "Read(/abs-looking)"],
            "allow": ["Edit(//srv/widened/**)"]},
            "sandbox": {"filesystem": {"allowWrite": ["/srv/widened"], "denyRead": ["/srv/pd"]},
                        "network": {"allowedDomains": ["evil.example"]}}},
            self.repo / ".claude/settings.local.json")
        self.write_settings(deny_rules(["Read(//srv/shared)"]), self.repo / ".claude/settings.json")
        policy = self.policy()
        fs = policy["filesystem"]
        for path in (f"{self.repo}/reports/held", "/srv/shared", "/srv/pd",
                     f"{self.repo}/abs-looking", f"{self.repo}/.claude/abs-looking"):
            with self.subTest(path=path):
                self.assertIn(path, [p.removesuffix("/**") for p in fs["denyRead"]])
        self.assertIn(f"{self.repo}/build/locked", fs["denyWrite"])
        self.assertNotIn("/srv/widened", fs["allowWrite"])
        self.assertNotIn("evil.example", policy["network"]["allowedDomains"])

    def test_the_sessions_own_project_denies_reach_a_job_in_another_repo(self) -> None:
        (self.project / ".claude").mkdir()
        self.write_settings(deny_rules([f"Read(/{self.repo}/s.txt)", "Edit(build/out)"]),
                            self.project / ".claude/settings.json")
        fs = self.policy()["filesystem"]
        self.assertIn(f"{self.repo}/s.txt", fs["denyRead"])
        self.assertIn(f"{self.project}/build/out", fs["denyWrite"])

    def test_unreadable_session_project_settings_refuse_to_start(self) -> None:
        (self.project / ".claude").mkdir()
        (self.project / ".claude/settings.local.json").write_text("{not json")
        with self.assertRaisesRegex(RuntimeError, f"{self.project}/.claude/settings.local.json"):
            self.job()

    def test_unreadable_project_settings_refuse_to_start(self) -> None:
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/settings.local.json").write_text("{not json")
        with self.assertRaisesRegex(RuntimeError, "settings.local.json"):
            self.job()

    def test_deny_write_globs_match_as_gitignore_does(self) -> None:
        (self.repo / "docs").mkdir()
        for name in ("v1.txt", "va.txt", "Notes.md", "notes.md"):
            (self.repo / "docs" / name).write_text("")
        deny = self.with_deny("Edit(**/v[[:digit:]].txt)", "Edit(docs/[[:upper:]]*.md)"
                              )["filesystem"]["denyWrite"]
        self.assertIn(str(self.repo / "docs/v1.txt"), deny)
        self.assertIn(str(self.repo / "docs/Notes.md"), deny)
        self.assertNotIn(str(self.repo / "docs/va.txt"), deny)
        self.assertNotIn(str(self.repo / "docs/notes.md"), deny)

    def test_untranslatable_settings_refuse_to_start(self) -> None:
        cases = {"bare Read": ["Read"], "empty Edit": ["Edit()"], "escape": ["Read(a\\*b)"],
                 "not a string": [{"Read": "x"}], "bare Edit": ["Edit"]}
        for name, deny in cases.items():
            self.write_settings({"sandbox": SANDBOX, **deny_rules(deny)})
            with self.subTest(name), self.assertRaises(RuntimeError):
                self.job()
        self.write_settings({"sandbox": SANDBOX, **deny_rules("Read(x)")})
        with self.assertRaises(RuntimeError):
            self.job()

    def test_unreadable_managed_settings_refuse_to_start(self) -> None:
        managed = self.home / "managed-settings.json"
        managed.write_text("{not json")
        self.sources = [*self.sources, (managed, False)]
        with self.assertRaisesRegex(RuntimeError, "managed-settings"):
            self.job()

    def test_nothing_starts_without_the_sessions_sandbox_policy(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        for content in ({"model": "haiku"}, {"sandbox": "on"}, None):
            if content is None:
                settings.unlink()
            else:
                self.write_settings(content)
            with self.subTest(content=content), self.assertRaisesRegex(RuntimeError, "sandbox"):
                self.job()


HARDENING = {
    "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateUsers=self",
    "NoNewPrivileges=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "SystemCallFilter=@system-service @mount", "SystemCallErrorNumber=EPERM", "PrivatePIDs=yes",
    "RestrictNamespaces=user mnt pid net", "PrivateIPC=yes", "PrivateDevices=yes",
    "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectClock=yes",
    "LockPersonality=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
    "CapabilityBoundingSet=", "RemainAfterExit=yes", "Type=oneshot",
}


class PropertyTests(Home):
    def props(self, profile: str, **over) -> list[str]:
        return self.server.properties(self.job(profile=profile, **over), self.home)

    def test_hardening_present_in_every_profile(self) -> None:
        for profile in ("repo", "nix"):
            with self.subTest(profile=profile):
                props = self.props(profile)
                self.assertLessEqual(HARDENING, set(props))
                for hidden in ("/run/github-app", "/run/dbus", "/run/nscd", "/run/agenix",
                               "/run/agenix.d", "/run/secrets"):
                    self.assertIn(f"InaccessiblePaths=-{hidden}", props)

    def test_repo_profile_writes_only_the_repo_and_job_caches(self) -> None:
        props = self.props("repo")
        caches = {str(p) for p in self.server.job_caches(self.home).values()}
        self.assertEqual([p for p in values(props, "BindPaths") if p not in caches
                          and p != str(self.repo) and not p.startswith(f"{self.repo}/")], [])
        self.assertIn(f"-{self.home}/.config/git", values(props, "BindReadOnlyPaths"))
        self.assertEqual(values(props, "WorkingDirectory"), [str(self.repo)])
        self.assertNotIn("Slice=ram-heavy.slice", props)

    def test_nix_profile_adds_the_heavy_slice_and_no_host_cache(self) -> None:
        props = self.props("nix")
        self.assertEqual(values(props, "BindPaths"), values(self.props("repo"), "BindPaths"))
        self.assertIn(f"-{self.home}/.nix-memory-pressure", values(props, "BindReadOnlyPaths"))
        self.assertIn("Slice=ram-heavy.slice", props)
        self.assertIn("OOMPolicy=kill", props)

    def test_runtime_and_failure_recorder(self) -> None:
        props = self.props("repo", max_runtime_sec=600)
        self.assertEqual(values(props, "TimeoutStartSec"), ["600"])
        log = self.home / ".local/state/claude-jobs/claude-job-check.log"
        self.assertEqual(values(props, "StandardOutput"), [f"append:{log}"])
        record, sweep = values(props, "ExecStopPost")
        self.assertTrue(record.startswith("+"), record)
        self.assertTrue(record.endswith(f"record-user-unit-failure claude-job-check {log}"), record)
        self.assertEqual(values(props, "ExecStartPost"), [sweep])
        self.assertTrue(sweep.startswith("+") and sweep.endswith("sweep claude-job-check"), sweep)

    def test_every_bound_repo_path_is_identity_checked(self) -> None:
        wt = self.home / "worktrees" / "wt"
        admin = self.repo / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        job = self.job(repo=str(wt))
        props = self.server.properties(job, self.home)
        bound = {v.lstrip("-") for key in ("BindPaths", "BindReadOnlyPaths") for v in values(props, key)}
        cmd = self.server.start_argv(job, self.home)
        checked = set(cmd[cmd.index("jobs-broker") + 2:cmd.index("--", cmd.index("jobs-broker")):2])
        under_roots = {p for p in bound if p.startswith((str(self.home / "Repos"),
                                                         str(self.home / "worktrees")))}
        self.assertTrue(under_roots)
        self.assertEqual(under_roots, checked)

    def test_argv_runs_under_srt_with_the_policy_bound_read_only(self) -> None:
        job = self.job()
        cmd = self.server.start_argv(job, self.home)
        policy = self.server.policy_path(self.home, job.unit)
        self.assertEqual(cmd[-4:], [str(policy), "--", "make", "check"])
        self.assertEqual(cmd[-7:-4], ["--", self.server.SRT, "-s"])
        props = self.server.properties(job, self.home)
        self.assertIn(str(policy), values(props, "BindReadOnlyPaths"))
        self.assertNotIn(str(policy), values(props, "BindPaths"))

    def test_start_writes_the_policy_the_job_runs_under(self) -> None:
        from unittest import mock
        started = []
        self.server._show = lambda unit: {"LoadState": "not-found"}
        real_run = subprocess.run
        with mock.patch.object(self.server.subprocess, "run", lambda argv, **kw: (
                started.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
                if argv[0] == "systemd-run" else real_run(argv, **kw))):
            self.server.tool_start(self.start_args(), self.home, self.project)
        job = self.job()
        written = self.server.policy_path(self.home, job.unit)
        self.assertEqual(json.loads(written.read_text()), job.policy)
        self.assertEqual(written.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(started), 1)

    def test_caller_argv_never_reaches_properties(self) -> None:
        argv = ["sh", "-c", "PrivateNetwork=no", "BindPaths=/"]
        job = self.job(argv=argv)
        props = self.server.properties(job, self.home)
        self.assertFalse([p for p in props if "PrivateNetwork=no" in p or p == "BindPaths=/"])
        cmd = self.server.start_argv(job, self.home)
        self.assertEqual(cmd[-len(argv):], argv)
        self.assertEqual(cmd[-len(argv) - 1], "--")
        self.assertIn("--expand-environment=no", cmd)
        self.assertIn("--unit=claude-job-check", cmd)


class SweepTests(Home):
    def sweep(self) -> list:
        job = self.job()
        self.server.write_baseline(job, self.home)
        (self.repo / "new/.claude").mkdir(parents=True)
        (self.repo / "new/.claude/settings.json").write_text("planted\n")
        outside = self.home / "outside"
        (outside / ".claude").mkdir(parents=True)
        (self.repo / "link").symlink_to(outside)
        self.server.sweep(job.unit, self.home)
        return [outside / ".claude", self.repo / "link"]

    def test_moves_only_names_that_appeared_and_never_through_a_symlink(self) -> None:
        kept = self.sweep()
        self.assertFalse((self.repo / "new/.claude").exists())
        self.assertTrue((self.repo / ".git").is_dir())
        for path in kept:
            self.assertTrue(path.exists(), path)

    def test_names_created_where_the_job_could_not_write_are_left_in_place(self) -> None:
        job = self.job()
        self.server.write_baseline(job, self.home)
        owner_made = [".claude/settings.local.json", ".envrc", ".git/modules/x/config"]
        for rel in owner_made:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("owner\n")
        self.assertEqual(self.server.sweep(job.unit, self.home), [])
        for rel in owner_made:
            with self.subTest(rel=rel):
                self.assertEqual((self.repo / rel).read_text(), "owner\n")

    def test_moved_files_stay_readable_and_restorable(self) -> None:
        job = self.job()
        self.server.write_baseline(job, self.home)
        (self.repo / "deep/.claude").mkdir(parents=True)
        (self.repo / "deep/.claude/settings.json").write_text("planted\n")
        self.assertEqual(self.server.sweep(job.unit, self.home), ["deep/.claude"])
        [held] = (self.home / self.server.HELD).glob(f"{job.unit}.*")
        self.assertNotIn("quarantine", str(held))
        self.assertEqual((held / "deep/.claude/settings.json").read_text(), "planted\n")
        restore = self.server.restore_hint(job.unit, self.home)
        self.assertIn(str(held), restore)
        subprocess.run(["sh", "-c", restore.split("restore: ", 1)[1]], check=True)
        self.assertEqual((self.repo / "deep/.claude/settings.json").read_text(), "planted\n")

    def test_a_missing_baseline_moves_nothing(self) -> None:
        job = self.job()
        (self.repo / "new/.claude").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            self.server.sweep(job.unit, self.home)
        self.assertTrue((self.repo / "new/.claude").exists())
        self.assertTrue((self.repo / ".git").is_dir())


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_wrapper(self, want: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", self.server.WRAPPER, "jobs-broker",
                               str(self.dir / "tmp"), str(self.dir), want, "--", *argv],
                              capture_output=True, text=True, timeout=30)

    def test_runs_argv_and_appends_markers(self) -> None:
        ok = self.run_wrapper(ident(self.dir), "echo", "hi")
        self.assertEqual((ok.returncode, ok.stdout), (0, "hi\nMARKER_OK\n"))
        self.assertTrue((self.dir / "tmp").is_dir())
        bad = self.run_wrapper(ident(self.dir), "sh", "-c", "exit 3")
        self.assertEqual((bad.returncode, bad.stdout), (3, "MARKER_FAILED rc=3\n"))

    def test_own_exit_143_is_not_a_stop(self) -> None:
        done = self.run_wrapper(ident(self.dir), "sh", "-c", "exit 143")
        self.assertEqual((done.returncode, done.stdout), (1, "MARKER_FAILED rc=143\n"))

    def test_term_reports_a_stop(self) -> None:
        import signal
        proc = subprocess.Popen(["bash", "-c", self.server.WRAPPER, "jobs-broker", str(self.dir / "tmp"),
                                 str(self.dir), ident(self.dir), "--", "sleep", "30"],
                                stdout=subprocess.PIPE, text=True, start_new_session=True)
        time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 143)
        self.assertIn("MARKER_FAILED stopped", out)

    def test_swapped_directory_refuses_before_argv(self) -> None:
        done = self.run_wrapper("1:1", "touch", str(self.dir / "ran"))
        self.assertEqual(done.returncode, 125)
        self.assertFalse((self.dir / "ran").exists())
        self.assertIn("MARKER_FAILED rc=125", done.stdout)


class OwnUnitTests(Home):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple] = []
        self.loaded = "not-found"

        def fake(*argv, timeout=60):
            self.calls.append(argv)
            out = ""
            if argv[0] == "show":
                out = (f"LoadState={self.loaded}\nActiveState=active\nSubState=exited\n"
                       "Result=success\nExecMainStatus=0\nExecMainStartTimestamp=x\n"
                       "ExecMainExitTimestamp=y\n")
            return subprocess.CompletedProcess(argv, 0, out, "")

        self.server._systemctl = fake

    def test_status_log_stop_touch_only_the_prefixed_unit(self) -> None:
        self.loaded = "loaded"
        self.server.tool_status({"slug": "pull"}, self.home)
        self.server.tool_log({"slug": "pull"}, self.home)
        self.server.tool_stop({"slug": "pull"}, self.home)
        units = {a for argv in self.calls for a in argv if a.endswith(".service")}
        self.assertEqual(units, {"claude-job-pull.service"})
        self.assertEqual([argv[0] for argv in self.calls if argv[0] != "show"],
                         ["stop", "reset-failed"])

    def test_raw_unit_names_refused(self) -> None:
        for args in ({"slug": "pull", "unit": "claude-pull"}, {"unit": "claude-pull"},
                     {"slug": "claude-pull.service"}):
            for tool in (self.server.tool_status, self.server.tool_log, self.server.tool_stop):
                with self.subTest(args=args, tool=tool.__name__), self.assertRaises(RuntimeError):
                    tool(args, self.home)

    def test_stop_refuses_a_unit_that_is_not_loaded(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no job"):
            self.server.tool_stop({"slug": "pull"}, self.home)
        self.assertNotIn("stop", [argv[0] for argv in self.calls])

    def test_start_refuses_a_loaded_unit_and_keeps_its_log(self) -> None:
        self.loaded = "loaded"
        log = self.home / ".local/state/claude-jobs/claude-job-check.log"
        log.parent.mkdir(parents=True)
        log.write_text("previous run\n")
        with self.assertRaisesRegex(RuntimeError, "already"):
            self.server.tool_start(self.start_args(), self.home, self.project)
        self.assertEqual(log.read_text(), "previous run\n")

    def test_log_tail_is_bounded(self) -> None:
        log = self.home / ".local/state/claude-jobs/claude-job-check.log"
        log.parent.mkdir(parents=True)
        log.write_text("".join(f"line {i}\n" for i in range(1000)))
        text = self.server.tool_log({"slug": "check", "lines": 3}, self.home)
        self.assertIn("line 997\nline 998\nline 999", text)
        self.assertNotIn("line 996", text)
        with self.assertRaises(RuntimeError):
            self.server.tool_log({"slug": "check", "lines": self.server.MAX_LOG_LINES + 1},
                                 self.home)


class StdioTests(unittest.TestCase):
    def test_tools_list(self) -> None:
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = subprocess.run([sys.executable, str(SERVER)],
                             input="\n".join(map(json.dumps, msgs)),
                             capture_output=True, text=True, timeout=30).stdout.splitlines()
        tools = json.loads(out[1])["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["start", "status", "log", "stop"])


def user_manager() -> bool:
    if not shutil.which("systemd-run"):
        return False
    probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"],
                           capture_output=True, text=True)
    return probe.returncode == 0 and probe.stdout.strip() != "Version="


@unittest.skipUnless(user_manager(), "no systemd user manager")
class RealUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        # Lets a candidate sandbox-runtime build be proven on the real path before it deploys.
        self.server.SRT = os.environ.get("JOBS_BROKER_TEST_SRT", self.server.SRT)
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.dir = Path(tempfile.mkdtemp(prefix=".jobs-broker-test-", dir=self.home / "worktrees"))
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.project = self.dir / "session"
        self.project.mkdir()
        self.canary = self.dir / "canary"
        self.canary.write_text("CANARY-OUTSIDE-REPO\n")
        self.slug = f"test-{uuid.uuid4().hex[:12]}"
        self.unit = f"claude-job-{self.slug}"
        self.use_settings()
        self.git("init", "-q", cwd=self.repo)

    def use_settings(self, deny: tuple = (), **filesystem) -> None:
        live = json.loads((self.home / ".claude/settings.json").read_text())["sandbox"]
        sandbox = {**live, "filesystem": {**live.get("filesystem", {}), **filesystem}}
        settings = self.dir / "settings.json"
        settings.write_text(json.dumps({"sandbox": sandbox, **deny_rules(list(deny))}))
        self.server.settings_sources = lambda home: [(settings, True)]

    def reset_unit(self) -> None:
        subprocess.run(["systemctl", "--user", "stop", f"{self.unit}.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", f"{self.unit}.service"],
                       capture_output=True)

    def tearDown(self) -> None:
        self.reset_unit()
        state = self.home / ".local/state/claude-jobs"
        for leftover in (state / f"{self.unit}.log", self.server.policy_path(self.home, self.unit),
                         *state.glob(f"{self.unit}.*"),
                         self.home / ".local/state/claude-unit-failures" / f"{self.unit}.json"):
            leftover.unlink(missing_ok=True)
        for held in (self.home / self.server.HELD).glob(f"{self.unit}.*"):
            subprocess.run(["chmod", "-R", "u+rwx", str(held)], check=True)
            shutil.rmtree(held)
        subprocess.run(["chmod", "-R", "u+rwx", str(self.dir)], check=True)
        shutil.rmtree(self.dir)

    def held(self) -> list:
        root = self.home / self.server.HELD
        return sorted(str(p.relative_to(d)) for d in root.glob(f"{self.unit}.*")
                      for p in d.rglob("*") if p.is_file())

    def start(self, script: str, runtime: int = 120, repo: Path | None = None) -> str:
        return self.server.tool_start({"repo": str(repo or self.repo), "argv": ["sh", "-c", script],
                                       "slug": self.slug, "max_runtime_sec": runtime}, self.home, self.project)

    def run_job(self, script: str, repo: Path | None = None) -> str:
        self.start(script, repo=repo)
        return self.status() + "\n" + self.server.tool_log({"slug": self.slug}, self.home)

    def git(self, *args: str, cwd: Path) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                              check=True, capture_output=True, text=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout

    def main_and_worktree(self) -> Path:
        self.git("init", "-q", cwd=self.repo)
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        wt = self.dir / "wt"
        self.git("worktree", "add", "-q", str(wt), cwd=self.repo)
        return wt

    def snapshot(self, *paths: Path) -> dict:
        return {p: p.read_bytes() if p.is_file() else sorted(os.listdir(p)) if p.is_dir() else None
                for p in paths}

    def record(self) -> Path:
        return self.home / ".local/state/claude-unit-failures" / f"{self.unit}.json"

    def wait_record(self) -> dict:
        deadline = time.monotonic() + 10
        while not self.record().exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        return json.loads(self.record().read_text())

    def status(self) -> str:
        return self.server.tool_status({"slug": self.slug, "wait_sec": 60}, self.home)

    def test_job_writes_repo_and_cannot_see_outside_it(self) -> None:
        self.start(f"echo printed; echo made > out.txt; cat {self.canary}; "
                   f"test -e /run/user/{os.getuid()} && echo RUNTIME-DIR-VISIBLE; exit 0")
        self.assertIn("result=success status=0", self.status())
        self.assertEqual((self.repo / "out.txt").read_text(), "made\n")
        log = self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("printed", log)
        self.assertIn("MARKER_OK", log)
        self.assertNotIn("CANARY-OUTSIDE-REPO", log)
        self.assertNotIn("RUNTIME-DIR-VISIBLE", log)

    def test_job_cannot_write_what_the_session_sandbox_protects(self) -> None:
        self.git("init", "-q", cwd=self.repo)
        self.git("init", "-q", "--bare", "x.git", cwd=self.repo)
        bare_config = self.snapshot(self.repo / "x.git/config")
        before = sorted(os.listdir(self.repo))
        planted = [".claude/settings.local.json", ".claude/settings.json", ".mcp.json",
                   ".vscode/x", ".gitmodules", ".git/hooks/x", ".bashrc"]
        script = "".join(f"mkdir -p $(dirname {p}); echo planted > {p}; " for p in planted)
        out = self.run_job(script + "echo '[core]' >> x.git/config; echo kept > normal.txt; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertEqual((self.repo / "normal.txt").read_text(), "kept\n")
        for p in planted:
            with self.subTest(path=p):
                self.assertFalse((self.repo / p).exists(), out)
        self.assertEqual(self.snapshot(self.repo / "x.git/config"), bare_config, out)
        self.assertEqual(sorted(os.listdir(self.repo)), sorted([*before, "normal.txt"]), out)

    def test_job_cannot_write_outside_the_repo(self) -> None:
        out = self.run_job(f"echo escaped > {self.dir}/outside; echo escaped >> {self.canary}; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertFalse((self.dir / "outside").exists(), out)
        self.assertEqual(self.canary.read_text(), "CANARY-OUTSIDE-REPO\n")

    def test_job_cannot_read_a_path_the_session_sandbox_denies(self) -> None:
        (self.repo / "denied").mkdir()
        (self.repo / "denied/f").write_text("CANARY-DENIED-READ\n")
        (self.repo / "allowed").write_text("CANARY-ALLOWED-READ\n")
        self.use_settings(denyRead=[str(self.repo / "denied")])
        out = self.run_job("cat denied/f; cat allowed; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertIn("CANARY-ALLOWED-READ", out)
        self.assertNotIn("CANARY-DENIED-READ", out)

    def test_job_cannot_read_what_a_read_deny_rule_names(self) -> None:
        (self.repo / "abs.txt").write_text("CANARY-ABS-RULE\n")
        (self.repo / "rel.txt").write_text("CANARY-REL-RULE\n")
        self.use_settings(deny=(f"Read(/{self.repo}/abs.txt)", "Read(rel.txt)"))
        out = self.run_job("cat abs.txt; cat rel.txt; echo readable > visible.txt; cat visible.txt; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertIn("readable", out)
        self.assertNotIn("CANARY-ABS-RULE", out)
        self.assertNotIn("CANARY-REL-RULE", out)

    def test_job_cannot_write_what_an_edit_deny_glob_names(self) -> None:
        (self.repo / "sub/held").mkdir(parents=True)
        self.use_settings(deny=("Edit(//**/held/**)",))
        out = self.run_job("echo planted > sub/held/x; echo kept > sub/y; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertEqual((self.repo / "sub/y").read_text(), "kept\n")
        self.assertFalse((self.repo / "sub/held/x").exists(), out)

    def test_job_cannot_read_what_a_read_deny_glob_names(self) -> None:
        (self.repo / ".env.local").write_text("CANARY-GLOB-DENIED\n")
        (self.repo / "env.txt").write_text("CANARY-NOT-DENIED\n")
        self.use_settings(deny=(f"Read(/{self.repo}/.env.*)",))
        out = self.run_job("cat .env.local; cat env.txt; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertIn("CANARY-NOT-DENIED", out)
        self.assertNotIn("CANARY-GLOB-DENIED", out)

    # srt, like upstream and Claude Code's bundled copy, holds an absent protected
    # name with an empty mount-point file the host sees while the job runs. The
    # owner's git and nix must still read them; the job must not fill them.
    def test_absent_protected_names_stay_empty_and_readable_for_the_owner_mid_job(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.start("(echo planted > .gitmodules) 2>/dev/null && echo WROTE; "
                   "git status --porcelain >/dev/null || echo GIT-FAILED; sleep 3; exit 0")
        deadline = time.monotonic() + 10
        while "activating" not in self.server.tool_status({"slug": self.slug}, self.home):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)
        time.sleep(1)
        gitmodules = self.repo / ".gitmodules"
        mid_job = gitmodules.read_bytes() if gitmodules.exists() else b""
        host_status = self.git("status", "--porcelain", cwd=self.repo).splitlines()
        sizes = {line: (self.repo / line[3:]).lstat() for line in host_status}
        out = self.status() + "\n" + self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("MARKER_OK", out)
        self.assertNotIn("WROTE", out)
        self.assertNotIn("GIT-FAILED", out)
        self.assertEqual(mid_job, b"")
        for line, st in sizes.items():
            with self.subTest(entry=line):
                self.assertTrue(line.startswith("?? "), host_status)
                self.assertTrue(stat.S_ISREG(st.st_mode))
                self.assertEqual(st.st_size, 0)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.repo), "")

    def test_root_anchored_read_deny_globs_hold_in_the_repo_and_git_still_reads(self) -> None:
        (self.repo / "sub/held").mkdir(parents=True)
        (self.repo / "sub/held/f").write_text("CANARY-ROOT-GLOB\n")
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.use_settings(deny=("Read(//**/held/**)", "Read(//**/.git/**)"))
        out = self.run_job("cat sub/held/f; git log --format=LOGGED-%s; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertIn("LOGGED-init", out)
        self.assertNotIn("CANARY-ROOT-GLOB", out)

    def test_nix_profile_job_cannot_write_the_hosts_nix_cache(self) -> None:
        canary = self.home / ".cache/nix" / f"jobs-broker-test-{uuid.uuid4().hex[:8]}"
        try:
            self.server.tool_start({"repo": str(self.repo), "slug": self.slug, "profile": "nix",
                                    "argv": ["sh", "-c", f"echo planted > {canary}; exit 0"]},
                                   self.home, self.project)
            self.assertIn("result=success", self.status())
            self.assertFalse(canary.exists())
        finally:
            canary.unlink(missing_ok=True)

    def test_files_the_owner_creates_mid_job_where_the_job_cannot_write_stay(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.start("sleep 4; exit 0")
        deadline = time.monotonic() + 10
        while "activating" not in self.server.tool_status({"slug": self.slug}, self.home):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)
        time.sleep(1)
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude/settings.local.json").write_text('{"owner": true}\n')
        self.assertIn("result=success", self.status())
        self.assertEqual((self.repo / ".claude/settings.local.json").read_text(), '{"owner": true}\n')
        self.assertEqual(self.held(), [])

    def plant(self, *paths: str) -> str:
        return "".join(f"mkdir -p $(dirname {p}) 2>/dev/null; (echo planted > {p}) 2>/dev/null "
                       f"&& echo WROTE-{p}; " for p in paths)

    def test_job_cannot_write_execute_later_files_at_any_depth(self) -> None:
        for d in ("sub/.claude/skills", "sub/.codex", "a/b/c/d/e/f/.vscode", "w/.agents", "w/.cursor"):
            (self.repo / d).mkdir(parents=True)
        (self.repo / "sub/.envrc").write_text("orig\n")
        planted = ["sub/.claude/settings.json", "sub/.claude/settings.local.json",
                   "sub/.claude/skills/x.md", "sub/.codex/hooks.json", "a/b/c/d/e/f/.vscode/tasks.json",
                   "w/.agents/x", "w/.cursor/mcp.json", ".codex/hooks.json", ".agents/x", ".envrc"]
        out = self.run_job(self.plant(*planted) + "echo x >> sub/.envrc; echo kept > normal.txt; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertEqual((self.repo / "normal.txt").read_text(), "kept\n")
        self.assertEqual((self.repo / "sub/.envrc").read_text(), "orig\n", out)
        for p in planted:
            with self.subTest(path=p):
                self.assertNotIn(f"WROTE-{p}", out)
                self.assertFalse((self.repo / p).exists(), out)

    def test_execute_later_names_a_job_creates_are_moved_out_after_it(self) -> None:
        (self.repo / "keep").mkdir()
        created = ("git init -q sub && echo planted > sub/.git/hooks/post-checkout && "
                   "mkdir -p deep/x/.claude && echo planted > deep/x/.claude/settings.json && "
                   "echo planted > deep/.envrc && echo kept > keep/f; ")
        for rc in (0, 1):
            self.reset_unit()
            with self.subTest(rc=rc):
                out = self.run_job(created + f"exit {rc}")
                deadline = time.monotonic() + 10
                while (self.repo / "sub/.git").exists() and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertEqual((self.repo / "keep/f").read_text(), "kept\n", out)
                for p in ("sub/.git", "deep/x/.claude", "deep/.envrc"):
                    self.assertFalse((self.repo / p).exists(), out)
                held = self.held()
                for p in ("sub/.git/hooks/post-checkout", "deep/x/.claude/settings.json", "deep/.envrc"):
                    self.assertIn(p, held)
                self.assertIn("deep/.envrc", self.server.tool_log({"slug": self.slug}, self.home))
                self.assertIn("3 files that run later", self.status())
                self.assertTrue((self.repo / ".git/HEAD").is_file())
                for held_dir in (self.home / self.server.HELD).glob(f"{self.unit}.*"):
                    shutil.rmtree(held_dir)

    def test_a_git_dir_a_job_makes_is_moved_out_however_git_would_find_it(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        out = self.run_job(
            "git init -q --bare linked && rm linked/HEAD && ln -s refs/heads/main linked/HEAD && "
            "git --git-dir=linked config core.pager 'touch PWNED-linked; cat' && "
            "mkdir common && echo 'ref: refs/heads/main' > common/HEAD && "
            "echo ../linked > common/commondir; exit 0")
        self.assertIn("MARKER_OK", out)
        for name in ("linked", "common"):
            with self.subTest(name=name):
                self.assertFalse((self.repo / name).exists(), out)
                self.assertIn(f"{name} appeared during the job", out)

    def test_names_under_directories_the_job_closed_to_the_owner_are_moved_out(self) -> None:
        out = self.run_job("mkdir -p d/x/.claude && echo '{\"hooks\": {}}' > d/x/.claude/settings.json "
                           "&& chmod 0100 d && mkdir -p e/.claude && echo x > e/.claude/settings.json "
                           "&& chmod 0500 e && mkdir -p f/.claude && echo x > f/.claude/settings.json "
                           "&& chmod 0500 f/.claude && chmod 0500 . ; exit 0")
        self.assertIn("result=success", out)
        for p in ("d/x/.claude", "e/.claude", "f/.claude"):
            with self.subTest(path=p):
                self.assertFalse((self.repo / p).exists(), out)
                self.assertIn(f"{p}/settings.json", self.held())

    def test_a_name_the_sweep_cannot_reach_fails_the_job_and_every_move_is_recorded(self) -> None:
        deep = "/".join(["d"] * (self.server.MAX_SWEEP_DEPTH + 2))
        plant = "".join(f"mkdir -p a{i}/.claude && echo x > a{i}/.claude/settings.json && "
                        for i in range(4))
        out = self.run_job(plant + f"mkdir -p {deep}/.claude && echo x > {deep}/.claude/s; exit 0")
        self.assertIn("result=exit-code", out)
        self.assertIn('could not be moved out: "d/d/d/', out)
        self.assertTrue((self.repo / deep / ".claude").exists())
        recorded = self.server.moved_path(self.home, self.unit).read_text()
        self.assertEqual(sorted(self.held()), [f"a{i}/.claude/settings.json" for i in range(4)])
        for i in range(4):
            self.assertIn(f"a{i}/.claude", recorded)
        self.assertIn("restore:", self.server.restore_hint(self.unit, self.home))

    def test_names_the_owner_makes_after_the_job_ended_survive_stop(self) -> None:
        self.start("exit 0")
        self.assertIn("result=success", self.status())
        (self.repo / "sub/.claude").mkdir(parents=True)
        (self.repo / "sub/.claude/settings.json").write_text("owner\n")
        self.server.tool_stop({"slug": self.slug}, self.home)
        self.assertEqual((self.repo / "sub/.claude/settings.json").read_text(), "owner\n")
        self.assertEqual(self.held(), [])

    def test_a_deny_rule_of_the_sessions_own_project_holds_in_a_job_in_another_repo(self) -> None:
        (self.repo / "s.txt").write_text("SESSION-DENIED\n")
        (self.project / ".claude").mkdir()
        (self.project / ".claude/settings.json").write_text(
            json.dumps(deny_rules([f"Read(/{self.repo}/s.txt)"])))
        out = self.run_job("cat s.txt; exit 0")
        self.assertNotIn("SESSION-DENIED", out)

    def test_the_session_project_is_the_one_claude_code_names(self) -> None:
        (self.project / ".claude").mkdir()
        (self.project / ".claude/settings.json").write_text("{not json")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "start", "arguments": {"repo": str(self.repo), "argv": ["true"],
                                           "slug": self.slug}}}]
        done = subprocess.run([sys.executable, str(SERVER)], input=json.dumps(msgs[0]) + "\n",
                              capture_output=True, text=True, timeout=60, cwd=self.dir,
                              env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.project)})
        reply = json.loads(done.stdout)["result"]
        self.assertTrue(reply.get("isError"), reply)
        self.assertIn(f"{self.project}/.claude/settings.json", reply["content"][0]["text"])

    def test_a_slow_systemd_run_is_reported_as_the_unit_it_left(self) -> None:
        real = self.server.start_argv
        self.server.START_TIMEOUT_SEC = 5
        self.server.start_argv = lambda job, home: ["sh", "-c", '"$@"; exec sleep 30', "sh",
                                                    *real(job, home)]
        reply = self.start("sleep 20")
        self.assertIn(f"started {self.unit}.service", reply)
        self.assertIn("timed out", reply)
        self.assertIn("activating", self.server.tool_status({"slug": self.slug}, self.home))
        self.server.start_argv = lambda job, home: ["sleep", "30"]
        self.reset_unit()
        with self.assertRaisesRegex(RuntimeError, "not started"):
            self.start("true")

    def test_cargo_test_runs_as_a_job(self) -> None:
        (self.repo / "src").mkdir()
        (self.repo / "Cargo.toml").write_text(
            '[package]\nname = "jobs_broker_probe"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\nitoa = "1"\n')
        (self.repo / "src/lib.rs").write_text(
            '#[test]\nfn adds() { assert_eq!(itoa::Buffer::new().format(2), "2"); }\n')
        self.server.tool_start({"repo": str(self.repo), "slug": self.slug, "max_runtime_sec": 600,
                                "profile": "nix",
                                "argv": ["nix", "shell", "nixpkgs#gcc", "--command", "cargo", "test"]},
                               self.home, self.project)
        out = self.server.tool_status({"slug": self.slug, "wait_sec": 540}, self.home)
        out += self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("MARKER_OK", out)
        self.assertIn("test adds ... ok", out)

    def test_uv_run_of_a_script_with_a_dependency_runs_as_a_job(self) -> None:
        (self.repo / "probe.py").write_text(
            '# /// script\n# dependencies = ["iniconfig"]\n# ///\n'
            'import iniconfig\nprint("UV-RAN", iniconfig.__name__)\n')
        self.start("uv run --no-project --python python3 probe.py", runtime=300)
        out = self.server.tool_status({"slug": self.slug, "wait_sec": 280}, self.home)
        out += self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("MARKER_OK", out)
        self.assertIn("UV-RAN iniconfig", out)

    def test_nix_build_fetches_a_new_github_input(self) -> None:
        self.git("init", "-q", cwd=self.repo)
        (self.repo / "flake.lock").write_text(
            '{"nodes": {"root": {}}, "root": "root", "version": 7}\n')
        (self.repo / "flake.nix").write_text(
            '{ inputs.systems.url = "git+https://github.com/nix-systems/x86_64-linux";\n'
            '  outputs = { self, systems }: { packages.x86_64-linux.default = derivation {\n'
            '    name = "jobs-broker-net-test"; system = "x86_64-linux"; builder = "/bin/sh";\n'
            '    args = [ "-c" "echo ${toString (import systems)} > $out" ]; }; }; }\n')
        self.git("add", "flake.nix", "flake.lock", cwd=self.repo)
        self.git("commit", "-q", "-m", "flake", cwd=self.repo)
        self.server.tool_start({"repo": str(self.repo), "slug": self.slug, "max_runtime_sec": 300,
                                "profile": "nix",
                                "argv": ["nix", "build", "--refresh", "--no-write-lock-file",
                                         "--no-link", "--print-out-paths", ".#default"]},
                               self.home, self.project)
        out = self.server.tool_status({"slug": self.slug, "wait_sec": 280}, self.home)
        out += self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("MARKER_OK", out)
        self.assertIn("Added input 'systems'", out)
        self.assertIn("-jobs-broker-net-test", out)

    def test_failure_is_reported_and_recorded(self) -> None:
        self.start("exit 3")
        self.assertIn("result=exit-code status=3", self.status())
        self.assertEqual(self.wait_record()["exit_status"], "3")

    def test_runtime_bound_is_a_recorded_timeout(self) -> None:
        self.start("sleep 300", runtime=10)
        self.assertIn("result=timeout", self.status())
        self.assertEqual(self.wait_record()["result"], "timeout")

    def test_repo_path_with_whitespace_never_binds_home(self) -> None:
        (self.dir / "x").mkdir()
        repo = Path(f"{self.dir}/x {self.home}")
        repo.mkdir(parents=True)
        canary = self.home / f".jobs-broker-test-canary-{uuid.uuid4().hex[:8]}"
        try:
            try:
                self.server.tool_start({"repo": str(repo), "slug": self.slug, "max_runtime_sec": 60,
                                        "argv": ["sh", "-c", 'echo ESCAPED > "$1"', "sh", str(canary)]},
                                       self.home, self.project)
            except RuntimeError as refused:
                self.assertIn("characters", str(refused))
            else:
                self.status()
            self.assertFalse(canary.exists(), "the job wrote into the real home")
        finally:
            canary.unlink(missing_ok=True)

    def test_job_cannot_plant_hooks_or_rewrite_git_config(self) -> None:
        self.git("init", "-q", cwd=self.repo)
        shutil.rmtree(self.repo / ".git/hooks")
        before = self.snapshot(self.repo / ".git/config")
        out = self.run_job(
            "mkdir -p .git/hooks; echo planted > .git/hooks/pre-commit; "
            "git config core.hooksPath /tmp; echo '[core]' >> .git/config; "
            "mv .git .git-old && cp -a .git-old .git && echo planted > .git/hooks/pre-commit; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertFalse((self.repo / ".git/hooks/pre-commit").exists(), out)
        self.assertFalse((self.repo / ".git-old").exists(), out)
        self.assertEqual(self.snapshot(self.repo / ".git/config"), before, out)

    def test_worktree_job_cannot_redirect_or_plant_into_the_main_repo(self) -> None:
        wt = self.main_and_worktree()
        main_git = self.repo / ".git"
        watched = (main_git / "hooks", main_git / "config", main_git / "worktrees/wt/commondir",
                   wt / ".git")
        before = self.snapshot(*watched)
        out = self.run_job(
            f"echo planted > {main_git}/hooks/post-checkout; echo '[core]' >> {main_git}/config; "
            f"echo /tmp > {main_git}/worktrees/wt/commondir; echo 'gitdir: /tmp' > .git; "
            f"git config core.hooksPath /tmp; exit 0", repo=wt)
        self.assertIn("MARKER_OK", out)
        self.assertEqual(self.snapshot(*watched), before, out)

    def test_git_dir_as_repo_is_refused(self) -> None:
        self.git("init", "-q", cwd=self.repo)
        with self.assertRaises(RuntimeError):
            self.start("echo planted > hooks/x", repo=self.repo / ".git")
        self.assertFalse((self.repo / ".git/hooks/x").exists())

    def test_job_cannot_write_submodule_or_worktree_git_state(self) -> None:
        sub_src = self.dir / "subsrc"
        sub_src.mkdir()
        self.git("init", "-q", cwd=sub_src)
        self.git("commit", "-q", "--allow-empty", "-m", "sub", cwd=sub_src)
        wt = self.main_and_worktree()
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub",
                 cwd=self.repo)
        self.git("config", "extensions.worktreeConfig", "true", cwd=self.repo)
        self.git("config", "--worktree", "core.x", "main", cwd=self.repo)
        self.git("config", "--worktree", "core.x", "wt", cwd=wt)
        common = self.repo / ".git"
        admin = common / "worktrees/wt"
        watched = (common / "modules/sub/config", common / "modules/sub/hooks",
                   common / "config.worktree", admin / "config.worktree", self.repo / "sub/.git")
        before = self.snapshot(*watched)
        attack = (f"echo '[core]' >> {common}/modules/sub/config; "
                  f"echo planted > {common}/modules/sub/hooks/post-checkout; "
                  f"echo '[core]' >> {common}/config.worktree; "
                  f"echo '[core]' >> {admin}/config.worktree; "
                  "echo 'gitdir: /tmp' > sub/.git; exit 0")
        for where in (self.repo, wt):
            self.reset_unit()
            with self.subTest(where=where.name):
                out = self.run_job(attack, repo=where)
                self.assertIn("MARKER_OK", out)
                self.assertEqual(self.snapshot(*watched), before, out)

    def test_nested_repo_git_dir_cannot_be_written_or_swapped(self) -> None:
        self.git("init", "-q", cwd=self.repo)
        inner = self.repo / "vendor/inner"
        inner.mkdir(parents=True)
        self.git("init", "-q", cwd=inner)
        before = self.snapshot(inner / ".git/hooks", inner / ".git/config")
        out = self.run_job(
            "echo planted > vendor/inner/.git/hooks/post-checkout; "
            "echo '[core]' >> vendor/inner/.git/config; "
            "mv vendor vendor2 && mkdir -p vendor/inner/.git/hooks && "
            "echo planted > vendor/inner/.git/hooks/post-checkout; exit 0")
        self.assertIn("MARKER_OK", out)
        self.assertFalse((self.repo / "vendor2").exists(), out)
        self.assertEqual(self.snapshot(inner / ".git/hooks", inner / ".git/config"), before, out)

    def test_git_reads_work_and_commit_fails_in_a_repo_and_in_a_worktree(self) -> None:
        wt = self.main_and_worktree()
        for where in (self.repo, wt):
            (where / "f").write_text("a\n")
            self.git("add", "f", cwd=where)
            self.git("commit", "-q", "-m", "base", cwd=where)
        script = ("echo b >> f && git status --short && git diff --stat && "
                  "if git -c user.name=t -c user.email=t@t commit -qam from-job; "
                  "then echo COMMITTED; else echo COMMIT-REFUSED; fi")
        for where in (self.repo, wt):
            self.reset_unit()
            with self.subTest(where=where.name):
                out = self.run_job(script, repo=where)
                self.assertIn("MARKER_OK", out)
                self.assertIn(" M f", out)
                self.assertIn("1 file changed", out)
                self.assertIn("COMMIT-REFUSED", out)
                self.assertEqual(self.git("log", "-1", "--format=%s", cwd=where).strip(), "base")

    def test_nix_build_of_a_flake_in_a_worktree(self) -> None:
        wt = self.main_and_worktree()
        (wt / "flake.nix").write_text(
            '{ outputs = { self }: { packages.x86_64-linux.default = derivation {\n'
            '  name = "jobs-broker-test"; system = "x86_64-linux"; builder = "/bin/sh";\n'
            '  args = [ "-c" "echo ok > $out" ]; }; }; }\n')
        self.git("add", "flake.nix", cwd=wt)
        self.git("commit", "-q", "-m", "flake", cwd=wt)
        self.server.tool_start({"repo": str(wt), "slug": self.slug, "max_runtime_sec": 300,
                                "profile": "nix",
                                "argv": ["nix", "build", "--no-link", "--print-out-paths", ".#default"]},
                               self.home, self.project)
        out = self.server.tool_status({"slug": self.slug, "wait_sec": 240}, self.home)
        out += self.server.tool_log({"slug": self.slug}, self.home)
        self.assertIn("MARKER_OK", out)
        self.assertIn("-jobs-broker-test", out)

    def test_concurrent_starts_of_one_slug_start_one_job_and_keep_its_log(self) -> None:
        import itertools
        import threading
        marker = f"FIRST-{uuid.uuid4().hex[:6]}"
        real_show, order = self.server._show, itertools.count()
        first_checked, second_checked = threading.Event(), threading.Event()

        def show_holding_the_first_start_after_its_check(unit: str) -> dict:
            snap = real_show(unit)
            if next(order) == 0:
                first_checked.set()
                second_checked.wait(3)
            else:
                second_checked.set()
            return snap

        self.server._show = show_holding_the_first_start_after_its_check
        outcomes: list = []

        def start() -> None:
            try:
                outcomes.append(self.start(f"echo {marker}; sleep 30"))
            except Exception as refused:
                outcomes.append(refused)

        threads = [threading.Thread(target=start) for _ in range(2)]
        threads[0].start()
        first_checked.wait(10)
        threads[1].start()
        for t in threads:
            t.join(60)
        self.server._show = real_show
        started = [o for o in outcomes if isinstance(o, str)]
        refused = [str(o) for o in outcomes if not isinstance(o, str)]
        self.assertEqual(len(started), 1, outcomes)
        self.assertEqual(len(refused), 1, outcomes)
        self.assertIn("already exists", refused[0])
        deadline = time.monotonic() + 10
        while marker not in self.server.tool_log({"slug": self.slug}, self.home):
            self.assertLess(time.monotonic(), deadline, "the started job's log was replaced")
            time.sleep(0.2)

    def test_stop_ends_a_running_job_without_a_failure_record_or_debris(self) -> None:
        before = sorted(os.listdir(self.repo))
        self.start("sleep 300")
        time.sleep(3)
        self.server.tool_stop({"slug": self.slug}, self.home)
        self.assertIn("no job", self.server.tool_status({"slug": self.slug}, self.home))
        self.assertFalse(self.record().exists())
        self.assertEqual(sorted(os.listdir(self.repo)), before)


if __name__ == "__main__":
    unittest.main()

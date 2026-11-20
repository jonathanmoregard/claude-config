#!/usr/bin/env python3
"""Contract tests for the jobs broker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
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


class Home(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        for d in ("Repos/main/.git", "worktrees/wt", ".ssh", ".config/git", ".cache/nix"):
            (self.home / d).mkdir(parents=True)
        self.repo = self.home / "Repos" / "main"
        (self.repo / ".git" / "config").write_text("")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def start_args(self, **over) -> dict:
        return {"repo": str(self.repo), "argv": ["make", "check"], "slug": "check", **over}

    def job(self, **over):
        return self.server.validate_start(self.start_args(**over), self.home)


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
            self.server.confine_repo(raw, self.home)

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

    def writable(self, raw: Path) -> set:
        target = self.server.confine_repo(str(raw), self.home)
        self.assertEqual(target.repo, raw)
        return {path for path, _ in target.writable}

    def test_plain_repo_writes_only_inside_itself(self) -> None:
        self.assertEqual({p for p in self.writable(self.repo) if p != self.repo and
                          self.repo not in p.parents}, set())

    def test_linked_worktree_also_writes_main_git_dir(self) -> None:
        wt = self.home / "worktrees" / "wt"
        admin = self.repo / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        main_git = self.repo / ".git"
        self.assertIn(main_git, self.writable(wt))
        self.assertEqual({p for p in self.writable(wt) if p != wt and main_git != p
                          and main_git not in p.parents}, set())

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

    def test_refuses_a_worktree_whose_common_dir_path_is_not_clean(self) -> None:
        main = self.home / "Repos" / "ma in"
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        wt = self.home / "worktrees" / "wt"
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        self.refused(str(wt))


HARDENING = {
    "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateUsers=self",
    "NoNewPrivileges=yes", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
    "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM", "PrivatePIDs=yes",
    "RestrictNamespaces=yes", "PrivateIPC=yes", "PrivateDevices=yes",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes",
    "LockPersonality=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
    "CapabilityBoundingSet=", "RemainAfterExit=yes", "Type=exec",
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

    def test_repo_profile_writes_only_the_repo(self) -> None:
        props = self.props("repo")
        self.assertEqual([p for p in values(props, "BindPaths")
                          if p != str(self.repo) and not p.startswith(f"{self.repo}/")], [])
        self.assertIn(f"-{self.home}/.config/git", values(props, "BindReadOnlyPaths"))
        self.assertEqual(values(props, "WorkingDirectory"), [str(self.repo)])
        self.assertNotIn("Slice=ram-heavy.slice", props)

    def test_nix_profile_adds_store_cache_and_heavy_slice(self) -> None:
        props = self.props("nix")
        self.assertIn(f"{self.home}/.cache/nix", values(props, "BindPaths"))
        self.assertIn(f"-{self.home}/.nix-memory-pressure", values(props, "BindReadOnlyPaths"))
        self.assertIn("Slice=ram-heavy.slice", props)
        self.assertIn("OOMPolicy=kill", props)

    def test_runtime_and_failure_recorder(self) -> None:
        props = self.props("repo", max_runtime_sec=600)
        self.assertEqual(values(props, "RuntimeMaxSec"), ["600"])
        log = self.home / ".local/state/claude-jobs/claude-job-check.log"
        self.assertEqual(values(props, "StandardOutput"), [f"append:{log}"])
        [post] = values(props, "ExecStopPost")
        self.assertTrue(post.startswith("+"), post)
        self.assertTrue(post.endswith(f"record-user-unit-failure claude-job-check {log}"), post)

    def test_every_bound_repo_path_is_identity_checked(self) -> None:
        wt = self.home / "worktrees" / "wt"
        admin = self.repo / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        (self.repo / ".git" / "config").write_text("")
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        job = self.job(repo=str(wt))
        props = self.server.properties(job, self.home)
        bound = {v.lstrip("-") for key in ("BindPaths", "BindReadOnlyPaths") for v in values(props, key)}
        cmd = self.server.start_argv(job, self.home)
        checked = set(cmd[cmd.index("jobs-broker") + 1:cmd.index("--", cmd.index("jobs-broker")):2])
        under_roots = {p for p in bound if p.startswith((str(self.home / "Repos"),
                                                         str(self.home / "worktrees")))}
        self.assertTrue(under_roots)
        self.assertEqual(under_roots, checked)

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


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_wrapper(self, want: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", self.server.WRAPPER, "jobs-broker",
                               str(self.dir), want, "--", *argv],
                              capture_output=True, text=True, timeout=30)

    def test_runs_argv_and_appends_markers(self) -> None:
        ok = self.run_wrapper(ident(self.dir), "echo", "hi")
        self.assertEqual((ok.returncode, ok.stdout), (0, "hi\nMARKER_OK\n"))
        bad = self.run_wrapper(ident(self.dir), "sh", "-c", "exit 3")
        self.assertEqual((bad.returncode, bad.stdout), (3, "MARKER_FAILED rc=3\n"))

    def test_own_exit_143_is_not_a_stop(self) -> None:
        done = self.run_wrapper(ident(self.dir), "sh", "-c", "exit 143")
        self.assertEqual((done.returncode, done.stdout), (1, "MARKER_FAILED rc=143\n"))

    def test_term_reports_a_stop(self) -> None:
        import signal
        proc = subprocess.Popen(["bash", "-c", self.server.WRAPPER, "jobs-broker",
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
            self.server.tool_start(self.start_args(), self.home)
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
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.dir = Path(tempfile.mkdtemp(prefix=".jobs-broker-test-", dir=self.home / "worktrees"))
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.canary = self.dir / "canary"
        self.canary.write_text("CANARY-OUTSIDE-REPO\n")
        self.slug = f"test-{uuid.uuid4().hex[:12]}"
        self.unit = f"claude-job-{self.slug}"

    def tearDown(self) -> None:
        subprocess.run(["systemctl", "--user", "stop", f"{self.unit}.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", f"{self.unit}.service"],
                       capture_output=True)
        for leftover in (self.home / ".local/state/claude-jobs" / f"{self.unit}.log",
                         self.home / ".local/state/claude-unit-failures" / f"{self.unit}.json"):
            leftover.unlink(missing_ok=True)
        shutil.rmtree(self.dir)

    def start(self, script: str, runtime: int = 120, repo: Path | None = None) -> str:
        return self.server.tool_start({"repo": str(repo or self.repo), "argv": ["sh", "-c", script],
                                       "slug": self.slug, "max_runtime_sec": runtime}, self.home)

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
                                       self.home)
                self.status()
            except RuntimeError:
                pass
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

    def test_git_still_commits_in_a_repo_and_in_a_worktree(self) -> None:
        wt = self.main_and_worktree()
        commit = ("echo x > f && git add f && git -c user.name=t -c user.email=t@t "
                  "commit -q -m from-job && git status --short")
        for where in (self.repo, wt):
            with self.subTest(where=where.name):
                out = self.run_job(commit, repo=where)
                self.assertIn("MARKER_OK", out)
                self.assertIn("from-job", self.git("log", "-1", "--format=%s", cwd=where))
                self.server.tool_stop({"slug": self.slug}, self.home)

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

    def test_stop_ends_a_running_job_without_a_failure_record(self) -> None:
        self.start("sleep 300")
        self.server.tool_stop({"slug": self.slug}, self.home)
        self.assertIn("no job", self.server.tool_status({"slug": self.slug}, self.home))
        self.assertFalse(self.record().exists())


if __name__ == "__main__":
    unittest.main()

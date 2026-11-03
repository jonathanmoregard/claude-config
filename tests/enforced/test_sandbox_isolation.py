# The sandbox a run executes under, attacked rather than described.
#
# The honeypot lane hands the agent one credential that expires by itself, and
# keeps the means of minting another -- GitHub Actions' identity-token endpoint
# and its bearer -- out of the agent's environment. That promise is only worth
# something if the environment is the whole story, so these tests play the
# hostile config: they run the real argv the harness builds and try to recover
# what was withheld.
#
# Two ways these tests have already passed without testing anything, both
# found by running them against an unfixed sandbox and asking why they were
# green. They are the reason for the positive control and the ran-marker:
#
#   - a work directory beside the run instead of inside it vanished into the
#     sandbox's private /tmp, so bwrap exited before the attack began;
#   - the canary was injected with os.environ AFTER startup. /proc/PID/environ
#     exposes the environment as it was at exec, so it was never in the file
#     being attacked. Nothing was blocking the read; there was nothing to read.
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess

import pytest

HARNESS = (pathlib.Path(__file__).resolve().parents[2]
           / "runtime/scripts/agent-e2e/agent_e2e.py")

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    # Not run by `checks`, which installs no bubblewrap; it runs wherever the
    # harness itself can run, which is where a change to the sandbox is made.
    reason="bubblewrap is not installed here",
)

PROBE = "SANDBOX_PROBE_SECRET"
SECRET = "canary-f31c7a"
# Printed by every attack script as its last act. Without it an attack that
# never started -- a sandbox that failed to launch, a shell that died early --
# produces empty output, and "the canary is not in this output" passes.
RAN = "attack-ran-marker"


def harness():
    spec = importlib.util.spec_from_file_location("agent_e2e", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def canary_holder():
    """A live process whose INITIAL environment carries the canary.

    Supplied at exec, because that is the only environment /proc/PID/environ
    ever shows.
    """
    held = subprocess.Popen(["sleep", "120"], env={**os.environ, PROBE: SECRET})
    try:
        yield held
    finally:
        held.kill()
        held.wait()


def attack(tmp_path: pathlib.Path, script: str) -> subprocess.CompletedProcess:
    """Run SCRIPT inside the real sandbox, as a hostile hook would."""
    # `work` lives inside the run, as the harness lays it out: the sandbox
    # mounts a private /tmp and binds only the run and the shared log dir back
    # in, so a work directory beside them would simply vanish -- and an attack
    # that cannot start proves nothing.
    run, shared = tmp_path / "run", tmp_path / "shared"
    work = run / "work"
    for d in (run, shared, work):
        d.mkdir()
    argv = harness().sandboxed(
        ["sh", "-c", f"{script}; echo {RAN}"], claude=shutil.which("sh"),
        run=run, shared=shared, work=work)
    # Scrubbed from the child exactly as the harness scrubs its own list --
    # otherwise the child reads the probe out of its own environ and the test
    # reports a breakout that never happened.
    env = {k: v for k, v in os.environ.items() if k != PROBE}
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          env=env)
    assert RAN in done.stdout, f"the attack never ran: {done.stderr[:400]}"
    return done


def test_the_canary_is_readable_without_the_sandbox(canary_holder):
    # The control every other test here depends on. It proves the canary is
    # genuinely in the file the attacks read, so that "not recovered" means
    # the sandbox stopped them rather than that there was nothing there.
    environ = pathlib.Path(f"/proc/{canary_holder.pid}/environ").read_bytes()
    assert SECRET.encode() in environ


def test_the_agent_cannot_read_another_process_environment(tmp_path, canary_holder):
    # The harness holds ACTIONS_ID_TOKEN_REQUEST_TOKEN and _URL and removes
    # them from the child's environment. A run that can read them back out of
    # /proc mints credentials of its own for as long as the job lasts.
    #
    # What refuses this is bubblewrap's user namespace: a process inside one
    # holds no capability over the namespace that owns the target, so
    # ptrace_may_access fails on the read. NOT yama -- measured, after
    # assuming otherwise: kernel.yama.ptrace_scope is 1 on this host and an
    # unsandboxed child still reads its parent's environ, because yama gates
    # PTRACE_MODE_ATTACH and a read is not an attach. The pid namespace
    # removes the entry altogether, so the guarantee rests on two mechanisms
    # rather than one.
    done = attack(tmp_path, f"cat /proc/{canary_holder.pid}/environ 2>&1 || true")
    assert SECRET not in done.stdout, done.stdout[:400]


def test_the_agent_cannot_sweep_every_process_for_it(tmp_path, canary_holder):
    # Reading one known pid is the obvious attack; walking /proc for whichever
    # process happens to hold a token is the patient one.
    done = attack(tmp_path, "cat /proc/[0-9]*/environ 2>/dev/null || true")
    assert SECRET not in done.stdout, done.stdout[:400]


def test_the_host_process_list_is_not_visible(tmp_path):
    # Defence in depth rather than a secret of its own: a run that cannot
    # enumerate the host cannot pick a target to read in the first place.
    # This is the test that fails against the unfixed sandbox, at ~900.
    done = attack(tmp_path, "ls /proc | grep -c '^[0-9]' || true")
    visible = int(done.stdout.split()[0])
    assert visible < 10, f"{visible} host processes visible from the sandbox"

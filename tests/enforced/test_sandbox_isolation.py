# The sandbox a run executes under, attacked rather than described.
#
# The honeypot lane hands the agent one credential that expires by itself, and
# keeps the means of minting another -- GitHub Actions' identity-token endpoint
# and its bearer -- out of the agent's environment. That promise is only worth
# something if the environment is the whole story, so these tests play the
# hostile config: they run the real argv the harness builds and try to recover
# what was withheld.
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


def harness():
    spec = importlib.util.spec_from_file_location("agent_e2e", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        ["sh", "-c", script], claude=shutil.which("sh"),
        run=run, shared=shared, work=work)
    # Scrubbed from the child exactly as the harness scrubs its own list --
    # otherwise the child reads the probe out of its own environ and the test
    # reports a breakout that never happened.
    env = {k: v for k, v in os.environ.items() if k != PROBE}
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          env=env)


def test_the_agent_cannot_read_the_harness_environment_through_proc(tmp_path):
    # The harness holds ACTIONS_ID_TOKEN_REQUEST_TOKEN and _URL and removes
    # them from the child's environment. A child that can read its ancestor's
    # /proc entry gets them back and can mint credentials of its own for as
    # long as the job runs.
    os.environ[PROBE] = SECRET
    try:
        done = attack(tmp_path, f"cat /proc/{os.getpid()}/environ 2>&1 || true")
    finally:
        del os.environ[PROBE]
    assert SECRET not in done.stdout, done.stdout[:400]


def test_the_agent_cannot_sweep_every_process_on_the_host(tmp_path):
    # Reading one known pid is the obvious attack; walking /proc for whichever
    # process happens to hold a token is the patient one.
    os.environ[PROBE] = SECRET
    try:
        done = attack(tmp_path, "cat /proc/[0-9]*/environ 2>/dev/null || true")
    finally:
        del os.environ[PROBE]
    assert SECRET not in done.stdout, done.stdout[:400]


def test_the_host_process_list_is_not_visible(tmp_path):
    # Defence in depth rather than a secret of its own: a run that cannot
    # enumerate the host cannot pick a target to read in the first place.
    done = attack(tmp_path, "ls /proc | grep -c '^[0-9]' || true")
    visible = int(done.stdout.strip() or 0)
    assert visible < 10, f"{visible} host processes visible from the sandbox"

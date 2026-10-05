"""Contract for the three ai-router hooks (build contract §Hooks, 2026-09-14).

Every case runs the hook as a real subprocess over its real stdin contract,
with `AI_ROUTER_STATE` pointed at a temp dir, so a change to the JSON a hook
emits or the file it writes turns these red. The gate is driven by the stub
router in tests/stubs/ai-router-stub.py via `AI_ROUTER_BIN`; the real
`bin/ai-router` is never invoked here.

Run: nix shell nixpkgs#python3Packages.pytest -c pytest -q tests/test_ai_router_hooks.py
(pytest is not installed in the base python3; the nix form is the sanctioned one.)
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zoneinfo

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(REPO_ROOT, "hooks")
TEE = os.path.join(HOOKS, "statusline-quota-tee.sh")
STOP_FAILURE = os.path.join(HOOKS, "ai-router-stop-failure.py")
GATE = os.path.join(HOOKS, "ai-router-agent-gate.py")
STUB = os.path.join(REPO_ROOT, "tests", "stubs", "ai-router-stub.py")
SETTINGS = os.path.join(REPO_ROOT, "settings.json")
BASH = shutil.which("bash")

STATUSLINE_STDIN = {
    "version": "2.1.270",
    "model": {"id": "claude-fable-5-1"},
    "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 1789400000},
        "seven_day": {"used_percentage": 38, "resets_at": 1789805383},
    },
}


def base_env(tmp_path, **extra):
    """Process env for a hook: fresh AI_ROUTER_STATE, gate/stub knobs cleared."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("AI_ROUTER_"):
            del env[key]
    env["AI_ROUTER_STATE"] = str(tmp_path / "state")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def no_temp_leftovers(directory):
    return glob.glob(os.path.join(directory, "*.tmp*")) == []


# --- 1. statusline tee -----------------------------------------------------

@pytest.fixture
def statusline_stub(tmp_path):
    """A fake statusline: echoes stdin, writes a stderr line, exits 3."""
    path = tmp_path / "statusline-stub.sh"
    path.write_text("cat\nprintf 'ERR-LINE\\n' >&2\nexit 3\n", encoding="utf-8")
    return f"{BASH} {path}"


def run_tee(stdin: bytes, env):
    return subprocess.run(
        [BASH, TEE], input=stdin, capture_output=True, timeout=20, env=env
    )


def test_tee_passthrough_is_byte_identical_including_stderr_and_exit(
    tmp_path, statusline_stub
):
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    raw = json.dumps(STATUSLINE_STDIN).encode()  # no trailing newline on purpose
    proc = run_tee(raw, env)
    assert proc.stdout == raw
    assert proc.stderr == b"ERR-LINE\n"
    assert proc.returncode == 3


def test_tee_writes_claude_snapshot_when_rate_limits_present(
    tmp_path, statusline_stub
):
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    before = int(time.time())
    run_tee(json.dumps(STATUSLINE_STDIN).encode(), env)
    quota_dir = tmp_path / "state" / "quota"
    snap = read_json(quota_dir / "claude.json")
    assert snap["provider"] == "claude"
    assert snap["source"] == "statusline"
    assert snap["cli_version"] == "2.1.270"
    assert before <= snap["observed_at"] <= int(time.time())
    assert snap["rate_limits"] == STATUSLINE_STDIN["rate_limits"]
    assert set(snap) == {"provider", "source", "observed_at", "cli_version", "rate_limits"}
    assert no_temp_leftovers(str(quota_dir)), "atomic write must leave no .tmp behind"
    assert stat.S_IMODE(os.stat(quota_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "2.1.270", "model": {"id": "x"}},
        {"version": "2.1.270", "rate_limits": None},
    ],
    ids=["key-absent", "key-null"],
)
def test_tee_leaves_existing_snapshot_untouched_without_rate_limits(
    tmp_path, statusline_stub, payload
):
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    quota_dir = tmp_path / "state" / "quota"
    quota_dir.mkdir(parents=True)
    existing = quota_dir / "claude.json"
    existing.write_text('{"sentinel": true}', encoding="utf-8")
    raw = json.dumps(payload).encode()
    proc = run_tee(raw, env)
    assert proc.stdout == raw and proc.returncode == 3
    assert existing.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert no_temp_leftovers(str(quota_dir))


def test_tee_does_not_create_file_without_rate_limits(tmp_path, statusline_stub):
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    run_tee(b'{"version": "2.1.270"}', env)
    assert not (tmp_path / "state" / "quota" / "claude.json").exists()


def test_tee_survives_garbage_stdin(tmp_path, statusline_stub):
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    raw = b"\x00not json {\xff\n"
    proc = run_tee(raw, env)
    assert proc.stdout == raw and proc.stderr == b"ERR-LINE\n" and proc.returncode == 3
    assert not (tmp_path / "state" / "quota" / "claude.json").exists()


def test_tee_python_fallback_when_jq_is_absent(tmp_path, statusline_stub):
    """Without jq on PATH the python3 -S -E branch writes the same document."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "cat", "mktemp", "mkdir", "mv", "rm", "python3"):
        real = shutil.which(tool)
        assert real, tool
        os.symlink(os.path.realpath(real), bin_dir / tool)
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub, PATH=str(bin_dir))
    raw = json.dumps(STATUSLINE_STDIN).encode()
    proc = run_tee(raw, env)
    assert proc.stdout == raw and proc.returncode == 3
    snap = read_json(tmp_path / "state" / "quota" / "claude.json")
    assert snap["rate_limits"] == STATUSLINE_STDIN["rate_limits"]
    assert snap["cli_version"] == "2.1.270"
    assert no_temp_leftovers(str(tmp_path / "state" / "quota"))


def test_tee_added_latency_is_under_budget(tmp_path, statusline_stub):
    """Contract budget: < 50 ms added on top of the real statusline (min of 5).

    Measured 2026-09-14: ~24 ms overhead with jq (bash + mktemp + cat + jq +
    mkdir + mv). Min-of-5 keeps a single scheduler hiccup from failing it.
    """
    env = base_env(tmp_path, AI_ROUTER_STATUSLINE_CMD=statusline_stub)
    raw = json.dumps(STATUSLINE_STDIN).encode()

    def wall(argv):
        best = float("inf")
        for _ in range(5):
            t0 = time.perf_counter()
            subprocess.run(argv, input=raw, capture_output=True, env=env)
            best = min(best, time.perf_counter() - t0)
        return best

    plain = wall(statusline_stub.split())
    teed = wall([BASH, TEE])
    assert teed - plain < 0.05, f"tee overhead {teed - plain:.3f}s"


# --- 2. StopFailure hook ---------------------------------------------------

def run_stop_failure(payload, env):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, "-S", "-E", STOP_FAILURE],
        input=text, capture_output=True, text=True, timeout=20, env=env,
    )


def stop_payload(details, error="rate_limit"):
    # Shape from CC 2.1.270's StopFailure schema: `error` is the enum the
    # settings matcher tests, `error_details` the human text.
    return {
        "session_id": "s", "cwd": "/work", "hook_event_name": "StopFailure",
        "error": error, "error_details": details,
    }


def rejected_path(tmp_path):
    return tmp_path / "state" / "quota" / "claude.rejected.json"


def test_stop_failure_parses_epoch(tmp_path):
    env = base_env(tmp_path)
    until = int(time.time()) + 7200
    proc = run_stop_failure(stop_payload(f"You've hit your limit; resets at {until}"), env)
    assert (proc.returncode, proc.stdout) == (0, "")
    rec = read_json(rejected_path(tmp_path))
    assert rec["until"] == until
    assert rec["provider"] == "claude" and rec["source"] == "stop-failure"
    assert set(rec) == {"provider", "until", "reason", "observed_at", "source"}
    assert no_temp_leftovers(str(tmp_path / "state" / "quota"))


def test_stop_failure_parses_iso_time(tmp_path):
    env = base_env(tmp_path)
    when = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(hours=2)
    iso = when.isoformat().replace("+00:00", "Z")
    run_stop_failure(stop_payload(f"rate_limit_error: limit resets at {iso}"), env)
    assert read_json(rejected_path(tmp_path))["until"] == int(when.timestamp())


def test_stop_failure_parses_resets_at_clock_phrase(tmp_path):
    env = base_env(tmp_path)
    tz = zoneinfo.ZoneInfo("Europe/Stockholm")
    now = dt.datetime.now(tz)
    hour = (now + dt.timedelta(hours=2)).hour
    expected = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if expected <= now:
        expected += dt.timedelta(days=1)
    h12 = hour % 12 or 12
    ampm = "am" if hour < 12 else "pm"
    msg = f"You've hit your limit · resets at {h12}{ampm} (Europe/Stockholm)"
    run_stop_failure(stop_payload(msg), env)
    assert read_json(rejected_path(tmp_path))["until"] == int(expected.timestamp())


@pytest.mark.parametrize(
    "phrase,seconds",
    [("resets in 2h 15m", 2 * 3600 + 15 * 60), ("limit resets in 45 minutes", 45 * 60),
     ("resets in 1 hour and 5 min", 3600 + 300)],
)
def test_stop_failure_parses_relative_duration(tmp_path, phrase, seconds):
    env = base_env(tmp_path)
    run_stop_failure(stop_payload(f"You've hit your limit · {phrase}"), env)
    rec = read_json(rejected_path(tmp_path))
    assert rec["until"] == rec["observed_at"] + seconds


def test_stop_failure_parses_24h_clock(tmp_path):
    env = base_env(tmp_path)
    now = dt.datetime.now().astimezone()
    target = (now + dt.timedelta(hours=3)).replace(minute=30, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    run_stop_failure(stop_payload(f"until your limit resets at {target.hour:02d}:30"), env)
    assert read_json(rejected_path(tmp_path))["until"] == int(target.timestamp())


def test_stop_failure_defaults_to_one_hour_without_a_time(tmp_path):
    env = base_env(tmp_path)
    before = int(time.time())
    run_stop_failure(stop_payload("You've hit your limit."), env)
    rec = read_json(rejected_path(tmp_path))
    assert before <= rec["observed_at"] <= int(time.time())
    assert rec["until"] == rec["observed_at"] + 3600


def test_stop_failure_reason_is_first_300_chars(tmp_path):
    env = base_env(tmp_path)
    msg = "x" * 500
    run_stop_failure(stop_payload(msg), env)
    assert read_json(rejected_path(tmp_path))["reason"] == "x" * 300


def test_stop_failure_accepts_alternate_field_names(tmp_path):
    env = base_env(tmp_path)
    until = int(time.time()) + 600
    run_stop_failure(
        {"hook_event_name": "StopFailure", "error_type": "rate_limit",
         "message": f"resets at {until}"},
        env,
    )
    assert read_json(rejected_path(tmp_path))["until"] == until


def test_stop_failure_ignores_non_rate_limit_errors(tmp_path):
    env = base_env(tmp_path)
    proc = run_stop_failure(stop_payload("overloaded_error 529", error="overloaded"), env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not rejected_path(tmp_path).exists()


def test_stop_failure_is_silent_and_exit_zero_on_garbage(tmp_path):
    env = base_env(tmp_path)
    proc = run_stop_failure("not json {", env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not rejected_path(tmp_path).exists()


def test_stop_failure_fails_open_when_state_dir_is_unwritable(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir", encoding="utf-8")
    env = base_env(tmp_path, AI_ROUTER_STATE=str(blocked))
    proc = run_stop_failure(stop_payload("You've hit your limit."), env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert proc.stderr.strip() != ""


def test_stop_failure_runs_under_200ms(tmp_path):
    env = base_env(tmp_path)
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        run_stop_failure(stop_payload("You've hit your limit · resets at 3pm (Europe/Stockholm)"), env)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.2, f"stop-failure hook took {best:.3f}s"


# --- 3. Agent gate ---------------------------------------------------------

CLAUDE_NOT_OK = {
    "ok": False,
    "reason": "no eligible model in tier standard for provider claude",
    "candidates": [
        {"model": "claude-sonnet-5", "provider": "claude", "eligible": False,
         "headroom": 8, "why": "headroom 8 < reserve 15"},
    ],
}
ANY_CODEX_TERRA = {
    "ok": True, "provider": "codex", "model": "gpt-5.6-terra", "effort": "high",
    "tier": "standard", "native": False, "host": "claude", "headroom": 40,
    "reason": "rank-first: first eligible in pins order",
    "candidates": [
        {"model": "gpt-5.6-terra", "provider": "codex", "eligible": True,
         "headroom": 40, "why": "eligible"},
        {"model": "claude-sonnet-5", "provider": "claude", "eligible": False,
         "headroom": 8, "why": "headroom 8 < reserve 15"},
    ],
}
NOTHING_OK = {"ok": False, "reason": "nothing eligible", "candidates": []}


def native_pick(model, tier, headroom=62):
    return {
        "ok": True, "provider": "claude", "model": model, "effort": "high",
        "tier": tier, "native": True, "host": "claude", "headroom": headroom,
        "reason": "rank-first", "candidates": [
            {"model": model, "provider": "claude", "eligible": True,
             "headroom": headroom, "why": "eligible"},
        ],
    }


def agent_payload(model=None, cwd="/work/dir", tool_name="Agent"):
    tool_input = {
        "description": "spike",
        "prompt": "List the file names in bin and reply with them",
        "subagent_type": "general-purpose",
    }
    if model is not None:
        tool_input["model"] = model
    return {
        "session_id": "s", "cwd": cwd, "permission_mode": "default",
        "hook_event_name": "PreToolUse", "tool_name": tool_name,
        "tool_input": tool_input, "tool_use_id": "toolu_x",
    }


def run_gate(payload, tmp_path, *, claude=None, any_=None, response=None,
             stub_exit=0, stub_sleep=None, **env_extra):
    """Run the gate against the stub; return (rc, parsed stdout|None, stderr, calls)."""
    log = tmp_path / "stub-calls.jsonl"
    knobs = {"AI_ROUTER_BIN": STUB, "AI_ROUTER_STUB_LOG": str(log), "AI_ROUTER_STUB_EXIT": stub_exit}
    knobs.update(env_extra)  # a test's explicit override (e.g. a missing binary) wins
    env = base_env(tmp_path, **knobs)
    if claude is not None:
        env["AI_ROUTER_STUB_RESPONSE_CLAUDE"] = claude if isinstance(claude, str) else json.dumps(claude)
    if any_ is not None:
        env["AI_ROUTER_STUB_RESPONSE_ANY"] = any_ if isinstance(any_, str) else json.dumps(any_)
    if response is not None:
        env["AI_ROUTER_STUB_RESPONSE"] = response if isinstance(response, str) else json.dumps(response)
    if stub_sleep is not None:
        env["AI_ROUTER_STUB_SLEEP_S"] = str(stub_sleep)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, "-S", "-E", GATE],
        input=text, capture_output=True, text=True, timeout=30, env=env,
    )
    calls = []
    if log.exists():
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else None), proc.stderr, calls


def test_gate_rewrites_model_to_the_native_pick(tmp_path):
    payload = agent_payload(model="opus")
    rc, out, _err, calls = run_gate(payload, tmp_path, claude=native_pick("claude-fable-5-1", "strongest"))
    assert rc == 0
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["permissionDecisionReason"]
    expected = dict(payload["tool_input"], model="claude-fable-5-1")
    assert hso["updatedInput"] == expected, "only `model` may change"
    assert calls == [["pick", "--tier", "strongest", "--host", "claude", "--provider", "claude", "--json"]]


def test_gate_is_silent_when_alias_already_means_the_pick(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model="opus"), tmp_path,
                                    claude=native_pick("claude-opus-5", "strongest"))
    assert (rc, out) == (0, None)
    assert len(calls) == 1


def test_gate_is_silent_when_full_id_equals_the_pick(tmp_path):
    rc, out, _err, _calls = run_gate(agent_payload(model="claude-sonnet-5"), tmp_path,
                                     claude=native_pick("claude-sonnet-5", "standard"))
    assert (rc, out) == (0, None)


def test_gate_denies_with_delegate_command_when_claude_is_below_reserve(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model="sonnet", cwd="/work/dir"), tmp_path,
                                    claude=CLAUDE_NOT_OK, any_=ANY_CODEX_TERRA)
    assert rc == 0
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "updatedInput" not in hso
    reason = hso["permissionDecisionReason"]
    assert "below reserve" in reason
    assert "headroom 8 %" in reason
    assert "ai-router delegate --tier standard --prompt-file" in reason
    assert "--cwd /work/dir" in reason
    assert "codex/gpt-5.6-terra" in reason
    assert "skill ai-router" in reason
    assert calls == [
        ["pick", "--tier", "standard", "--host", "claude", "--provider", "claude", "--json"],
        ["pick", "--tier", "standard", "--host", "claude", "--json"],
    ]


def test_gate_allows_silently_when_nothing_is_eligible_anywhere(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model="haiku"), tmp_path,
                                    claude=NOTHING_OK, any_=NOTHING_OK)
    assert (rc, out) == (0, None)
    assert len(calls) == 2


def test_gate_allows_without_calling_router_when_model_absent(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model=None), tmp_path)
    assert (rc, out) == (0, None)
    assert calls == []


@pytest.mark.parametrize("model", ["inherit", "gpt-5.6-sol", "", "sol"])
def test_gate_allows_unknown_models_without_calling_router(tmp_path, model):
    rc, out, _err, calls = run_gate(agent_payload(model=model), tmp_path)
    assert (rc, out) == (0, None)
    assert calls == []


@pytest.mark.parametrize(
    "model,tier",
    [
        ("opus", "strongest"), ("fable", "strongest"),
        ("claude-opus-5", "strongest"), ("claude-fable-5-1", "strongest"),
        ("sonnet", "standard"), ("claude-sonnet-5", "standard"),
        ("haiku", "fast"), ("claude-haiku-4-5-20251001", "fast"),
    ],
)
def test_gate_maps_requested_model_to_tier(tmp_path, model, tier):
    _rc, _out, _err, calls = run_gate(agent_payload(model=model), tmp_path,
                                      claude=native_pick("claude-x", tier))
    assert calls and calls[0][1:3] == ["--tier", tier]


def test_gate_fails_open_when_router_exits_non_zero(tmp_path):
    rc, out, err, _calls = run_gate(agent_payload(model="opus"), tmp_path,
                                    response=json.dumps({"ok": False, "error_class": "setup", "message": "boom"}),
                                    stub_exit=2)
    assert (rc, out) == (0, None)
    assert len(err.strip().splitlines()) == 1


def test_gate_fails_open_on_garbage_router_output(tmp_path):
    rc, out, err, _calls = run_gate(agent_payload(model="opus"), tmp_path, response="not json {")
    assert (rc, out) == (0, None)
    assert len(err.strip().splitlines()) == 1


def test_gate_fails_open_when_router_hangs_past_timeout(tmp_path):
    t0 = time.perf_counter()
    rc, out, err, _calls = run_gate(agent_payload(model="opus"), tmp_path,
                                    stub_sleep=20, AI_ROUTER_GATE_TIMEOUT_S="0.5")
    elapsed = time.perf_counter() - t0
    assert (rc, out) == (0, None)
    assert elapsed < 5, f"gate took {elapsed:.1f}s; timeout override not honoured"
    assert len(err.strip().splitlines()) == 1


def test_gate_fails_open_when_router_binary_is_missing(tmp_path):
    rc, out, err, _calls = run_gate(agent_payload(model="opus"), tmp_path,
                                    AI_ROUTER_BIN=str(tmp_path / "no-such-router"))
    assert (rc, out) == (0, None)
    assert len(err.strip().splitlines()) == 1


def test_gate_opt_out_env_disables_it(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model="opus"), tmp_path, AI_ROUTER_GATE="off")
    assert (rc, out) == (0, None)
    assert calls == []


def test_gate_is_silent_on_garbage_stdin(tmp_path):
    rc, out, _err, calls = run_gate("not json {", tmp_path)
    assert (rc, out) == (0, None)
    assert calls == []


def test_gate_ignores_other_tools(tmp_path):
    rc, out, _err, calls = run_gate(agent_payload(model="opus", tool_name="Bash"), tmp_path)
    assert (rc, out) == (0, None)
    assert calls == []


def test_gate_runs_silently_on_empty_stdin_under_S_E():
    proc = subprocess.run([sys.executable, "-S", "-E", GATE], input="", capture_output=True, text=True, timeout=20)
    assert (proc.returncode, proc.stdout) == (0, "")


# --- 4. settings.json wiring ----------------------------------------------

def _settings():
    return read_json(SETTINGS)


def test_settings_statusline_routes_through_tee():
    assert _settings()["statusLine"] == {
        "type": "command",
        "command": "bash $HOME/.claude/hooks/statusline-quota-tee.sh",
    }


def test_settings_has_stop_failure_rate_limit_hook():
    entries = _settings()["hooks"]["StopFailure"]
    matching = [e for e in entries if e.get("matcher") == "rate_limit"]
    assert len(matching) == 1
    assert matching[0]["hooks"] == [{
        "type": "command",
        "command": "python3 -S -E $HOME/.claude/hooks/ai-router-stop-failure.py",
        "timeout": 5,
    }]


def test_settings_does_not_rewrite_native_agent_models():
    entries = _settings()["hooks"]["PreToolUse"]
    commands = [
        hook.get("command", "")
        for entry in entries
        for hook in entry.get("hooks", [])
    ]
    assert not any(command.endswith("hooks/ai-router-agent-gate.py") for command in commands)


def test_stub_router_is_executable():
    assert os.access(STUB, os.X_OK), "tests/stubs/ai-router-stub.py needs its exec bit"

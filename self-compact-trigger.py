#!/usr/bin/env python3
"""Stop hook: self-compact threshold trigger.

At a configurable fraction of the DETECTED context window (default 30%, i.e.
~300k on a 1M model) this writes a durable mission handoff and records a
self-compact *request*. `scripts/idle-handoff.sh` consumes the request and does
the reset+resume with its existing guard chain (fork ledger, per-cwd 24h budget,
staleness DENY-when-uncheckable, live-cwd collision abort, ralph collision).

WHY `Stop`
    Stop is the only hook event that recurs WITHOUT a human turn, and autonomy
    is the whole point: a trigger on UserPromptSubmit can only ever nudge,
    because it needs someone to type first. PreCompact is too late (it fires
    only if a compaction happens at all, and the 70% nudge means the user
    usually hands off instead). PostToolUse recurs but fires tens of times per
    turn for no extra signal. So: Stop, once per assistant turn.

HOW IT COMPOSES WITH ralph-loop's Stop HOOK
    ralph-loop registers its own Stop hook, which emits
    {"decision":"block","reason":<prompt>} to keep a loop running. Claude Code
    merges the outputs of all Stop hooks and a `block` wins. This hook therefore
    NEVER writes to stdout, NEVER emits a `decision`, and NEVER exits non-zero
    (exit 2 on Stop is itself a block). It can only add side effects on disk, so
    it cannot race, veto, or short-circuit ralph-loop's decision in either
    direction. When ralph IS looping, the fork is separately refused downstream
    by idle-handoff.sh's ralph-collision guard.

WHAT "RESET" MEANS HERE
    NOT /compact and NOT /clear. Standing decision, on measured grounds ($3-10
    cold cache-write per event against a 95.5% cache-hit rate), and anyway not
    mechanically possible: no tool emits slash commands, hooks inject text only,
    and there is no programmatic in-process context reset. Reset = the NEXT
    process is a fresh one seeded from the handoff. A live session's context is
    never truncated; this hook only makes the handoff exist while the cache is
    still warm, so the resume is cheap whenever it happens.

RUNTIME SWITCH (off by default; expensive fails off)
    ~/.claude/recursive-self-improvement/config/config.json
        {"components": {"self_compact": true},
         "self_compact": {"threshold": 0.30, "max_fires_per_session": 3}}
    Absent file, malformed JSON, missing key, `false`, or any non-boolean value
    (including the string "true") all read as OFF. Enabling is a one-key edit;
    no redeploy, no rebuild.

LOOP BOUND (B6)
    Once per session per threshold crossing, persisted in
    $REFLECT_ROOT/self-compact-state/<sid>.state.json. A reading below the
    threshold re-arms; only an armed session can fire; `max_fires_per_session`
    caps pathological oscillation. State is written BEFORE the model call, and
    if the state file cannot be written the hook does not fire at all — an
    unbounded trigger is worse than a missing one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context_usage  # noqa: E402  (path shim must precede the import)

DEFAULT_THRESHOLD = 0.30
DEFAULT_MAX_FIRES = 3
# Same charset reflect.sh validates: these ids become path components.
SID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Retention for state/request records. ~590 sessions/week would otherwise
# accumulate two files each, forever.
STATE_RETENTION_DAYS = 30
# Hard ceiling on the hand-off to reflect.sh. It detaches (REFLECT_ASYNC=1) and
# returns in ~1s; this only exists so a pathological child cannot eat Claude
# Code's HARD, unconfigurable 60s hook cap.
SPAWN_TIMEOUT_SECONDS = 25


# Truthy set, IDENTICAL to lib.sh:reflect_truthy — `1|true|yes|on`,
# case-insensitive, everything else false. This used to be
# `if os.environ.get("REFLECT_SUBPROCESS")`, i.e. truthy-if-non-empty, while
# reflect.sh:59 tested `== "1"`. So `REFLECT_SUBPROCESS=0` meant "not a
# subprocess" to reflect.sh and "subprocess, do nothing" here: the same lever
# silently DISABLED the trigger in one place and enabled the work in the other.
# One semantic, both sides.
_TRUTHY = {"1", "true", "yes", "on"}


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def claude_dir() -> Path:
    return Path.home() / ".claude"


def reflect_root() -> Path:
    env = os.environ.get("REFLECT_ROOT")
    return Path(env) if env else claude_dir() / "reflections"


def state_dir() -> Path:
    return reflect_root() / "self-compact-state"


def log(msg: str) -> None:
    """Best-effort audit line. Never raises; never touches stdout."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "trigger.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


class Config:
    __slots__ = ("threshold", "max_fires")

    def __init__(self, threshold: float, max_fires: int):
        self.threshold = threshold
        self.max_fires = max_fires


def load_config() -> Config | None:
    """None means OFF. Absent, malformed, or non-boolean-true all mean OFF."""
    path = claude_dir() / "recursive-self-improvement" / "config" / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    components = cfg.get("components")
    if not isinstance(components, dict):
        return None
    # `is not True` on purpose: "true", 1 and "on" are NOT an opt-in to
    # something that spends money.
    if components.get("self_compact") is not True:
        return None

    block = cfg.get("self_compact")
    if not isinstance(block, dict):
        block = {}

    # An ill-typed or out-of-range threshold used to fall back to 0.30 in
    # SILENCE, so the trigger fired at a value the operator did not configure
    # (`"0.5"` as a string, 50 meaning percent, 0, -1). The consumer,
    # scripts/idle-handoff.sh, substituted its own 0.30 by a different route, so
    # producer and consumer could disagree with no log line anywhere.
    #
    # Both ends now REFUSE: a threshold that is present but unusable turns the
    # feature OFF and says why. Absent is still the documented default — that is
    # a choice not to configure it, not a misconfiguration.
    threshold = DEFAULT_THRESHOLD
    if "threshold" in block:
        raw = block["threshold"]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 < raw <= 1:
            threshold = float(raw)
        else:
            log(f"config REFUSED: self_compact.threshold={raw!r} "
                f"({type(raw).__name__}) is not a number in (0, 1]; "
                f"self-compact stays OFF rather than firing at an unconfigured "
                f"{DEFAULT_THRESHOLD}. Fix {path}.")
            return None

    max_fires = DEFAULT_MAX_FIRES
    if "max_fires_per_session" in block:
        raw = block["max_fires_per_session"]
        if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 100:
            max_fires = raw
        else:
            log(f"config REFUSED: self_compact.max_fires_per_session={raw!r} "
                f"({type(raw).__name__}) is not an int in [1, 100]; "
                f"self-compact stays OFF rather than firing with an "
                f"unconfigured loop bound. Fix {path}.")
            return None

    return Config(threshold, max_fires)


def load_state(sid: str) -> dict:
    path = state_dir() / f"{sid}.state.json"
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data.setdefault("armed", True)
            data.setdefault("fires", 0)
            data.setdefault("crossings", 0)
            return data
    except Exception:
        pass
    # No state yet: a fresh session is armed, so the first crossing fires.
    return {"session_id": sid, "armed": True, "fires": 0, "crossings": 0}


def write_json_atomic(path: Path, payload: dict) -> bool:
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


def gc_state(retention_days: int = STATE_RETENTION_DAYS) -> None:
    """Prune old records. Called on fire only — not on every Stop."""
    cutoff = time.time() - retention_days * 86400
    try:
        entries = list(state_dir().iterdir())
    except Exception:
        return
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except Exception:
            pass


def spawn_mission_write(reflect_sh: Path, sid: str, transcript: str, cwd: str) -> str:
    """Hand the handoff write to session-reflect's `mission` mode, detached.

    REFLECT_ASYNC=1  -> the dispatcher detaches its worker (cgroup-immune
                        systemd-run, setsid fallback) and returns in ~1s, so the
                        model call never runs on the hook clock.
    REFLECT_FORCE=1  -> bypass the idempotency guard. A crossing must produce a
                        handoff describing the state AT the crossing; an earlier
                        `complete` mission for this session would otherwise make
                        us resume from something stale.
    """
    payload = json.dumps({
        "session_id": sid,
        "transcript_path": transcript,
        "hook_event_name": "SelfCompact",
        "cwd": cwd,
    })
    env = os.environ.copy()
    env["REFLECT_ASYNC"] = "1"
    env["REFLECT_FORCE"] = "1"
    try:
        proc = subprocess.run(
            ["bash", str(reflect_sh), "mission"],
            input=payload.encode(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=SPAWN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as exc:  # noqa: BLE001 - never let a Stop hook raise
        return f"spawn_error:{type(exc).__name__}"
    if proc.returncode != 0:
        tail = (proc.stdout or b"")[-400:].decode(errors="replace").replace("\n", " ")
        return f"rc={proc.returncode} {tail}"
    return "queued"


def main() -> int:
    # Fork-chain / recursion breaks. A reflection subprocess carries
    # REFLECT_SUBPROCESS=1; every headless run (including our own autofork)
    # reports a non-cli entrypoint. Either one firing this trigger would create
    # a self-feeding chain of mission writes.
    if env_truthy("REFLECT_SUBPROCESS"):
        return 0
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") != "cli":
        return 0

    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    sid = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    cwd = data.get("cwd") or ""
    if not SID_RE.match(sid) or not transcript or not os.path.exists(transcript):
        return 0
    if not cwd or not os.path.isdir(cwd):
        return 0

    cfg = load_config()
    if cfg is None:
        return 0

    occ = context_usage.occupancy(transcript)
    if occ is None:
        return 0
    if occ.estimated:
        # No recorded usage: firing an expensive path off a byte-count guess is
        # not acceptable.
        return 0
    if not occ.window_known:
        # The DENOMINATOR is a guess: this model family is in neither of
        # context_usage.py's prefix tables, so `window` is the 200k base as an
        # assumption. A 1M session measured against it reads ~5x high and would
        # fire at ~6% real occupancy — spending an opus-5 resume on a session
        # with 940k of runway left. Refuse, and say so loudly enough that the
        # table gets updated rather than silently drifting.
        log(f"sid={sid} REFUSING to fire: context window is UNKNOWN for "
            f"model={occ.model!r} (window_source={occ.window_source}, assuming "
            f"{occ.window} would read ~{occ.fill_pct:.2f} fill). Add the family "
            f"to context_usage.py ONE_M_MODEL_PREFIXES or "
            f"KNOWN_BASE_MODEL_PREFIXES.")
        return 0

    state = load_state(sid)

    if occ.fill_pct < cfg.threshold:
        if not state.get("armed"):
            state["armed"] = True
            state["rearmed_at"] = int(time.time())
            write_json_atomic(state_dir() / f"{sid}.state.json", state)
        return 0

    if not state.get("armed"):
        return 0  # already fired for this crossing
    if state.get("fires", 0) >= cfg.max_fires:
        if not state.get("capped"):
            state["capped"] = True
            write_json_atomic(state_dir() / f"{sid}.state.json", state)
            log(f"sid={sid} capped at {cfg.max_fires} fires this session; not firing")
        return 0

    now = int(time.time())
    state["armed"] = False
    state["fires"] = state.get("fires", 0) + 1
    state["crossings"] = state.get("crossings", 0) + 1
    state["last_fired_at"] = now
    state["last_fill_pct"] = occ.fill_pct
    state["window"] = occ.window
    state["threshold"] = cfg.threshold

    # Persist the bound BEFORE spending anything. If this write fails we do not
    # fire: an unbounded trigger is worse than a missing one.
    if not write_json_atomic(state_dir() / f"{sid}.state.json", state):
        log(f"sid={sid} could not persist state under {state_dir()}; refusing to fire")
        return 0

    request = {
        "session_id": sid,
        "cwd": cwd,
        "transcript_path": transcript,
        "tokens": occ.total_tokens,
        "window": occ.window,
        "fill_pct": occ.fill_pct,
        "threshold": cfg.threshold,
        "model": occ.model,
        "crossing": state["crossings"],
        "requested_at_epoch": now,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    write_json_atomic(state_dir() / f"{sid}.request.json", request)

    reflect_sh = claude_dir() / "skills" / "session-reflect" / "reflect.sh"
    if reflect_sh.exists():
        result = spawn_mission_write(reflect_sh, sid, transcript, cwd)
    else:
        result = f"reflect_sh_missing:{reflect_sh}"
    log(f"sid={sid} FIRE crossing={state['crossings']} "
        f"tokens={occ.total_tokens}/{occ.window} "
        f"fill={occ.fill_pct:.3f} threshold={cfg.threshold} cwd={cwd} mission={result}")
    gc_state()
    return 0


if __name__ == "__main__":
    # A Stop hook that exits non-zero BLOCKS the stop and feeds stderr back to
    # the model. Nothing in here is worth doing that for.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)

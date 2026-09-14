"""Regressions from the 2026-09-14 close-out review for the Claude-side hooks
(advice-refine-test-loop, findings F1 and F5), each reproduced before the fix.

  F1  The real router answers "nothing eligible" with {ok: false, message, …}
      AND exit 3. The gate treated every non-zero exit as a router failure
      and allowed silently, so the deny/redirect path could never fire in
      production; the stub had hidden it by exiting 0. The stub now mirrors
      the CLI (exit 3 on ok:false) and the gate reads the answer whatever the
      exit code, failing open only when there is no parseable answer.
  F5  `parse_until` accepted any 10/13-digit run as an epoch: a request id in
      the error text parked Claude until 2029. Reset times further out than a
      week are now ignored in favour of the default TTL.

Run: nix shell nixpkgs#python3Packages.pytest -c pytest -q tests/test_ai_router_hooks_review.py
"""
from __future__ import annotations

import json
import time

from test_ai_router_hooks import (ANY_CODEX_TERRA, agent_payload, base_env, rejected_path, run_gate,
                                  run_stop_failure, stop_payload)

REAL_NOT_OK = {
    "ok": False,
    "error_class": "quota",
    "message": "no eligible standard model for provider claude",
    "candidates": [
        {"eligible": False, "headroom": 8.0, "model": "claude-sonnet-5", "provider": "claude",
         "tier": "standard", "why": "headroom 8 % < reserve 15 %"},
    ],
}


# ── F1 ─────────────────────────────────────────────────────────────────────


def test_gate_denies_on_the_real_exit_3_answer(tmp_path):
    rc, out, err, calls = run_gate(agent_payload(model="sonnet", cwd="/work/dir"), tmp_path,
                                   claude=REAL_NOT_OK, any_=ANY_CODEX_TERRA, stub_exit="")
    assert rc == 0, err
    assert out is not None, f"gate allowed silently; stderr: {err}"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no eligible standard model for provider claude" in hso["permissionDecisionReason"]
    assert "ai-router delegate --tier standard" in hso["permissionDecisionReason"]
    assert len(calls) == 2


def test_gate_reads_exit_4_ok_false_as_an_answer(tmp_path):
    # Both calls answer ok:false with exit 4 (rejected). A router FAILURE would
    # stop after the first call; an ANSWER of "nothing eligible" is followed by
    # the second call and then a silent allow.
    rc, out, _err, calls = run_gate(agent_payload(model="sonnet"), tmp_path,
                                    claude=REAL_NOT_OK, any_=REAL_NOT_OK, stub_exit="4")
    assert (rc, out) == (0, None)
    assert len(calls) == 2


def test_gate_treats_exit_1_ok_true_as_router_failure(tmp_path):
    rc, out, err, calls = run_gate(agent_payload(model="sonnet"), tmp_path, stub_exit="1")
    assert (rc, out) == (0, None)
    assert "router exit 1" in err and len(calls) == 1


def test_gate_fails_open_on_exit_3_without_a_parseable_answer(tmp_path):
    rc, out, err, _calls = run_gate(agent_payload(model="sonnet"), tmp_path,
                                    response="Traceback (most recent call last): boom", stub_exit="3")
    assert (rc, out) == (0, None)
    assert "allowing" in err


def test_gate_fails_open_on_exit_0_non_object(tmp_path):
    rc, out, err, _calls = run_gate(agent_payload(model="sonnet"), tmp_path, response="[1, 2]", stub_exit="0")
    assert (rc, out) == (0, None)
    assert "allowing" in err


# ── F5 ─────────────────────────────────────────────────────────────────────


def _until(tmp_path):
    return json.loads(rejected_path(tmp_path).read_text(encoding="utf-8"))["until"]


def test_stop_failure_ignores_request_id_that_looks_like_an_epoch(tmp_path):
    env = base_env(tmp_path)
    now = int(time.time())
    proc = run_stop_failure(stop_payload("You've hit your limit (request id req_1889375859123x)"), env)
    assert proc.returncode == 0 and proc.stdout == ""
    assert now + 3000 <= _until(tmp_path) <= now + 3700, "must fall back to the default TTL, not 2029"


def test_stop_failure_ignores_far_future_iso_and_relative_times(tmp_path):
    env = base_env(tmp_path)
    now = int(time.time())
    proc = run_stop_failure(stop_payload("You've hit your limit; resets at 2031-01-01T00:00:00Z"), env)
    assert proc.returncode == 0
    assert now + 3000 <= _until(tmp_path) <= now + 3700
    proc = run_stop_failure(stop_payload("You've hit your limit; resets in 400h"), env)
    assert proc.returncode == 0
    assert now + 3000 <= _until(tmp_path) <= now + 3700


def test_stop_failure_still_accepts_a_plausible_epoch(tmp_path):
    env = base_env(tmp_path)
    until = int(time.time()) + 5 * 86400
    proc = run_stop_failure(stop_payload(f"You've hit your limit; resets at {until}"), env)
    assert proc.returncode == 0
    assert _until(tmp_path) == until

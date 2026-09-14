#!/usr/bin/env python3
"""PreToolUse gate on `Agent`: route a pinned subagent model through ai-router.

Implements build contract §Hooks 3 (ai-router, 2026-09-14). Measured
2026-09-14 on Claude Code 2.1.270 (spike, scratchpad/spike/log.jsonl): the
hook receives `{tool_name: "Agent", tool_input: {description, prompt,
subagent_type, model?}, cwd, ...}` and BOTH `permissionDecision: "deny"` and
`updatedInput` are enforced for `Agent`.

Behaviour (enforcing by default; `AI_ROUTER_GATE=off` is the opt-out):

  * `model` absent, empty, or not a Claude family (opus|fable → strongest,
    sonnet → standard, haiku → fast; full ids by family) → allow, no output.
  * `ai-router pick --tier <t> --host claude --provider claude --json`:
      - ok and the pick differs from the request (after alias normalisation:
        `opus` == `claude-opus-5`, a `[...]` suffix is ignored) → allow with
        `updatedInput` = tool_input with only `model` replaced;
      - ok and same model → no output.
  * Claude has nothing eligible in that tier (below reserve / rejected) →
    `ai-router pick --tier <t> --host claude --json`; if that picks a
    non-native provider → deny with the exact `delegate` command to run;
    if nothing is eligible anywhere → allow, no output.
  * Any router failure (missing binary, non-zero exit, unparseable output,
    timeout) → allow, one line on stderr. Fail-open, exit 0 always.

Why the Claude-only pick first: the design (design-provider-router.md §E and
skills/ai-router) reserves the deny for "Claude below reserve and Codex
eligible". A single unconstrained pick would return `gpt-5.6-sol` (pinned
first) whenever Codex has headroom and thereby deny every opus/fable dispatch
even with a full Claude budget; asking for the Claude pick first keeps
Claude→Claude native, as the design requires.

Env: AI_ROUTER_BIN (default ~/.claude/bin/ai-router), AI_ROUTER_STATE
(inherited by the router), AI_ROUTER_GATE=off, AI_ROUTER_GATE_TIMEOUT_S
(per-call timeout, default 8; the two calls share the 14 s hook budget).
`python3 -S -E` compatible (stdlib only).
"""
import json
import os
import re
import subprocess
import sys
import time

FAMILY_TIER = {
    "opus": "strongest",
    "fable": "strongest",
    "sonnet": "standard",
    "haiku": "fast",
}
DEFAULT_TIMEOUT_S = 8.0
HOOK_BUDGET_S = 14.0  # settings.json gives the hook 15 s
_SUFFIX = re.compile(r"\[[^\]]*\]$")


def _log(msg):
    sys.stderr.write("ai-router-agent-gate: %s\n" % msg)


def family_of(model):
    lowered = model.lower()
    for family in FAMILY_TIER:
        if family in lowered:
            return family
    return None


def same_model(requested, picked):
    req = _SUFFIX.sub("", requested.strip().lower())
    pick = picked.strip().lower()
    if req == pick:
        return True
    if req in FAMILY_TIER:  # bare alias names a family; the pick is its canonical id
        return family_of(pick) == req
    return False


def _fmt_headroom(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return "%g" % value


def claude_headroom(result):
    best = None
    for cand in result.get("candidates") or []:
        if not isinstance(cand, dict) or cand.get("provider") != "claude":
            continue
        value = cand.get("headroom")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        best = value if best is None else max(best, value)
    return best


def run_pick(router, args, timeout_s):
    """Return the parsed pick result, or None (with a stderr line) on failure."""
    argv = [router, "pick"] + args + ["--json"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        _log("router timed out after %.1fs; allowing" % timeout_s)
        return None
    except OSError as exc:
        _log("cannot run %s (%s); allowing" % (router, exc))
        return None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        _log("router exit %d (%s); allowing" % (proc.returncode, tail[-1] if tail else "no output"))
        return None
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        _log("router output not JSON; allowing")
        return None
    if not isinstance(result, dict):
        _log("router output not an object; allowing")
        return None
    return result


def decide(payload):
    """Return the hook output object, or None for a silent allow."""
    if payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    requested = tool_input.get("model")
    if not isinstance(requested, str) or not requested.strip():
        return None
    family = family_of(requested)
    if family is None:
        return None
    tier = FAMILY_TIER[family]

    router = os.environ.get("AI_ROUTER_BIN") or os.path.join(
        os.path.expanduser("~"), ".claude", "bin", "ai-router"
    )
    try:
        timeout_s = float(os.environ.get("AI_ROUTER_GATE_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    started = time.monotonic()

    claude = run_pick(router, ["--tier", tier, "--host", "claude", "--provider", "claude"], timeout_s)
    if claude is None:
        return None
    if claude.get("ok"):
        model = claude.get("model")
        if not isinstance(model, str) or not model or claude.get("native") is False:
            return None
        if same_model(requested, model):
            return None
        updated = dict(tool_input)
        updated["model"] = model
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "ai-router: %s -> %s (tier %s, headroom %s %%)"
                % (requested, model, tier, _fmt_headroom(claude.get("headroom"))),
                "updatedInput": updated,
            }
        }

    # Claude has nothing eligible in this tier: is another provider eligible?
    remaining = HOOK_BUDGET_S - (time.monotonic() - started)
    any_pick = run_pick(router, ["--tier", tier, "--host", "claude"], max(0.5, min(timeout_s, remaining)))
    if any_pick is None or not any_pick.get("ok"):
        return None
    if any_pick.get("native") is not False:
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else "<dir>"
    reason = (
        "Claude quota below reserve (headroom %s %%; router: %s). Delegate instead: "
        "`~/.claude/bin/ai-router delegate --tier %s --prompt-file <prompt-file> --cwd %s` "
        "(picks %s/%s); see skill ai-router."
        % (
            _fmt_headroom(claude_headroom(claude)),
            claude.get("reason") or "no eligible Claude model",
            tier,
            cwd,
            any_pick.get("provider"),
            any_pick.get("model"),
        )
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main():
    if os.environ.get("AI_ROUTER_GATE", "").strip().lower() == "off":
        return
    raw = sys.stdin.read()
    if not raw.strip():
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    out = decide(payload)
    if out is not None:
        sys.stdout.write(json.dumps(out))
        sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail-open: the gate must never block on its own bugs
        _log("%s: %s; allowing" % (type(exc).__name__, exc))
    sys.exit(0)

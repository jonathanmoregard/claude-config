#!/usr/bin/env python3
"""
Stop hook: fast-gate the last assistant text, call the Sonnet classifier
synchronously, and — if verdict is NUDGE_CONTINUE — block the stop with
a wrapped nudge that tells Claude to keep going autonomously.

Runs synchronously (unlike step-back-detector). The blocking JSON must be
emitted from THIS process, so we can't detach.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude"))
from importlib import import_module  # noqa: E402

classifier = import_module("continue-nudge-classifier")

# Runtime state only — classifier.log and one cooldown-<session> stamp per
# session. NOT ~/.claude/continue-nudge/: that is a live git checkout holding
# this experiment's tracked corpus (labels.jsonl, backtest-round-*.txt), and
# neither of the shapes written here is covered by an ignore rule, so every
# classified turn would leave the config repo dirty. The corpus keeps its
# place; the runtime does not live in a repo.
STATE_DIR = Path.home() / ".local" / "state" / "continue-nudge"
COOLDOWN_SECONDS = 20
COOLDOWN_TTL_SECONDS = 24 * 60 * 60
LOG = STATE_DIR / "classifier.log"

# Mode: "shadow" (default) = classify + log verdict, never block. Zero-latency
# because classifier runs in a detached subprocess. Safe to wire live.
# "active" = classify synchronously and BLOCK stops with the nudge. This is
# the intended behavior once precision is validated on real production data.
# Override via env: CONTINUE_NUDGE_MODE=active
_RAW_MODE = os.environ.get("CONTINUE_NUDGE_MODE", "shadow")
_MODE_CLEAN = (_RAW_MODE or "").strip().lower()
if _MODE_CLEAN in ("shadow", "active"):
    MODE = _MODE_CLEAN
    _MODE_TYPO = None
else:
    MODE = "shadow"
    _MODE_TYPO = _RAW_MODE  # logged in main() so user notices unrecognized value

# Fast gate — case-insensitive regex list from spec § Fast gate. Match ANY
# to proceed to the LLM judge. Deliberately imprecise (LLM filters FPs).
FAST_GATE_PATTERNS = [
    r"would you like me to",
    r"should i (continue|proceed|go ahead|start|move on|dive in|tackle|do that|try|attempt)",
    r"shall i",
    r"do you want me to",
    r"let me know (if|whether|when|how)",
    r"i'?ll (pause|stop|wait|hold off|hold on|hold up|check in) (here|now|before|first)",
    r"seem(s|ed)? like a good (stopping|pausing|breaking|check.?in) point",
    r"ready (to|when|for) (continue|proceed|you)",
    r"next step(s)? (would be|is|are)",
    r"before (i|we) (continue|proceed|move|go)",
    r"if (you'?d like|that (works|looks good)),? (i|we) can",
    r"want me to (keep going|proceed|continue)",
    r"happy to continue if",
    r"your (call|choice|preference|thoughts)",
    r"once you confirm",
]
FAST_GATE_RE = re.compile("|".join(FAST_GATE_PATTERNS), re.IGNORECASE)

NUDGE_WRAPPER_TEMPLATE = (
    "[continue-nudge] __LLM_NUDGE__\n\n"
    "The continue-nudge classifier judged this a case where you should keep\n"
    "going autonomously. Apply the most reasonable interpretation, document\n"
    "assumptions inline, and continue. Only stop if the next step is truly\n"
    "irreversible, needs credentials, or is genuinely ambiguous in a way\n"
    "that would cause wasted work. Loop protection: Claude Code's\n"
    "`stop_hook_active` flag prevents an immediate re-nudge if you stop\n"
    "again on this same turn; a 20s cooldown per session also throttles\n"
    "re-fires. If you continue and then stop, that stop stands."
)


def wrap_nudge(llm_nudge):
    """Inject LLM-generated nudge into the wrapper without using str.format()
    (LLM output may contain `{` / `}` characters that would crash .format).
    """
    return NUDGE_WRAPPER_TEMPLATE.replace("__LLM_NUDGE__", llm_nudge or "")


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [detector] {msg}\n")
    except OSError:
        pass


def cleanup_cooldowns():
    """Unlink any cooldown-* older than COOLDOWN_TTL_SECONDS."""
    try:
        now = time.time()
        for p in STATE_DIR.glob("cooldown-*"):
            try:
                if now - p.stat().st_mtime > COOLDOWN_TTL_SECONDS:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        return


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if not isinstance(data, dict):
        sys.exit(0)

    session_id = data.get("session_id", "") or ""
    transcript_path = data.get("transcript_path", "") or ""
    stop_hook_active = bool(data.get("stop_hook_active", False))
    cwd = data.get("cwd", "") or ""

    # One-time MODE-typo warning per invocation so a bad env value is noisy
    # (silent-shadow-fallback is a sharp-edges footgun on active-mode wiring).
    if _MODE_TYPO is not None:
        log(f"WARN: CONTINUE_NUDGE_MODE={_MODE_TYPO!r} unrecognized; falling back to shadow")

    # Guard: loop protection — Claude Code sets this true after we block once.
    if stop_hook_active:
        sys.exit(0)

    # Guard: skip homunculus background sessions.
    if "/homunculus/" in cwd:
        sys.exit(0)

    # Guard: skip our OWN classifier subprocesses (they run `claude -p` from
    # STATE_DIR, which triggers Stop hooks on the inner session — recursion
    # risk). Match by resolved path equality, NOT a
    # "/continue-nudge" substring check that would false-positive on any
    # unrelated project directory containing the phrase.
    try:
        if cwd and Path(cwd).resolve() == STATE_DIR.resolve():
            sys.exit(0)
    except OSError:
        pass  # cwd unresolvable — proceed; other guards still apply

    # Guard: need a session id + transcript to do anything.
    if not session_id:
        sys.exit(0)
    if not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    # Path-traversal defense on session_id — Claude Code assigns UUIDs, but
    # a malformed input shouldn't be able to write cooldown-<sid> outside
    # STATE_DIR. Accept only [A-Za-z0-9_-] up to 128 chars.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        log(f"WARN: session_id fails validation, refusing to proceed: {session_id!r}")
        sys.exit(0)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_cooldowns()

    # Cooldown: skip if we nudged this session <COOLDOWN_SECONDS ago.
    cooldown_file = STATE_DIR / f"cooldown-{session_id}"
    if cooldown_file.exists():
        try:
            last = float(cooldown_file.read_text().strip())
            if time.time() - last < COOLDOWN_SECONDS:
                sys.exit(0)
        except (ValueError, OSError):
            pass

    # Extract last assistant text (walk back past tool_use-only turns).
    try:
        last_text = classifier.get_last_assistant_text(transcript_path)
    except Exception as e:
        log(f"{session_id}: get_last_assistant_text crash: {e!r}")
        sys.exit(0)

    if not last_text:
        sys.exit(0)

    # Fast gate — must match at least one deferral pattern to proceed.
    if not FAST_GATE_RE.search(last_text):
        sys.exit(0)

    log(f"{session_id}: gate passed, mode={MODE}")

    # Shadow mode: spawn detached classifier subprocess, log verdict, never
    # block. This lets the user collect real production data with zero
    # latency impact and zero risk of false nudges. Cooldown still applies
    # so we don't burn $ classifying the same session repeatedly.
    if MODE == "shadow":
        # Write cooldown timestamp NOW (before subprocess returns) so a fast
        # follow-up Stop doesn't re-fire the classifier.
        try:
            cooldown_file.write_text(str(time.time()))
        except OSError as e:
            log(f"{session_id}: cooldown write failed: {e!r}")

        classifier_script = Path.home() / ".claude" / "continue-nudge-classifier.py"
        try:
            subprocess.Popen(
                ["python3", str(classifier_script), session_id, transcript_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log(f"{session_id}: shadow classifier spawned")
        except OSError as e:
            log(f"{session_id}: shadow spawn failed: {e!r}")
        sys.exit(0)

    # Active mode: call the classifier synchronously and block on nudge.
    try:
        result = classifier.classify(session_id, transcript_path)
    except Exception as e:
        log(f"{session_id}: classifier crash: {e!r}")
        sys.exit(0)

    if not result:
        log(f"{session_id}: classifier returned None")
        sys.exit(0)

    verdict = result.get("verdict", "")
    nudge = (result.get("nudge") or "").strip()
    reason = (result.get("reason") or "").strip()

    if verdict != "NUDGE_CONTINUE":
        log(f"{session_id}: verdict={verdict} reason={reason[:120]!r} — allowing stop")
        sys.exit(0)

    if not nudge:
        log(f"{session_id}: NUDGE_CONTINUE but empty nudge — allowing stop")
        sys.exit(0)

    # Write cooldown timestamp and emit block.
    try:
        cooldown_file.write_text(str(time.time()))
    except OSError as e:
        log(f"{session_id}: cooldown write failed: {e!r}")

    wrapped = wrap_nudge(nudge)
    log(f"{session_id}: BLOCKING with nudge (len={len(nudge)})")

    try:
        print(json.dumps({"decision": "block", "reason": wrapped}))
    except Exception as e:
        log(f"{session_id}: print block json failed: {e!r}")
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"unhandled crash: {e!r}")
        sys.exit(0)

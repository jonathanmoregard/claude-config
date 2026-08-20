"""The loop-bounds gate in advice-refine-test-loop must behave as written.

The gate is the only thing in that skill that decides whether a review loop
runs another round, halts, or advances a phase — and it is written as a bash
cascade inside SKILL.md. Prose that reads well is the weakest possible evidence
that a cascade routes correctly, so this test EXTRACTS the cascade from the
skill and executes it. Editing the skill therefore re-tests the skill; there is
no second copy of the logic here to drift out of sync.

The churn branch (B1.5) exists because of a real run: on `feat/rag-hybrid-v2`
round 3 found that 3 of its 4 HIGH findings were created by round 2's own
fixes, and the suite was green throughout — so `regressed_r` was 0 and the
cascade said "continue" while the loop ate itself.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from conftest import REPO_ROOT

SKILL = os.path.join(REPO_ROOT, "skills", "advice-refine-test-loop", "SKILL.md")

# The six variables the cascade reads. `selfinflicted_r` / `selfinflicted_prev`
# are strings because "null" (attribution unavailable) is a legal value.
PREAMBLE = """set -uo pipefail
new_r=$1; repro_r=$2; regressed_r=$3
selfinflicted_r=$4; selfinflicted_prev=$5; n=$6
stop=""; continue_round=0
"""

EPILOGUE = """
if [[ -n "$stop" ]]; then echo "$stop"; else echo "continue"; fi
"""

VALID_STOPS = {
    "accounting-error",
    "no-marginal-gain",
    "self-inflicted-churn",
    "converged",
    "unreproducible",
    "ceiling",
    "continue",
}


def extract_cascade() -> str:
    """Pull the gate's bash block out of SKILL.md.

    Anchored on the sentence that introduces it, so a different bash block
    elsewhere in the skill can never be picked up by accident.
    """
    with open(SKILL, "r", encoding="utf-8") as fh:
        text = fh.read()
    anchor = text.index("Gate, evaluated at step 8")
    block = re.search(r"```bash\n(.*?)```", text[anchor:], re.DOTALL)
    assert block, "no bash block follows the gate sentence in SKILL.md"
    body = block.group(1)
    assert "B1.5" in body, "the churn branch is missing from the cascade"
    return body


def make_script() -> str:
    path = os.path.join(tempfile.mkdtemp(), "gate.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PREAMBLE + extract_cascade() + EPILOGUE)
    return path


SCRIPT = make_script()


def gate(new_r, repro_r, regressed_r, selfinflicted_r="null", selfinflicted_prev="null", n=3):
    r = subprocess.run(
        ["bash", SCRIPT, str(new_r), str(repro_r), str(regressed_r),
         str(selfinflicted_r), str(selfinflicted_prev), str(n)],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def test_the_run_that_motivated_the_churn_branch_now_halts():
    # Round 3: 4 HIGHs reproduced, 3 of them created by round 2's fixes,
    # suite green. The old cascade continued here.
    assert gate(4, 4, 0, selfinflicted_r=3, selfinflicted_prev=0) == "self-inflicted-churn"


def test_without_attribution_the_same_round_still_continues():
    # `null` means the attribution check could not run. It must degrade to the
    # old behaviour, never to a silent "0 self-inflicted".
    assert gate(4, 4, 0, selfinflicted_r="null", selfinflicted_prev="null") == "continue"
    assert gate(4, 4, 0, selfinflicted_r="null", selfinflicted_prev=2) == "continue"


def test_one_self_inflicted_finding_is_noise_not_churn():
    assert gate(4, 4, 0, selfinflicted_r=1, selfinflicted_prev="null") == "continue"
    assert gate(4, 4, 0, selfinflicted_r=1, selfinflicted_prev=0) == "continue"
    # The floor of 2 keeps a 1-of-1 round from tripping the majority branch.
    assert gate(1, 1, 0, selfinflicted_r=1, selfinflicted_prev="null") == "continue"


def test_the_same_rate_two_rounds_running_is_churn():
    assert gate(4, 4, 0, selfinflicted_r=1, selfinflicted_prev=1) == "self-inflicted-churn"


def test_majority_branch_boundary():
    assert gate(4, 4, 0, selfinflicted_r=2, selfinflicted_prev=0) == "self-inflicted-churn"
    assert gate(5, 5, 0, selfinflicted_r=2, selfinflicted_prev=0) == "continue"


def test_damage_outranks_churn():
    # B1 sits above B1.5: unrepaired baseline breakage is the louder signal.
    assert gate(4, 2, 3, selfinflicted_r=2, selfinflicted_prev=2) == "no-marginal-gain"


def test_accounting_invariant_covers_the_new_counter():
    assert gate(4, 2, 0, selfinflicted_r=3) == "accounting-error"
    assert gate(4, 4, 0, selfinflicted_r=-1) == "accounting-error"


def test_the_pre_existing_stops_still_route():
    assert gate(0, 0, 0, selfinflicted_r=0, selfinflicted_prev=0) == "converged"
    assert gate(3, 0, 0, selfinflicted_r=0, selfinflicted_prev=0) == "unreproducible"
    assert gate(3, 3, 0, selfinflicted_r=0, selfinflicted_prev=0, n=8) == "ceiling"
    assert gate(4, 2, 3, selfinflicted_r=0, selfinflicted_prev=0) == "no-marginal-gain"


def test_cascade_is_exhaustive():
    """Every tuple classifies as exactly one known stop — nothing falls through.

    The sweep runs inside ONE bash process. Spawning a subprocess per tuple
    would be ~8600 forks and turn a correctness check into a slow test nobody
    runs.
    """
    sweep = f"""
for new_r in 0 1 2 3 4 5; do
 for repro_r in 0 1 2 3 4 5; do
  for regressed_r in 0 1 2; do
   for self in null 0 1 2 3; do
    for prev in null 0 1 2; do
     for n in 1 8; do
       out=$(bash {SCRIPT} "$new_r" "$repro_r" "$regressed_r" "$self" "$prev" "$n")
       case "$out" in
         {'|'.join(sorted(VALID_STOPS))}) ;;
         *) echo "UNCLASSIFIED $new_r $repro_r $regressed_r $self $prev $n -> $out" ;;
       esac
     done
    done
   done
  done
 done
done
"""
    r = subprocess.run(["bash", "-c", sweep], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "", r.stdout

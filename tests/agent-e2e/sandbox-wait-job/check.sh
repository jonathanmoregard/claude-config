#!/usr/bin/env bash
# PASS needs the job to have finished through `wait-for job` itself, on top of
# the wait-job grading: an agent that ran job.sh some other way after a failed
# wait reports the right token too.
set -uo pipefail
run=${1:?run dir}
verdict=$(python3 "$(dirname "$(readlink -f "$0")")/../_lib/wait_grade.py" "$run" main); rc=$?
echo "$verdict"
[ "$rc" = 0 ] || exit "$rc"
slug=$(cat "$run/work/job-slug.txt")
if ! grep -qF "job $slug ran inside the sandbox result=success" "$run/transcript.jsonl"; then
  echo "FAIL: wait-for job did not run the job to completion inside the sandbox"
  exit 1
fi

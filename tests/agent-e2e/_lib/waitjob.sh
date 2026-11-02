#!/usr/bin/env bash
# Builds a ~10 s x WAITJOB_STEPS (default 6) job in $E2E_WORK whose result is unguessable until it finishes:
# the RESULT token is drawn from /dev/urandom at the end and also recorded in
# RUN/job-results.txt, where the grader reads it. Used by the wait-job-* scenarios.
set -euo pipefail
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
STEPS="${WAITJOB_STEPS:-6}"

cat > "$WORK/job.sh" <<EOF
#!/usr/bin/env bash
# Data refresh: $STEPS steps of about ten seconds each, logged to job.log.
set -eu
log=job.log
: > "\$log"
for step in \$(seq 1 $STEPS); do
  echo "step \$step/$STEPS refreshing ..." >> "\$log"
  sleep 10
done
tok=\$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')
echo "\$tok" >> "$RUN/job-results.txt"
echo "RESULT: \$tok" >> "\$log"
echo DONE >> "\$log"
EOF
chmod +x "$WORK/job.sh"
printf '# refresh\n\n`./job.sh` refreshes the data set (about a minute). Progress goes to\n`job.log`, which ends with a `RESULT:` line and then `DONE`.\n' > "$WORK/README.md"

# WAITJOB_START=1: the job is already running when the agent starts, launched
# detached so setup.sh returns — the agent owns no process that could notify it.
# Session startup under load measured 1.5 to 8 minutes, so a job started here
# was often done before the agent's first look. The launcher (outside the
# workspace) therefore holds the job until job.log is first READ (atime catches
# up with mtime; relatime guarantees that update), then execs it. Its command
# line names job.sh, so `pgrep -f job.sh` sees it running throughout.
if [ "${WAITJOB_START:-0}" = 1 ]; then
  cat > "$RUN/launch-on-read.sh" <<'EOF'
#!/usr/bin/env bash
cd "$1"; GATE="$2"; shift 2
echo "starting ..." > job.log
touch -a -d @0 job.log  # only a read from now on moves atime past mtime
python3 - "$GATE" <<'PY'
import os, sys, time
deadline = time.time() + 1200  # start anyway if nothing ever reads the log
how = "deadline"
while time.time() < deadline:
    s = os.stat("job.log")
    if s.st_atime_ns >= s.st_mtime_ns:
        how = "read"
        break
    time.sleep(0.5)
open(sys.argv[1], "w").write(how + "\n")  # the grader's precondition check
PY
exec "$@"
EOF
  setsid nohup bash "$RUN/launch-on-read.sh" "$WORK" "$RUN/gate.txt" ./job.sh > /dev/null 2>&1 < /dev/null &
fi

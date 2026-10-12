#!/bin/bash
# run-job.sh: the runner convention jobwatch reads.
#
#   run-job.sh <name> <logfile> <command> [args...]
#
# Writes a start line, runs the command, and writes the trailer AFTER the
# command exits. Never `exec` the payload: after `exec` there is nobody left to
# write the trailer, and a run that started and died must look different from
# a run that finished.
set -u

name="$1"; log="$2"; shift 2

# On a laptop, launchd often fires calendar jobs inside a 10-45s maintenance
# DarkWake. Hold an idle-sleep assertion for exactly as long as this runner
# lives, so the job is not suspended mid-flight. Re-exec the runner, not the
# payload.
if [ -z "${SCHED_AWAKE:-}" ] && [ -x /usr/bin/caffeinate ]; then
  export SCHED_AWAKE=1
  exec /usr/bin/caffeinate -i "$0" "$name" "$log" "$@"
fi

mkdir -p "$(dirname "$log")"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') $name start ===" >> "$log"
"$@" >> "$log" 2>&1
rc=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S') $name done rc=$rc ===" >> "$log"
exit $rc

#!/bin/bash

# Custom entrypoint for PyFlink standalone-job mode.
# This script starts the JobManager only. It does not submit a job.
# start.sh submits the job (or use the manual steps in OPERATIONS.md).
#
# This script used to submit the job too. That caused two "Clip Detector
# Job" entries on every restart: this script's own submission raced
# start.sh's submission. Now only one script submits the job.
#
# Before this change, this script had its own copy of the -pyFiles
# argument, baked into the image at build time. Adding a new module
# without rebuilding the image made that copy stale. Removing it fixes
# that only for the automatic restart path. OPERATIONS.md still carries
# its own copies of the same -pyFiles argument, for manual recovery. Keep
# those in sync by hand when the argument changes.
#
# Trade-off: if this container crashes, Docker restarts it on its own
# (compose's `restart: unless-stopped`). Before this change, the restarted
# container would resubmit the job by itself. Now it does not. After an
# unexpected restart, submit the job by hand. See OPERATIONS.md, "No
# running jobs" in Flink.

set -e

# Start the JobManager in the foreground. This keeps the container running.
# No `wait` command is needed.
echo "Starting Flink JobManager..."
exec /docker-entrypoint.sh jobmanager

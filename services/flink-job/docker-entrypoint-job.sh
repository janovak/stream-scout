#!/bin/bash

# Custom entrypoint for PyFlink standalone-job mode.
# This script starts the JobManager only. It does not submit a job.
# start.sh submits the job (or use the manual steps in OPERATIONS.md).
#
# This script used to submit the job too. That caused two "Clip Detector
# Job" entries on every restart: this script's own submission raced
# start.sh's submission. Now only one script submits the job. This gives
# the -pyFiles argument one source of truth: start.sh, which runs on the
# host and always matches the checked-out repo. Before this change, this
# script had its own copy of the -pyFiles argument, baked into the image
# at build time. That copy went stale if a new module was added and the
# image was not rebuilt.
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

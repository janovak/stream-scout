#!/bin/bash

# Custom entrypoint for PyFlink standalone-job mode
# Starts the JobManager only. Job submission is start.sh's job (or the
# manual steps in OPERATIONS.md). This entrypoint used to also submit the
# job itself, which produced two "Clip Detector Job" entries on every
# restart: this auto-submission plus start.sh's own submission, racing
# each other. Submitting only from the host-side script also means the
# -pyFiles list has one source of truth, always current with the
# checked-out repo -- not a second copy baked into the image that goes
# stale if the image isn't rebuilt after a new module is added.
#
# Trade-off: if this container crashes and Docker restarts it on its own
# (compose's `restart: unless-stopped`), no job resubmits automatically
# anymore. See OPERATIONS.md, "No running jobs" in Flink.

set -e

# Start the JobManager in the foreground. This keeps the container running;
# no `wait` is needed.
echo "Starting Flink JobManager..."
exec /docker-entrypoint.sh jobmanager

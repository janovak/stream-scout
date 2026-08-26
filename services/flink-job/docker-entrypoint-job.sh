#!/bin/bash

# Custom entrypoint for PyFlink standalone-job mode.
#
# Starts the JobManager. Waits for its REST API to answer. Then submits
# the Clip Detector job. This runs on every container start. That
# includes a normal start.sh restart. It also includes an unattended
# restart from Docker's own `restart: unless-stopped` policy after a
# crash. Both cases produce a brand-new JobManager process with no prior
# job state. Checkpointing is disabled (see flink-conf.yaml) and no HA
# store is configured, so there is never a pre-existing job here to
# collide with. This script is the only thing that ever submits the job.
# start.sh does not. There is no race between two submitters to guard
# against.
#
# -pyFiles comes from the FLINK_PYFILES environment variable, set in
# docker-compose.yml. It is not hardcoded here. Editing docker-compose.yml
# and running "docker compose up -d" picks up a change immediately. No
# image rebuild is needed for that. A copy hardcoded in this script would
# need a rebuild every time a module is added.

set -e

echo "Starting Flink JobManager..."
/docker-entrypoint.sh jobmanager &

echo "Waiting for JobManager to be ready..."
until curl -sf http://localhost:8081/overview > /dev/null 2>&1; do
    echo "  JobManager not ready yet, waiting..."
    sleep 2
done
echo "JobManager is ready!"

echo "Submitting Clip Detector Job..."
# A submission failure here must not bring this script -- and so the
# container -- down. Under `restart: unless-stopped`, an exit here would
# crash-loop the container, retrying the same broken submission forever.
# Log it and keep the JobManager running instead, so an operator can
# inspect and fix it by hand.
if ! flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d; then
    echo "ERROR: job submission failed. The JobManager is still running." >&2
    echo "Check the error above. Fix it. Then submit by hand:" >&2
    echo '  docker exec streamscout-flink-jobmanager sh -c '"'"'flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d'"'"'' >&2
fi

# Keep the container running by waiting on the JobManager process.
wait

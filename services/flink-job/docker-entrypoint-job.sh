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

if [ -z "$FLINK_PYFILES" ]; then
    echo "ERROR: FLINK_PYFILES is not set. Not attempting to submit." >&2
    echo "Set it in docker-compose.yml, then restart this container." >&2
else
    echo "Submitting Clip Detector Job..."
    # A few attempts, a wait apart, before giving up. A submission can
    # fail only because something it depends on is not quite ready yet,
    # even though its own health check already passed. Kafka is one
    # example: a leader election can still be settling. Retrying gives
    # that kind of failure a chance to clear on its own. 5 attempts, 10
    # seconds apart, give this about a minute of margin.
    #
    # A submission failure must not bring this script down, and so must
    # not bring the container down either. Under `restart: unless-stopped`,
    # an unguarded exit here would crash-loop the container forever on a
    # submission that keeps failing for a real reason, such as bad code or
    # a bad module path, not just a transient one. Retry a bounded number
    # of times, then log it and keep the JobManager running. That lets an
    # operator inspect and fix it by hand.
    #
    # Before each retry, check whether a job already exists. `flink run`
    # can report failure on the client side after the JobManager already
    # accepted the job -- the client process killed partway through, for
    # example. A newly-accepted job is not necessarily RUNNING yet; it can
    # sit briefly in CREATED or SCHEDULED first. Checking for RUNNING only
    # would miss it in that window and retry anyway. `flink list`, with no
    # flags, only ever lists non-terminal jobs -- CREATED, RUNNING,
    # RESTARTING, and so on. A failed or cancelled job never appears
    # there. So the job's name showing up in this output at all, in any
    # state, is enough: retrying blindly past that point would submit a
    # second, duplicate job, which is the exact bug this script exists to
    # prevent.
    #
    # If the `flink list` check itself fails, do not treat that the same
    # as "no job found." A job could exist and the check could simply be
    # unable to confirm it right now. Guessing wrong in that direction
    # risks the exact duplicate this whole guard exists to prevent, so
    # stop and ask for a human instead of retrying blind.
    SUBMIT_OK=0
    for attempt in 1 2 3 4 5; do
        if flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d; then
            SUBMIT_OK=1
            break
        fi
        if CHECK_OUTPUT=$(flink list 2>&1); then
            if echo "$CHECK_OUTPUT" | grep -q "Clip Detector Job"; then
                echo "  Submission reported failure, but a Clip Detector Job already exists. Not retrying." >&2
                SUBMIT_OK=1
                break
            fi
        else
            echo "  Submission reported failure, and 'flink list' itself failed: $CHECK_OUTPUT" >&2
            echo "  Not retrying -- cannot confirm a job doesn't already exist. Check by hand." >&2
            break
        fi
        if [ "$attempt" -lt 5 ]; then
            echo "  Submission attempt $attempt failed. Retrying in 10 seconds..." >&2
            sleep 10
        fi
    done
    if [ "$SUBMIT_OK" -ne 1 ]; then
        echo "ERROR: job submission did not succeed. The JobManager is still running." >&2
        echo "Check the error above. Fix it. Then submit by hand:" >&2
        echo '  docker exec streamscout-flink-jobmanager sh -c '"'"'flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles "$FLINK_PYFILES" -d'"'"'' >&2
    fi
fi

# Keep the container running by waiting on the JobManager process.
wait

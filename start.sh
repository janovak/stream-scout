#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

echo "[1/5] Building the Flink images..."
# docker-entrypoint-job.sh is baked into the flink-jobmanager image at
# build time. It is not bind-mounted. A plain "up -d" would reuse the old
# image. It would skip any change to that file. This command builds only
# the two Flink services. They share one Dockerfile. It leaves the other
# services' images alone.
#
# This step runs before anything is stopped. Example: the build fails
# because of a network problem downloading a JAR. The running stack stays
# untouched. Nothing gets stopped with the rebuild incomplete.
docker compose build flink-jobmanager flink-taskmanager

echo ""
echo "[2/5] Stopping existing containers..."
docker compose down

echo ""
echo "[3/5] Starting all services..."
# --wait blocks until every service with a health check reports healthy.
# flink-jobmanager is one of them (see its health check in
# docker-compose.yml). Kafka, Postgres, and Redis already use this same
# mechanism in this file. Submission below then only runs once the
# JobManager is actually answering, not after a fixed guess at how long
# that takes.
#
# flink-jobmanager also waits before its own container even starts. It
# waits on kafka's own health check. It also waits on kafka-init
# finishing (see depends_on in docker-compose.yml). kafka-init creates
# the chat-messages and stream-lifecycle topics. Compose fails as soon as
# any one watched container reports unhealthy. It does not wait for the
# full timeout below in that case. kafka's health check gives up after
# about 150 seconds.
# flink-jobmanager's gives up after about 210 seconds. flink-jobmanager's
# own clock does not start until kafka is healthy and kafka-init has
# finished. The 400-second value below is an outer limit only. It covers
# all of that together, with margin. It is there in case a container
# stays in "starting" longer than its own check would suggest.
docker compose up -d --wait --wait-timeout 400

echo ""
echo "[4/5] Submitting Flink job..."
# -pyFiles ships spike_detector.py, token_manager.py, and clip_attempt.py to
# the SDK harness workers. Workers do not inherit sys.path from
# clip_detector_job.py's own directory. Without -pyFiles, the job fails at
# runtime with "ModuleNotFoundError: No module named 'spike_detector'" (or
# 'token_manager', or 'clip_attempt'). This is the only place that submits
# the job. The jobmanager container's own entrypoint does not submit a job.
# "|| true" here matters. Without it, a submission failure would make
# this assignment itself fail. Under "set -e" that kills the script
# right here, before the echo below ever runs, and the operator would
# see no explanation at all. Letting it continue means the diagnostic
# output below still prints, and the empty-JOB_ID branch further down
# still catches the failure and sets a non-zero exit code.
SUBMIT_OUTPUT=$(docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d 2>&1 || true)
echo "$SUBMIT_OUTPUT"
JOB_ID=$(echo "$SUBMIT_OUTPUT" | grep -oP 'JobID \K[0-9a-f]+' | head -1 || true)

echo ""
echo "[5/5] Waiting for the job to reach RUNNING..."
# A fixed sleep here would be a guess, the same problem step 3 already
# fixed. Poll the REST API instead of "flink list". The REST API reports
# the job's exact state. A job that fails right after submission is
# caught right away, instead of the poll running out its full budget
# first. flink-taskmanager has no health check of its own. A cold
# rebuild can leave it still registering task slots after
# flink-jobmanager already reports healthy. The bound below allows for
# that: up to a minute.
# EXIT_CODE stays 0 only if the job is confirmed RUNNING. The diagnostic
# banner below still prints either way, so a human sees what is up and
# what is not. The exit code is what an automated caller checks. It must
# reflect a real failure here, not just the container-level success from
# the steps above.
EXIT_CODE=0
if [ -z "$JOB_ID" ]; then
    echo "WARNING: could not read a JobID from the submission output above. Skipping the RUNNING check. Run 'flink list' by hand." >&2
    EXIT_CODE=1
else
    JOB_RUNNING=0
    JOB_TERMINAL_FAILURE=0
    for attempt in $(seq 1 30); do
        # python3 reads the "state" field the same way OPERATIONS.md's own
        # examples already parse JSON elsewhere in this project. A field
        # lookup survives a harmless change to key order or whitespace in
        # the response; a regex against the raw text would not.
        JOB_STATE=$(curl -s "http://localhost:8081/jobs/$JOB_ID" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("state", ""))
except ValueError:
    pass' 2>/dev/null || true)
        if [ "$JOB_STATE" = "RUNNING" ]; then
            JOB_RUNNING=1
            break
        fi
        case "$JOB_STATE" in
            FAILED|FAILING|CANCELED|FINISHED)
                # No restart strategy is configured for this job (and none
                # applies with checkpointing off), so FAILING always settles
                # into FAILED. Catching it here saves the remaining poll
                # budget instead of waiting for that settle.
                echo "ERROR: job $JOB_ID entered state $JOB_STATE instead of RUNNING." >&2
                echo "Check: docker logs streamscout-flink-taskmanager" >&2
                JOB_TERMINAL_FAILURE=1
                break
                ;;
        esac
        sleep 2
    done
    if [ "$JOB_RUNNING" -ne 1 ]; then
        if [ "$JOB_TERMINAL_FAILURE" -ne 1 ]; then
            echo "WARNING: job did not reach RUNNING within about a minute. Check 'flink list' by hand." >&2
        fi
        EXIT_CODE=1
    fi
fi

echo ""
echo "=== Startup Complete ==="
echo ""
echo "Services running:"
# "|| true" on these two: they are diagnostic output only, not part of
# the pass/fail decision. Without it, a broken JobManager could fail one
# of them and exit the script here, under set -e, before reaching "exit
# $EXIT_CODE" below -- silently dropping the exit code this step was
# added to get right.
docker compose ps --format "table {{.Name}}\t{{.Status}}" || true
echo ""
echo "Flink job status:"
docker exec streamscout-flink-jobmanager flink list || true
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""
exit $EXIT_CODE

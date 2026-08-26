#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

echo "[1/3] Stopping existing containers..."
docker compose down

echo ""
echo "[2/3] Starting all services..."
# --wait blocks until every service with a health check reports healthy,
# flink-jobmanager included (see its health check in docker-compose.yml).
# This replaces a fixed sleep with a real readiness check, the same
# mechanism Kafka, Postgres, and Redis already use in this file.
#
# The flink-jobmanager container submits the Flink job itself, once its
# own JobManager is ready (see docker-entrypoint-job.sh). This script
# does not submit it.
#
# Guarded with "|| true", like every other fallible step below. Without
# it, a timeout here (under set -e) would exit the script immediately --
# skipping the diagnostics below entirely, the one case where they would
# matter most. "docker compose ps" is still informative even when some
# container never went healthy: it shows which one, unlike Compose's own
# terse timeout message.
CONTAINERS_UP=1
if ! docker compose up -d --wait --wait-timeout 400; then
    CONTAINERS_UP=0
    echo "" >&2
    echo "ERROR: not every container reported healthy within 400 seconds." >&2
fi

JOB_RUNNING=0
if [ "$CONTAINERS_UP" -eq 1 ]; then
    echo ""
    echo "[3/3] Waiting for the Flink job to register..."
    # flink-jobmanager's health check only proves its REST API answers.
    # It does not prove the job itself reached RUNNING: submission
    # happens after that check already passes, inside
    # docker-entrypoint-job.sh, which can itself retry up to 3 times a
    # few seconds apart. Poll instead of guessing a fixed wait, bounded
    # at 60 seconds.
    #
    # The REST API (queried directly, port 8081 is published to the
    # host) is used for this check, not `flink list`'s text output. The
    # text format is meant for a human, not a stable interface, and a
    # Flink version bump could reformat it without warning.
    for attempt in $(seq 1 12); do
        if ! OVERVIEW=$(curl -sf http://localhost:8081/jobs/overview 2>&1); then
            echo "  Could not reach the Flink REST API: $OVERVIEW" >&2
        else
            JOB_STATE=$(echo "$OVERVIEW" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    for job in data.get("jobs", []):
        if job.get("name") == "Clip Detector Job" and job.get("state") == "RUNNING":
            print("RUNNING")
            break
except ValueError:
    pass
' 2>/dev/null || true)
            if [ "$JOB_STATE" = "RUNNING" ]; then
                JOB_RUNNING=1
                break
            fi
        fi
        if [ "$attempt" -lt 12 ]; then
            sleep 5
        fi
    done
fi

echo ""
if [ "$JOB_RUNNING" -eq 1 ]; then
    EXIT_CODE=0
    echo "=== Startup Complete ==="
elif [ "$CONTAINERS_UP" -eq 0 ]; then
    EXIT_CODE=1
    echo "=== Startup Failed: containers did not all become healthy ==="
    echo ""
    echo "WARNING: see the ERROR above. Check 'docker compose ps' below" >&2
    echo "for which container, then 'docker logs <name>' for why." >&2
else
    EXIT_CODE=1
    echo "=== Startup Finished, BUT THE FLINK JOB IS NOT RUNNING ==="
    echo ""
    echo "WARNING: the Flink job did not reach RUNNING within 60 seconds." >&2
    echo "Check 'docker logs streamscout-flink-jobmanager' for an ERROR" >&2
    echo "line -- see OPERATIONS.md Part 1." >&2
fi
echo ""
echo "Services running:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" || true
echo ""
echo "Flink job status:"
if ! FLINK_LIST=$(docker exec streamscout-flink-jobmanager flink list 2>&1); then
    echo "Could not check: $FLINK_LIST" >&2
else
    echo "$FLINK_LIST"
fi
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""
exit $EXIT_CODE

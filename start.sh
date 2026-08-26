#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

echo "[1/2] Stopping existing containers..."
docker compose down

echo ""
echo "[2/2] Starting all services..."
# --wait blocks until every service with a health check reports healthy,
# flink-jobmanager included (see its health check in docker-compose.yml).
#
# flink-jobmanager runs the Clip Detector job itself, as part of its own
# startup (Application Mode -- see docker-entrypoint-job.sh). There is no
# separate submission step for this script to wait on or perform. If the
# job fails to start, the whole flink-jobmanager process exits, so it
# never reports healthy in the first place, and this command fails here
# rather than reporting a false success.
CONTAINERS_UP=1
if ! docker compose up -d --wait --wait-timeout 500; then
    CONTAINERS_UP=0
    echo "" >&2
    echo "ERROR: not every container reported healthy within 500 seconds." >&2
fi

echo ""
if [ "$CONTAINERS_UP" -eq 1 ]; then
    echo "=== Startup Complete ==="
else
    echo "=== Startup Failed: containers did not all become healthy ==="
    echo ""
    echo "WARNING: see the ERROR above. Check 'docker compose ps' below" >&2
    echo "for which container, then 'docker logs <name>' for why." >&2
fi
echo ""
echo "Services running:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" || true
echo ""
echo "Flink job status:"
if ! FLINK_LIST=$(docker exec streamscout-flink-jobmanager flink list 2>&1); then
    echo "Could not check: $FLINK_LIST" >&2
    CONTAINERS_UP=0
else
    echo "$FLINK_LIST"
fi
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""
if [ "$CONTAINERS_UP" -eq 1 ]; then
    exit 0
else
    exit 1
fi

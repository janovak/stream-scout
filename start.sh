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
# That health check itself confirms the Clip Detector job is registered,
# not just that flink-jobmanager's REST API answers -- those are not the
# same moment in Flink Application Mode. See the health check in
# docker-compose.yml for why that distinction matters.
#
# flink-jobmanager runs the job itself, as part of its own startup. There
# is no separate submission step for this script to wait on or perform.
CONTAINERS_UP=1
if ! docker compose up -d --wait --wait-timeout 500; then
    CONTAINERS_UP=0
    echo "" >&2
    echo "ERROR: not every container reported healthy within 500 seconds." >&2
fi

echo ""
echo "Flink job status:"
if ! FLINK_LIST=$(docker exec streamscout-flink-jobmanager flink list 2>&1); then
    echo "Could not check: $FLINK_LIST" >&2
    CONTAINERS_UP=0
else
    echo "$FLINK_LIST"
fi

# The banner below reflects CONTAINERS_UP's final value -- after both
# checks above, not just the first one. Printing it any earlier risked
# showing "Startup Complete" and then having a later check silently
# invalidate it, leaving a misleading banner on screen even though the
# script still exited 1.
echo ""
if [ "$CONTAINERS_UP" -eq 1 ]; then
    echo "=== Startup Complete ==="
else
    echo "=== Startup Failed ==="
    echo ""
    echo "WARNING: see the ERROR above. Check 'docker compose ps' below" >&2
    echo "for which container, then 'docker logs <name>' for why." >&2
fi
echo ""
echo "Services running:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" || true
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

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
docker compose up -d --wait --wait-timeout 400

echo ""
echo "[3/3] Waiting 20 seconds for the job to register..."
sleep 20

echo ""
echo "=== Startup Complete ==="
echo ""
echo "Services running:"
docker compose ps --format "table {{.Name}}\t{{.Status}}" || true
echo ""
echo "Flink job status:"
# flink-jobmanager's health check only proves its REST API answers. It
# does not prove the job itself reached RUNNING. Check that here, so a
# submission failure is not reported as a successful restart.
FLINK_LIST=$(docker exec streamscout-flink-jobmanager flink list 2>&1 || true)
echo "$FLINK_LIST"
if echo "$FLINK_LIST" | grep -q "Clip Detector Job.*RUNNING"; then
    EXIT_CODE=0
else
    echo "" >&2
    echo "WARNING: the Flink job is not RUNNING. Check 'docker logs" >&2
    echo "streamscout-flink-jobmanager' for an ERROR line, or wait longer" >&2
    echo "and check 'flink list' again -- see OPERATIONS.md Part 1." >&2
    EXIT_CODE=1
fi
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""
exit $EXIT_CODE

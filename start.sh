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
echo "[3/3] Waiting 15 seconds for the job to register..."
sleep 15

echo ""
echo "=== Startup Complete ==="
echo ""
echo "Services running:"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo ""
echo "Flink job status:"
docker exec streamscout-flink-jobmanager flink list
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""

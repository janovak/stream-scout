#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

echo "[1/5] Building the Flink images..."
# docker-entrypoint-job.sh is baked into the flink-jobmanager image at
# build time, not bind-mounted. A plain "up -d" would reuse the old image
# and skip any change to that file. Build only the two Flink services --
# they share one Dockerfile -- and leave the other services' images
# alone. This step runs before anything is stopped. If the build fails
# (for example, a network problem downloading a JAR), the running stack
# is left untouched instead of stopped with nothing rebuilt.
docker compose build flink-jobmanager flink-taskmanager

echo ""
echo "[2/5] Stopping existing containers..."
docker compose down

echo ""
echo "[3/5] Starting all services..."
# --wait blocks until every service with a health check reports healthy,
# flink-jobmanager included (see its health check in docker-compose.yml).
# This is the same mechanism Kafka, Postgres, and Redis already use in
# this file, so submission below only runs once the JobManager is
# actually answering, not after a fixed guess at how long that takes.
docker compose up -d --wait --wait-timeout 200

echo ""
echo "[4/5] Submitting Flink job..."
# -pyFiles ships spike_detector.py, token_manager.py, and clip_attempt.py to
# the SDK harness workers. Workers do not inherit sys.path from
# clip_detector_job.py's own directory. Without -pyFiles, the job fails at
# runtime with "ModuleNotFoundError: No module named 'spike_detector'" (or
# 'token_manager', or 'clip_attempt'). This is the only place that submits
# the job. The jobmanager container's own entrypoint does not submit a job.
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d

echo ""
echo "[5/5] Waiting 15 seconds for job to start..."
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

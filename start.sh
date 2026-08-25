#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

# Stop any existing containers
echo "[1/7] Stopping existing containers..."
docker compose down

echo ""
echo "[2/7] Building the Flink images..."
# docker-entrypoint-job.sh is baked into the flink-jobmanager image at
# build time, not bind-mounted. A plain "up -d" would reuse the old image
# and skip any change to that file. Build only the two Flink services --
# they share one Dockerfile -- and leave the other services' images alone.
docker compose build flink-jobmanager flink-taskmanager

echo ""
echo "[3/7] Starting all services..."
docker compose up -d

echo ""
echo "[4/7] Waiting 60 seconds for services to initialize..."
sleep 60

echo ""
echo "[5/7] Waiting for the Flink JobManager to be ready..."
JOBMANAGER_READY=0
for attempt in $(seq 1 90); do
    if curl -s http://localhost:8081/overview > /dev/null 2>&1; then
        JOBMANAGER_READY=1
        break
    fi
    echo "  JobManager not ready yet, waiting..."
    sleep 2
done
if [ "$JOBMANAGER_READY" -ne 1 ]; then
    echo "ERROR: JobManager did not become ready within 180 seconds." >&2
    exit 1
fi

echo ""
echo "[6/7] Submitting Flink job..."
# -pyFiles ships spike_detector.py, token_manager.py, and clip_attempt.py to
# the SDK harness workers. Workers do not inherit sys.path from
# clip_detector_job.py's own directory. Without -pyFiles, the job fails at
# runtime with "ModuleNotFoundError: No module named 'spike_detector'" (or
# 'token_manager', or 'clip_attempt'). This is the only place that submits
# the job. The jobmanager container's own entrypoint does not submit a job.
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d

echo ""
echo "[7/7] Waiting 15 seconds for job to start..."
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

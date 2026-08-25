#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

# Stop any existing containers
echo "[1/6] Stopping existing containers..."
docker compose down

echo ""
echo "[2/6] Building images and starting all services..."
# --build is required. docker-entrypoint-job.sh is baked into the
# flink-jobmanager image at build time, not bind-mounted. Without --build,
# "up -d" reuses the old image and the fix in that file does not take
# effect.
docker compose up -d --build

echo ""
echo "[3/6] Waiting 60 seconds for services to initialize..."
sleep 60

echo ""
echo "[4/6] Waiting for the Flink JobManager to be ready..."
JOBMANAGER_READY=0
for attempt in $(seq 1 30); do
    if curl -s http://localhost:8081/overview > /dev/null 2>&1; then
        JOBMANAGER_READY=1
        break
    fi
    echo "  JobManager not ready yet, waiting..."
    sleep 2
done
if [ "$JOBMANAGER_READY" -ne 1 ]; then
    echo "ERROR: JobManager did not become ready within 60 seconds." >&2
    exit 1
fi

echo ""
echo "[5/6] Submitting Flink job..."
# -pyFiles ships spike_detector.py, token_manager.py, and clip_attempt.py to
# the SDK harness workers. Workers do not inherit sys.path from
# clip_detector_job.py's own directory. Without -pyFiles, the job fails at
# runtime with "ModuleNotFoundError: No module named 'spike_detector'" (or
# 'token_manager', or 'clip_attempt'). This is the only place that submits
# the job. The jobmanager container's own entrypoint does not submit a job.
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -pyFiles /opt/flink/usrlib/spike_detector.py,/opt/flink/usrlib/token_manager.py,/opt/flink/usrlib/clip_attempt.py -d

echo ""
echo "[6/6] Waiting 15 seconds for job to start..."
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

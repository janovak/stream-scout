#!/bin/bash
# submit-flink-job.sh
# Helper script to submit Flink job if it's not already running
# Can be run manually or via systemd timer

set -eu

COMPOSE_FILE="${1:-.}/docker-compose.processing.yml"
PROJECT_DIR="${1:-.}"

cd "$PROJECT_DIR"

# Wait for Flink to be ready
echo "Waiting for Flink JobManager to be ready..."
for i in {1..60}; do
  if docker exec streamscout-flink-jobmanager /opt/flink/bin/flink list >/dev/null 2>&1; then
    echo "Flink JobManager is ready"
    break
  fi
  echo "...waiting ($i/60)"
  sleep 2
done

# Check if job already exists
if docker exec streamscout-flink-jobmanager /opt/flink/bin/flink list | grep -q 'Clip Detector Job'; then
  echo "Flink job already running. Nothing to do."
  exit 0
fi

# Submit job
echo "Submitting Flink job..."
docker exec streamscout-flink-jobmanager /opt/flink/bin/flink run -py /opt/flink/usrlib/clip_detector_job.py -d

# Verify
sleep 5
if docker exec streamscout-flink-jobmanager /opt/flink/bin/flink list | grep -q 'Clip Detector Job'; then
  echo "✓ Flink job submitted and running"
  exit 0
else
  echo "✗ Flink job submission failed!"
  exit 1
fi

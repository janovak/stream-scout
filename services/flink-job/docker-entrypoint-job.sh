#!/bin/bash

# Custom entrypoint for PyFlink standalone-job mode
# Starts the JobManager and automatically submits the PyFlink job

set -e

# Start the JobManager in the background
echo "Starting Flink JobManager..."
/docker-entrypoint.sh jobmanager &

# Wait for JobManager to be ready
echo "Waiting for JobManager to be ready..."
until curl -s http://localhost:8081/overview > /dev/null 2>&1; do
    echo "  JobManager not ready yet, waiting..."
    sleep 2
done
echo "JobManager is ready!"

# Submit the PyFlink job
echo "Submitting PyFlink job..."
flink run -py /opt/flink/usrlib/clip_detector_job.py -d

echo "Job submitted successfully!"

# Keep the container running by waiting on the JobManager process
wait

#!/bin/bash

# Stream Scout - Start Script
# Run this to start all services from scratch

set -e

echo "=== Stream Scout Startup ==="
echo ""

# Stop any existing containers
echo "[1/5] Stopping existing containers..."
docker-compose down

echo ""
echo "[2/5] Starting all services..."
docker-compose up -d

echo ""
echo "[3/5] Waiting 60 seconds for services to initialize..."
sleep 60

echo ""
echo "[4/5] Submitting Flink job..."
docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d

echo ""
echo "[5/5] Waiting 15 seconds for job to start..."
sleep 15

echo ""
echo "=== Startup Complete ==="
echo ""
echo "Services running:"
docker-compose ps --format "table {{.Name}}\t{{.Status}}"
echo ""
echo "Flink job status:"
docker exec streamscout-flink-jobmanager flink list
echo ""
echo "URLs:"
echo "  Frontend:  http://localhost:5000"
echo "  Flink UI:  http://localhost:8081"
echo "  Grafana:   http://localhost:3000 (admin/admin)"
echo ""

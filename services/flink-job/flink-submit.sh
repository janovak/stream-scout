#!/bin/sh
set -eu

# wait for Flink CLI / JobManager to be ready
for i in $(seq 1 20); do
  if /opt/flink/bin/flink list >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

# only submit if job not already present
if /opt/flink/bin/flink list | grep -q 'Clip Detector Job'; then
  echo "Flink job already present"
  exit 0
fi

echo "Submitting Flink job..."
/opt/flink/bin/flink run -py /opt/flink/usrlib/clip_detector_job.py -d

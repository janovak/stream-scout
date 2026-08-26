#!/bin/bash

# Custom entrypoint for PyFlink Application Mode.
#
# Runs the Clip Detector job as part of the JobManager's own startup,
# using Flink's Application Mode (standalone-job.sh --job-classname).
# org.apache.flink.client.python.PythonDriver is the Java entry point
# Flink ships for running a Python job this way -- it is not a custom
# integration. There is no separate "submit the job" step at all: the
# job's Python driver runs inline as the cluster boots, and Flink ties
# the cluster's lifecycle to the job's. If the job never starts (a
# startup exception) or later reaches a terminal failure, the whole
# process exits, and `restart: unless-stopped` brings up a fresh
# container and a fresh attempt. There is nothing to duplicate, because
# there is no second command that could ever race this one.
#
# -pyFiles (below, -pyfs) comes from the FLINK_PYFILES environment
# variable, set in docker-compose.yml. It currently lists spike_detector.py,
# token_manager.py, and clip_attempt.py. clip_detector_job.py imports all
# three, and they do not inherit sys.path from clip_detector_job.py's own
# directory -- without each one listed here, the job fails at runtime
# with "ModuleNotFoundError: No module named 'spike_detector'" (or
# 'token_manager', or 'clip_attempt'), not at this check below. The
# check below only catches the variable being unset entirely, not an
# incomplete list.
#
# Editing docker-compose.yml and running "docker compose up -d" picks up
# a change immediately. No image rebuild is needed for that.

set -e

if [ -z "$FLINK_PYFILES" ]; then
    echo "ERROR: FLINK_PYFILES is not set. Refusing to start." >&2
    exit 1
fi

echo "Starting Clip Detector Job (Application Mode)..."
exec /opt/flink/bin/standalone-job.sh start-foreground \
    --job-classname org.apache.flink.client.python.PythonDriver \
    -pyclientexec /usr/bin/python3 \
    -py /opt/flink/usrlib/clip_detector_job.py \
    -pyfs "$FLINK_PYFILES"

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
# Every module is copied by name. A new one is invisible until it is listed
# here AND bind-mounted in docker-compose.yml -- the same class of gap that
# bit the Flink job's -pyFiles list.
COPY stream_monitoring_service.py .
COPY desired_set_store.py .
COPY reconciler.py .
COPY eventsub_pool.py .
COPY token_manager.py .

# Run as uid:gid 9999, matching the Flink containers (their base image's
# `flink` user). All three share the bind-mounted secrets/ dir and read and
# write the Twitch token file there; a single uid keeps those writes mutually
# accessible and removes the root-vs-9999 asymmetry that let one container's
# token write lock the others out of a refresh. TWITCH_TOKEN_GID in
# docker-compose.yml must stay 9999 to match.
RUN groupadd -g 9999 app \
    && useradd -u 9999 -g 9999 --no-create-home --shell /usr/sbin/nologin app \
    && chown -R 9999:9999 /app
USER 9999

# Expose ports
EXPOSE 8080 9100

# Run the service
CMD ["python", "stream_monitoring_service.py"]

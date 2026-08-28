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
COPY reconciler.py .
COPY eventsub_pool.py .
COPY token_manager.py .

# Expose ports
EXPOSE 8080 9100

# Run the service
CMD ["python", "stream_monitoring_service.py"]

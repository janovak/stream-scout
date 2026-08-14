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

# Bound twitchAPI's Chat.leave_room() with a timeout (upstream doesn't have
# this; see patches/twitchapi_leave_room_timeout.py for why)
COPY patches/twitchapi_leave_room_timeout.py /tmp/
RUN python3 /tmp/twitchapi_leave_room_timeout.py && rm /tmp/twitchapi_leave_room_timeout.py

# Copy application code
COPY stream_monitoring_service.py .
COPY token_manager.py .

# Expose ports
EXPOSE 8080 9100

# Run the service
CMD ["python", "stream_monitoring_service.py"]

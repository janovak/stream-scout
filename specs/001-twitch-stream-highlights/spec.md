# Twitch Stream Highlights System - Specification

## What We're Building

A distributed system that monitors popular Twitch streams, detects moments of high engagement through chat activity analysis using Apache Flink, automatically creates clips of these moments, and presents them to users through a web interface.

## Core Functionality

**1. Stream Monitoring Service**
- Poll Twitch Streams API every 2 minutes to identify top live streamers
- Filter streamers who have clipping disabled
- Automatically join chat rooms of top-ranked streamers
- Use hysteresis for chat room management to preserve Flink baseline data
- Listen to all messages in joined chat rooms and publish to Kafka
- Dynamically manage chat room lifecycle based on stream status
- Use Redis TTL to detect when streamers go offline
- Store streamer metadata in Postgres
- Expose Prometheus metrics for stream count, message rate, connection health

**2. Apache Flink Stream Processing Job**
- Consume chat messages from Kafka topic `chat-messages`
- Filter out command messages (starting with !) to avoid false positives
- Use sliding windows (5-second buckets) to track message frequency per broadcaster
- Build baseline for 5 minutes before firing any anomalies
- Detect statistical anomalies when activity exceeds baseline mean + 3 standard deviation
- Implement 30-second cooldown between detections per broadcaster using Flink state
- On anomaly detection, wait 10 seconds then call Twitch Clips API (centers the moment in the clip)
- Smart retry logic for transient errors (retries at 2s and 4s after initial attempt)
- Token validation at startup with fail-fast behavior if tokens are invalid or missing
- Wait for clip processing and retrieve metadata (15 sec after successful creation)
- Store clip information directly in Postgres
- Expose Flink metrics for monitoring (messages processed, anomalies detected, clips created)
- Run as PyFlink job in standalone cluster via Docker Compose
- Load user OAuth tokens from shared token file (same as Stream Monitoring Service)

**3. API & Frontend Service**
- Direct Postgres database access with connection pooling
- REST API endpoints:
  - `GET /`: Frontend HTML
  - `GET /v1.0/clip?start=<iso8601>&end=<iso8601>&limit=<int>`: Query clips
  - `GET /health`: Health check
  - `GET /static/*`: Static assets
- Serve static frontend assets (HTML/CSS/JS)
- Web interface displaying recent clips in responsive grid
- Support for time-range queries and pagination
- Connection pooling for database efficiency

**4. Monitoring & Observability**
- Prometheus metrics exposed by Stream Monitoring Service and API Service
- Flink metrics reporter configured for Prometheus integration
- Centralized logging via Promtail to Grafana Loki
- Grafana dashboards for operational visibility (services + Flink job)
- Alertmanager with Discord webhook notifications for critical alerts

**5. Twitch OAuth Authentication (Headless)**
- Support headless/server deployment without interactive browser authentication at runtime
- One-time initial token seeding via CLI tool that uses pyTwitchAPI's `CodeFlow`:
  - Generate authorization URL for user to visit in browser
  - Wait for user to complete OAuth flow on Twitch website
  - Receive and persist access token + refresh token to JSON file
- Runtime authentication loads pre-seeded tokens from JSON file
- Use `set_user_authentication()` with stored tokens and refresh token
- Automatic token refresh via pyTwitchAPI's built-in refresh mechanism
- Persist refreshed tokens via `user_auth_refresh_callback` to update JSON file
- Required OAuth scopes: `chat:read` (join chat rooms), `clips:edit` (create clips)
- Token file stored as Docker volume mount for persistence across container restarts
- Refresh tokens never expire for Confidential client types (only invalidate if user changes password or disconnects app)

## Architecture Benefits

**Simplicity**:
- 2 core services + Flink job (minimal deployment units)
- Direct database access with connection pooling
- Clean service boundaries with clear responsibilities

**Technology Stack**:
- Single database: Postgres for all persistent data
- Single message queue: Kafka for all inter-service communication
- Industry-standard stream processing with Apache Flink (PyFlink)

**Operational**:
- Simple service dependency graph
- Low inter-service communication overhead
- Co-located related logic for easier debugging
- Cost-effective infrastructure (minimal running processes)

## User Workflows

**End User Journey**:
1. User visits web application homepage
2. System fetches clips from past 7 days via REST API
3. Clips appear as clickable thumbnails sorted by recency
4. User clicks thumbnail to watch clip inline
5. Clip auto-plays with Twitch embed player

**System Workflow** (Simplified):
1. Stream Monitoring Service identifies top 10 live streamers every 2 minutes
2. Service joins chat rooms for streamers entering top 5, leaves only when they exit top 10 (hysteresis)
3. Chat messages are published to Kafka for all joined streamers
4. Flink job consumes messages, detects anomalies, creates clips, and stores in Postgres
5. API Service queries Postgres and serves data to frontend

## Functional Requirements

- System must monitor at least top 5 most popular streamers at any time
- Anomaly detection must build baseline for 5 minutes before firing any anomalies
- Clip API must be called 10 seconds after anomaly detection to center the moment in the clip
- API must support time-range queries with ISO 8601 timestamps
- All services must expose `/health` endpoint for liveness checks
- Frontend must load clips from past 7 days on initial page load
- System must handle graceful shutdown (at-least-once delivery via Kafka is sufficient)

## Data Models

**Postgres Schema**:

**Table: `streamers`**
```sql
CREATE TABLE streamers (
    broadcaster_id BIGINT PRIMARY KEY,
    streamer_login VARCHAR(255) NOT NULL,
    allows_clipping BOOLEAN DEFAULT TRUE,
    first_seen_at TIMESTAMP DEFAULT NOW(),
    last_seen_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_streamers_last_seen ON streamers(last_seen_at);
```

**Table: `clips`**
```sql
CREATE TABLE clips (
    id SERIAL PRIMARY KEY,
    broadcaster_id BIGINT NOT NULL,
    clip_id VARCHAR(255) NOT NULL UNIQUE,
    embed_url TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    detected_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_clips_detected_at ON clips(detected_at DESC);
CREATE INDEX idx_clips_broadcaster_id ON clips(broadcaster_id);
```

**Kafka Message Schemas**:

**Topic: `stream-lifecycle`**
```json
{
  "event_type": "online|offline",
  "broadcaster_id": 12345,
  "broadcaster_login": "streamer_name",
  "rank": 1,
  "timestamp": 1704067200
}
```

**Topic: `chat-messages`**
```json
{
  "broadcaster_id": 12345,
  "timestamp": 1704067200000,
  "message_id": "uuid-string",
  "text": "message content",
  "user_id": 67890,
  "user_name": "viewer_name",
  "metadata": {
    "emotes": {},
    "badges": {},
    "is_subscriber": false,
    "is_mod": false
  }
}
```

Note: We no longer store individual chat messages in Postgres. They are only used transiently for anomaly detection.

## Debugging & Operational Notes

**Flink Job Submission**:
- The Flink job must be manually submitted after the cluster starts
- Submit command: `docker exec streamscout-flink-jobmanager flink run -py /opt/flink/usrlib/clip_detector_job.py -d`
- The job requires ~5 minutes to build baseline data before detecting anomalies

## Observability

### Grafana Dashboard

The StreamScout Overview dashboard (`configs/grafana/dashboards/streamscout-overview.json`) includes panels for:

- **Active Streams**: Current number of monitored streams (stat panel)
- **Chat Message Rate**: Messages per second across all streams (stat panel)
- **Chat Message Rate Over Time**: Time series of message throughput
- **Flink Processing Rate**: Records in/out for the Flink job

### Prometheus Scrape Targets

Configured in `configs/prometheus.yml`:

| Job Name | Target | Description |
|----------|--------|-------------|
| `prometheus` | `localhost:9090` | Prometheus self-monitoring |
| `stream-monitoring` | `stream-monitoring:9100` | Stream monitoring service metrics |
| `flink-jobmanager` | `flink-jobmanager:9249` | Flink JobManager metrics |
| `flink-taskmanager` | `flink-taskmanager:9249` | Flink TaskManager metrics |
| `node` | `node-exporter:9100` | Host system metrics |

### Infrastructure Alerts

Prometheus Alertmanager is configured with Discord webhook notifications. Generic infrastructure alert rules are defined in `configs/prometheus/alert_rules.yml`:

| Alert | Condition | Severity | Description |
|-------|-----------|----------|-------------|
| `KafkaConsumerLagHigh` | lag > 10,000 for 5m | warning | Consumer group falling behind |
| `FlinkTaskManagerDown` | down for 2m | critical | TaskManager unreachable |
| `FlinkJobManagerDown` | down for 2m | critical | JobManager unreachable |
| `HighMemoryUsage` | > 85% for 5m | warning | Node memory threshold exceeded |
| `StreamMonitoringDown` | down for 2m | critical | Stream monitoring service unreachable |
| `NoChatMessagesProcessed` | rate = 0 for 5m with active streams | warning | Pipeline may be stalled |

### Alertmanager Configuration

Alertmanager (`configs/alertmanager.yml`) routes alerts to Discord:

- **Group wait**: 30s (10s for critical alerts)
- **Group interval**: 5m
- **Repeat interval**: 4h (1h for critical alerts)
- **Notification channel**: Discord webhook

### Logging

All services emit structured logs to stdout, collected by Promtail and stored in Loki. Query logs via Grafana's Explore view with LogQL.

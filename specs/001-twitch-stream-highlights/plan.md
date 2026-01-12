# Twitch Stream Highlights System - Implementation Plan

## Technology Stack

**Languages**:
- Python 3.11+ for backend services
- PyFlink (Apache Flink Python API) for stream processing

**Stream Processing**:
- Apache Flink 1.18+ (standalone cluster)
- PyFlink for job implementation
- Flink Kafka Connector for consuming from Kafka
- Flink JDBC Connector for writing to Postgres
- No state checkpointing (hobby project - rebuild from Kafka on restart)
- 4 task slots for parallelism
- Sliding window aggregation (5-second windows)

**Message Queue**:
- Apache Kafka (Confluent Kafka client for services)
- Topics: `stream-lifecycle`, `chat-messages`
- Flink consumes from `chat-messages` topic

**Database**:
- PostgreSQL exclusively (psycopg2-binary)
- Connection pooling (psycopg2.pool)
- Tables: `streamers`, `clips`

**Caching**:
- Redis for managing online streamer state with TTL-based expiration
- Redis keyspace notifications for offline detection

**API Layer**:
- Flask for REST API server
- Flask-CORS for cross-origin requests
- Flask Parameter Validation for request validation
- Direct database access (no gRPC)

**External APIs**:
- TwitchAPI Python library (v4.5.0) for Twitch integration (used in Stream Monitoring Service)
- Twitch API HTTP calls from PyFlink for clip creation (requests library or aiohttp)

**Monitoring & Observability**:
- Prometheus (prometheus-client library) for service metrics
- Flink Prometheus Reporter for Flink job metrics
- Grafana Cloud for metrics visualization
- Promtail for log shipping
- Loki for log aggregation
- Structured logging (JSON format)
- Flink Web UI dashboard (port 8081) for job monitoring

**Scheduling**:
- APScheduler for periodic polling tasks

**Utilities**:
- asyncio for concurrent operations
- aiohttp for async HTTP requests

## Architecture Overview

**Services & Processing Jobs**:

1. **Stream Monitoring Service** (`stream_monitoring_service.py`)
   - **Responsibilities**:
     - Scheduled polling of Twitch Streams API (every 2 minutes)
     - Manage chat room connections (join/leave)
     - Publish chat messages to Kafka
     - Publish stream lifecycle events to Kafka
     - Update streamer database
     - Maintain Redis cache of online streamers
   - **External Dependencies**:
     - Twitch API (streams, chat)
     - Kafka Producer
     - Postgres (streamers table)
     - Redis
   - **Exposed Ports**:
     - 9100: Prometheus metrics
     - 8080: Health check endpoint
   - **Metrics**:
     - `active_stream_count`: Number of currently monitored streams
     - `chat_message_rate`: Messages per second
     - `twitch_api_errors_total`: API error counter

2. **Apache Flink Stream Processing Job** (`clip_detector_job.py`)
   - **Responsibilities**:
     - Consume chat messages from Kafka topic `chat-messages`
     - Filter command messages (starting with !)
     - Window messages by broadcaster_id using sliding windows (5-second buckets)
     - Calculate message rate statistics (mean, standard deviation)
     - Detect anomalies (recent activity > baseline + 3 std dev)
     - Track 30-second cooldown per broadcaster in Flink state
     - Call Twitch Clips API to create clip on anomaly
     - Retry failed clips (max 3 attempts within 10-second window: 0s, 3s, 6s delays)
     - Write clip metadata to Postgres clips table
   - **External Dependencies**:
     - Kafka (consumer)
     - Twitch API (clips endpoint)
     - Postgres (clips table via JDBC)
   - **Deployment**:
     - Flink JobManager (coordinator)
     - Flink TaskManager (1 instance, 4 task slots)
   - **Exposed Ports**:
     - 8081: Flink Web UI
     - 9249: Flink Prometheus metrics
   - **Flink Configuration**:
     - Parallelism: 4
     - Checkpointing: Disabled
     - State backend: In-memory (RocksDB not needed)
     - Window type: Sliding (5-second size, 1-second slide)
   - **Metrics**:
     - `flink_taskmanager_job_task_numRecordsIn`: Messages consumed
     - `flink_taskmanager_job_task_numRecordsOut`: Clips created
     - Custom metrics via Flink MetricGroup:
       - `anomalies_detected`: Counter per broadcaster
       - `clips_created_success`: Counter
       - `clips_created_failed`: Counter
       - `twitch_api_latency`: Histogram

3. **API & Frontend Service** (`api_frontend_service.py`)
   - **Responsibilities**:
     - Serve REST API for clip queries
     - Serve static frontend files
     - Direct Postgres database queries
     - Handle pagination and filtering
   - **External Dependencies**:
     - Postgres (read-only access to clips table)
   - **Exposed Ports**:
     - 5000: HTTP server (API + static files)
   - **Endpoints**:
     - `GET /`: Frontend HTML
     - `GET /v1.0/clip?start=<iso8601>&end=<iso8601>&limit=<int>`: Query clips
     - `GET /health`: Health check
     - `GET /static/*`: Static assets

**Simplified Data Flow**:
```
Twitch API → Stream Monitoring Service → Kafka (chat-messages)
      ↓                                         ↓
    Postgres (streamers)            Apache Flink Job (PyFlink)
                                                ↓
                                    Filter commands, detect anomalies
                                                ↓
                                    Twitch API (create clip)
                                                ↓
                                    Postgres (clips)
                                                ↓
                                    API & Frontend Service
                                                ↓
                                            Browser
```

## Service Communication

**Kafka Topics**:

- `stream-lifecycle`: Stream online/offline events
  - Retention: 7 days
  - Partitions: 10
  - Replication factor: 3
  - Compaction: None

- `chat-messages`: Real-time chat messages
  - Retention: 1 hour (transient processing only)
  - Partitions: 20 (keyed by broadcaster_id)
  - Replication factor: 3
  - Compaction: None

**Database Connection Pooling**:
- Minimum connections: 2
- Maximum connections: 10
- Connection timeout: 30 seconds
- Idle timeout: 600 seconds

## Deployment Architecture

**Container Structure** (Docker):
```
twitch-highlights/
├── services/
│   ├── stream-monitoring/
│   │   ├── Dockerfile
│   │   └── stream_monitoring_service.py
│   ├── flink-job/
│   │   ├── Dockerfile (extends Flink base image)
│   │   ├── clip_detector_job.py
│   │   ├── requirements.txt (pyflink, requests, psycopg2)
│   │   └── flink-conf.yaml
│   └── api-frontend/
│       ├── Dockerfile
│       ├── api_frontend_service.py
│       └── static/
├── docker-compose.yml
├── configs/
│   ├── prometheus.yml
│   ├── promtail-config.yml
│   └── flink-conf.yaml
└── infrastructure/
    └── postgres/
        └── init.sql
```

**Environment Variables**:

*Stream Monitoring Service:*
- `KAFKA_BROKER_URL`: Kafka connection string
- `POSTGRES_URL`: Postgres connection string
- `REDIS_URL`: Redis connection string
- `TWITCH_CLIENT_ID`: Twitch API credentials
- `TWITCH_CLIENT_SECRET`: Twitch API credentials
- `PROMETHEUS_PORT`: 9100
- `HEALTH_CHECK_PORT`: 8080
- `LOG_LEVEL`: INFO

*Flink Job:*
- `KAFKA_BOOTSTRAP_SERVERS`: Kafka brokers (passed to Flink Kafka connector)
- `POSTGRES_URL`: JDBC connection string (e.g., jdbc:postgresql://postgres:5432/twitch)
- `POSTGRES_USER`: Database username
- `POSTGRES_PASSWORD`: Database password
- `TWITCH_CLIENT_ID`: Twitch API credentials
- `TWITCH_CLIENT_SECRET`: Twitch API credentials
- `FLINK_PARALLELISM`: 4
- `FLINK_JOB_MANAGER_MEMORY`: 1024m
- `FLINK_TASK_MANAGER_MEMORY`: 2048m

*API & Frontend Service:*
- `POSTGRES_URL`: Postgres connection string
- `HTTP_PORT`: 5000
- `LOG_LEVEL`: INFO

# Twitch Stream Highlights System - Implementation Tasks

## Infrastructure Setup

- [X] Design and create Postgres schema (streamers, clips tables)
- [X] Configure Kafka cluster with topics (stream-lifecycle, chat-messages)
- [X] Set up Redis with keyspace notifications enabled
- [X] Deploy Flink standalone cluster (JobManager + TaskManager via Docker Compose)
- [X] Configure Flink Prometheus metrics reporter
- [X] Configure Prometheus scraping for all services and Flink
- [X] Configure Promtail for log shipping
- [X] Create Grafana dashboards for services and Flink job

## Service Implementation

**Stream Monitoring Service**:
- [X] Implement Twitch API polling scheduler
- [X] Implement chat connection manager
- [X] Implement hysteresis for chat room join/leave (join at top 5, leave at top 10)
- [X] Implement Kafka producer for chat messages
- [X] Implement Kafka producer for lifecycle events
- [X] Implement Postgres streamer persistence
- [X] Implement Redis cache management
- [X] Add Prometheus metrics
- [X] Add health check endpoint
- [X] Add graceful shutdown handling
- [X] Implement headless OAuth authentication with pre-seeded tokens
- [X] Implement token refresh callback to persist refreshed tokens
- [X] Unit tests for anomaly detection logic
- [X] Integration tests with mocked Twitch API

**Twitch OAuth Token Seeding**:
- [X] Create `seed_twitch_tokens.py` CLI tool using pyTwitchAPI CodeFlow
- [X] Implement token file save/load functions (JSON format)
- [X] Add secrets directory and Docker volume mount configuration
- [X] Update docker-compose.yml with token file volume mount
- [X] Update Stream Monitoring Service to load tokens from file on startup
- [X] Register user_auth_refresh_callback to update token file on refresh

**Flink Stream Processing Job**:
- [X] Set up PyFlink development environment
- [X] Implement Kafka source connector (FlinkKafkaConsumer)
- [X] Implement command message filter (regex: ^![a-zA-Z0-9]+)
- [X] Implement keyBy(broadcaster_id) for partitioned processing
- [X] Implement sliding window (5-second window, 1-second slide)
- [X] Implement window aggregation function (count messages per window)
- [X] Implement anomaly detection logic (mean + 1 std dev baseline comparison)
- [X] Implement process function for stateful cooldown tracking (30 seconds)
- [X] Implement Twitch API clip creation with async HTTP requests
- [X] Implement retry logic within 10-second window (3 attempts: 0s, 3s, 6s delays)
- [X] Implement JDBC sink for writing clips to Postgres
- [X] Add custom Flink metrics (anomalies_detected, clips_created, etc.)
- [X] Configure Flink Prometheus reporter
- [X] Implement proper error handling and logging
- [X] Unit tests for anomaly detection logic
- [X] Implement startup token validation (call /oauth2/validate endpoint)
- [X] Add masked token logging at startup for debugging
- [X] Implement proactive token refresh if expired at startup
- [X] Add clear error messages for token issues (missing file, invalid token, refresh failure)
- [ ] Integration tests with Flink MiniCluster
- [ ] End-to-end test with Kafka and Postgres

**Flink Job Debugging & Fixes (2026-01-12)**:
- [X] Diagnose why no clips were being created
- [X] Identify Flink job not auto-starting after docker-compose up
- [X] Fix invalid checkpoint interval config (`execution.checkpointing.interval: 0`)
- [X] Fix tuple vs string type mismatch in AnomalyDetector.process_element()
- [X] Fix UnboundLocalError in exception handler (broadcaster_id referenced before assignment)
- [X] Add comprehensive logging to TwitchAPIClient (OAuth, create_clip, get_clip)
- [X] Add comprehensive logging to PostgresClient (connection, insert, conflict detection)
- [X] Add comprehensive logging to AnomalyDetector (baseline progress, anomaly detection, cooldown)
- [X] Add comprehensive logging to ClipCreator (full pipeline with timing)
- [X] Add startup configuration logging to main()
- [X] Verify Flink job consumes from Kafka successfully
- [X] Document manual job submission procedure

**Flink Job OAuth & Retry Fixes (2026-01-13)**:
- [X] Diagnose 401 "Missing User OAUTH Token" errors
- [X] Mount user token file to Flink containers (docker-compose.yml)
- [X] Update TwitchAPIClient to load user tokens from JSON file
- [X] Implement token refresh flow using refresh_token grant type
- [X] Save refreshed tokens back to file for persistence
- [X] Add TwitchAPIError exception with is_retryable flag
- [X] Define RETRYABLE_STATUS_CODES (408, 429, 500, 502, 503, 504)
- [X] Update ClipCreator to use smart retry logic
- [X] Stop retrying on non-retryable errors (400, 401, 403, 404)
- [X] Auto-refresh token on 401 before marking as non-retryable
- [X] Add TWITCH_TOKEN_FILE to startup configuration logging

**API & Frontend Service**:
- [X] Implement Flask application structure
- [X] Implement Postgres connection pool
- [X] Implement GET /v1.0/clip endpoint
- [X] Implement query parameter validation
- [X] Implement pagination logic
- [X] Implement health check endpoint
- [X] Serve static frontend files
- [X] Add database query optimization (indexes, EXPLAIN ANALYZE)
- [X] Add request/response logging
- [X] Unit tests for API endpoints
- [X] Integration tests with Postgres

**Frontend Updates**:
- [X] Update API endpoint URLs (if changed)
- [X] Add error handling for failed API requests
- [X] Add loading states
- [X] Test responsive design
- [ ] Browser compatibility testing

## Testing & Validation

- [ ] End-to-end integration test (Twitch → Flink → Clip Display)
- [ ] Chaos engineering: simulate Kafka failures (Flink restart from offsets)
- [ ] Chaos engineering: simulate Postgres failures
- [ ] Chaos engineering: simulate Twitch API failures (verify retries)
- [ ] Chaos engineering: Flink TaskManager failure (verify JobManager recovery)
- [ ] Load testing for high-traffic streams (1000+ msg/sec)
- [ ] Validate anomaly detection accuracy with known events
- [ ] Verify Flink windowing behavior (sliding windows)
- [ ] Verify Flink stateful cooldown tracking
- [ ] Verify Redis expiration event handling
- [ ] Verify Prometheus metrics accuracy for all components
- [ ] Verify Flink Web UI accessibility
- [ ] Verify log aggregation in Loki
- [ ] Performance benchmarking and optimization

## Documentation

- [ ] System architecture documentation
- [ ] PyFlink job implementation guide
- [ ] Flink deployment guide (Docker Compose)
- [ ] Service deployment guides
- [ ] API documentation (OpenAPI/Swagger)
- [X] Monitoring and alerting runbook (basic commands documented in spec.md)
- [X] Troubleshooting guide (debugging section in plan.md)
- [ ] Disaster recovery procedures
- [ ] Capacity planning guide

## System Characteristics

**Core Features**:
- Apache Flink for robust stream processing
- Sliding window aggregation for smooth anomaly detection
- Stateful cooldown tracking in Flink (30 seconds per broadcaster)
- Flink Web UI dashboard for job monitoring
- Flink Prometheus metrics integration
- Health check endpoints for all services
- Graceful shutdown handling
- Connection pooling for database efficiency
- Structured logging (JSON format)
- Retry logic with timing constraints (10-second window for clip creation)
- Clean service boundaries with cohesive responsibilities

**Scalability**:
- Horizontal scaling: Each service can run multiple instances
- Flink TaskManagers scale independently (add more task slots as needed)
- Kafka consumer groups for load distribution
- Flink parallel processing with keyBy(broadcaster_id) partitioning
- Postgres read replicas for API service (future enhancement)
- Redis Cluster for cache high availability (future enhancement)

**Observability**:
- Consistent metrics naming across all services
- Flink Web UI for real-time job visualization and debugging
- Flink metrics exposed to Prometheus (numRecordsIn, numRecordsOut, custom metrics)
- Custom business metrics (anomalies detected, clips created, API latency)
- Structured JSON logging for efficient parsing
- Centralized log aggregation via Loki
- Distributed tracing ready (future: OpenTelemetry integration)
- Clear service dependency mapping for troubleshooting

## Known Limitations & Future Enhancements

**Current Limitations**:
- No authentication/authorization on API endpoints
- Chat messages not persisted (only used for anomaly detection)
- Single region deployment (no geographic distribution)
- No A/B testing framework for anomaly detection tuning

**Future Enhancements**:
- User accounts and clip favoriting/sharing
- Custom anomaly detection thresholds per user
- Machine learning model for better anomaly detection (Flink ML integration)
- Real-time WebSocket notifications for new clips
- Multi-language support for frontend
- Mobile application
- Clip transcription and searchability
- Social features (comments, reactions)
- Flink state checkpointing to S3 for production resilience
- Flink SQL for ad-hoc analytics queries on chat streams

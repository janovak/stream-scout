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
- [X] Implement Kafka producer for chat messages
- [X] Implement Kafka producer for lifecycle events
- [X] Implement Postgres streamer persistence
- [X] Implement Redis cache management
- [X] Add Prometheus metrics
- [X] Add health check endpoint
- [X] Add graceful shutdown handling
- [X] Implement headless OAuth authentication with pre-seeded tokens
- [X] Implement token refresh callback to persist refreshed tokens
- [ ] Unit tests for anomaly detection logic
- [ ] Integration tests with mocked Twitch API

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
- [ ] Implement anomaly detection logic (mean + 1 std dev baseline comparison)
- [X] Implement process function for stateful cooldown tracking (30 seconds)
- [X] Implement Twitch API clip creation with async HTTP requests
- [X] Implement retry logic within 10-second window (3 attempts: 0s, 3s, 6s delays)
- [X] Implement JDBC sink for writing clips to Postgres
- [X] Add custom Flink metrics (anomalies_detected, clips_created, etc.)
- [X] Configure Flink Prometheus reporter
- [X] Implement proper error handling and logging
- [ ] Unit tests for anomaly detection logic
- [ ] Integration tests with Flink MiniCluster
- [ ] End-to-end test with Kafka and Postgres

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
- [ ] Unit tests for API endpoints
- [ ] Integration tests with Postgres

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
- [ ] Monitoring and alerting runbook
- [ ] Troubleshooting guide
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

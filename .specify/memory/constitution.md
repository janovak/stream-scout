# Twitch Stream Highlights System - Constitution

## Project Governing Principles

**Purpose**: Automatically identify and capture highlight-worthy moments from popular Twitch streams by detecting chat activity spikes and creating clips for user consumption.

**Core Values**:
- Real-time responsiveness: Monitor live streams and detect anomalies as they happen
- Scalability: Handle monitoring multiple high-traffic streams simultaneously
- Reliability: Ensure no highlight moments are missed due to system failures
- Simplicity: Reduce operational complexity through service consolidation
- Data integrity: Accurately capture and store clip metadata for retrieval

**Technical Constraints**:
- Must use Kafka for all message streaming between services
- Must use Postgres exclusively for all persistent data storage
- Must use Apache Flink (PyFlink) for stream processing and anomaly detection
- Must integrate with Twitch API for stream data and clip creation
- Must provide Prometheus metrics for monitoring
- Must use Grafana/Loki for observability

**Non-Negotiables**:
- No data loss in the event pipeline (Kafka → Processing → Storage)
- All services must expose health check endpoints
- All services must log to centralized logging (Promtail → Loki)
- Anomaly detection must filter out bot commands to avoid false positives
- Single source of truth: All data in Postgres
- Must always use Python virtual environments when installing packages
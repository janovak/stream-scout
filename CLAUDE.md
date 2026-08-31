# stream-scout Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-08-30

## Active Technologies

- Python 3.11 (unchanged; `services/stream-monitoring/Dockerfile`) + `psycopg2-binary==2.9.9`, `redis==5.0.1`, `APScheduler==3.10.4`, `prometheus-client==0.19.0`, `confluent-kafka==2.3.0`, `twitchAPI==4.5.0` (all pins unchanged) (006-batch-poller-io)

## Project Structure

```text
services/
├── stream-monitoring/
│   ├── stream_monitoring_service.py
│   ├── desired_set_store.py
│   ├── reconciler.py
│   ├── test_stream_monitoring.py
│   └── test_desired_set_store.py
├── flink-job/
└── api-frontend/
specs/
infrastructure/
configs/
```

## Commands

```text
cd services/stream-monitoring
source .venv/bin/activate
python -m pytest -q test_stream_monitoring.py test_desired_set_store.py
```

## Code Style

Python 3.11: follow the existing service conventions and keep tests co-located
with the modules they cover.

## Recent Changes

- 006-batch-poller-io: Added Python 3.11 (unchanged; `services/stream-monitoring/Dockerfile`) + `psycopg2-binary==2.9.9`, `redis==5.0.1`, `APScheduler==3.10.4`, `prometheus-client==0.19.0`, `confluent-kafka==2.3.0`, `twitchAPI==4.5.0` (all pins unchanged)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->

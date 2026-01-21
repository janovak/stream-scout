# Quickstart: Clips Anomaly Selector

**Feature**: 002-clips-anomaly-selector
**Date**: 2026-01-20

## Prerequisites

- Docker and Docker Compose installed
- Python 3.11+ with venv
- Existing stream-scout environment running

## Development Setup

### 1. Start the Environment

```bash
# From project root
docker-compose up -d
```

This starts:
- PostgreSQL (port 5432)
- Kafka + Zookeeper
- Redis
- Flink cluster
- All microservices

### 2. Apply Database Migration

```bash
# Connect to postgres container
docker exec -it stream-scout-postgres-1 psql -U stream_scout -d stream_scout

# Run migration
ALTER TABLE clips ADD COLUMN intensity FLOAT;
CREATE INDEX idx_clips_intensity_detected ON clips (intensity, detected_at DESC);

# Verify
\d clips
```

### 3. Backend Development

```bash
cd services/api-frontend

# Create/activate virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Run locally (outside Docker for hot reload)
export DATABASE_URL="postgresql://stream_scout:password@localhost:5432/stream_scout"
export HTTP_PORT=5000
python api_frontend_service.py
```

### 4. Frontend Development

Frontend files are static - edit directly and refresh browser:

```
services/api-frontend/static/
├── index.html    # Page structure
├── app.js        # Application logic
└── styles.css    # Styling
```

Access at: http://localhost:5000

### 5. Flink Job Development

```bash
cd services/flink-job

# Activate venv
source venv/bin/activate

# Submit updated job to Flink cluster
# (See OPERATIONS.md for Flink job deployment)
```

## Key Files to Modify

| Component | File | Changes |
|-----------|------|---------|
| Database | `infrastructure/postgres/init.sql` | Add intensity column |
| Flink Job | `services/flink-job/flink_job.py` | Calculate & store intensity |
| API | `services/api-frontend/api_frontend_service.py` | Add filtering & pagination |
| Frontend | `services/api-frontend/static/app.js` | New UI logic |
| Styles | `services/api-frontend/static/styles.css` | Modern styling |

## Testing

### Manual API Testing

```bash
# Get clips with default filter (5+ intensity)
curl "http://localhost:5000/v1.0/clip"

# Get clips with specific intensity threshold
curl "http://localhost:5000/v1.0/clip?min_intensity=7"

# Get second page of clips
curl "http://localhost:5000/v1.0/clip?min_intensity=5&offset=24&limit=24"
```

### Frontend Testing

1. Open http://localhost:5000 in browser
2. Verify filter dropdown appears with intensity labels
3. Select different thresholds, verify clip count updates
4. Click a clip, verify inline playback starts
5. Scroll to bottom, verify more clips load automatically

## Common Issues

### Clips not showing intensity

Old clips have `NULL` intensity. Only new clips (after Flink job update) will have values.

### Autoplay not working

Check browser console. Autoplay requires:
- `allow="autoplay"` attribute on iframe
- Page must have had user interaction (click)

### Infinite scroll not triggering

Check that `has_more` is `true` in API response. Verify scroll event listener is attached.

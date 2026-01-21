# Implementation Plan: Clips Anomaly Selector & Enhanced Browser

**Branch**: `002-clips-anomaly-selector` | **Date**: 2026-01-20 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/002-clips-anomaly-selector/spec.md`

## Summary

Add anomaly intensity filtering to the clips browser, enabling users to filter clips by chat activity spike magnitude (3+ to 11+ standard deviations). Replace the current modal-based playback with inline video playback in the grid. Implement offset-based infinite scrolling instead of loading 7 days of clips. Modernize the page UI while preserving the grid layout.

**Key Technical Changes**:
- Add `intensity` column to clips table (database migration)
- Modify Flink job to calculate and store intensity values
- Add pagination and intensity filtering to clips API
- Rewrite frontend for inline playback, filtering UI, and infinite scroll

## Technical Context

**Language/Version**: Python 3.11 (backend), Vanilla JavaScript (frontend)
**Primary Dependencies**: Flask 3.0.0, psycopg2, Twitch Embed API
**Storage**: PostgreSQL (existing clips table needs migration)
**Testing**: Manual testing (no test framework currently in place)
**Target Platform**: Web browser (desktop and mobile responsive)
**Project Type**: Web application (microservices architecture)
**Performance Goals**: Initial load <2s, filter response <1s, infinite scroll preloads before bottom
**Constraints**: Must use existing Kafka/Flink pipeline for data flow
**Scale/Scope**: Single-user hobby project, moderate clip volume

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| Kafka for message streaming | PASS | No changes to Kafka usage |
| Postgres for persistent storage | PASS | Adding intensity column to existing clips table |
| PyFlink for stream processing | PASS | Modifying existing Flink job to store intensity |
| Twitch API integration | PASS | Using existing Twitch embed for inline playback |
| Prometheus metrics | PASS | No new metrics required for this feature |
| Grafana/Loki observability | PASS | Existing logging sufficient |
| No data loss in pipeline | PASS | Intensity calculation is additive, not replacing |
| Health check endpoints | PASS | No new services being added |
| Centralized logging | PASS | Using existing Promtail → Loki |
| Filter bot commands | N/A | Already handled by existing Flink job |
| Single source of truth (Postgres) | PASS | All clip data including intensity in Postgres |
| Python virtual environments | PASS | Using existing venv setup |

**Gate Result**: PASS - No constitution violations.

## Project Structure

### Documentation (this feature)

```text
specs/002-clips-anomaly-selector/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (via /speckit.tasks)
```

### Source Code (repository root)

```text
services/
├── api-frontend/
│   ├── api_frontend_service.py    # Flask API (modify for pagination + filtering)
│   ├── static/
│   │   ├── index.html             # Page structure (update layout)
│   │   ├── app.js                 # Client logic (rewrite for new features)
│   │   └── styles.css             # Styling (modernize)
│   ├── requirements.txt
│   └── Dockerfile
├── flink-job/
│   ├── flink_job.py               # Anomaly detection (add intensity storage)
│   └── ...
└── stream-monitoring/
    └── ...                        # No changes needed

infrastructure/
└── postgres/
    └── init.sql                   # Schema (add intensity column)
```

**Structure Decision**: Existing web application structure with backend (Flask API) and frontend (static files). No new services needed - modifying existing api-frontend service and flink-job.

## Complexity Tracking

> No constitution violations requiring justification.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| None | N/A | N/A |

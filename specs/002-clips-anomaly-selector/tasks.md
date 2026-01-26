# Tasks: Clips Anomaly Selector & Enhanced Browser

**Input**: Design documents from `/specs/002-clips-anomaly-selector/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Manual testing only (no test framework in place per plan.md)

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3, US4)
- Include exact file paths in descriptions

## Path Conventions

- **Backend**: `services/api-frontend/api_frontend_service.py`
- **Frontend**: `services/api-frontend/static/` (index.html, app.js, styles.css)
- **Flink**: `services/flink-job/flink_job.py`
- **Schema**: `infrastructure/postgres/init.sql`

---

## Phase 1: Setup

**Purpose**: No new project setup needed - modifying existing codebase

- [x] T001 Review existing code structure and verify all target files exist (Note: Flink job is clip_detector_job.py not flink_job.py)
- [x] T002 Create feature branch backup point with `git tag pre-002-clips-anomaly`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Database and data pipeline changes that MUST complete before any frontend work

**⚠️ CRITICAL**: User stories cannot be fully tested until clips have intensity values

- [x] T003 Add intensity column to clips table in infrastructure/postgres/init.sql
- [ ] T004 Run database migration: `ALTER TABLE clips ADD COLUMN intensity FLOAT;` (MANUAL: run when DB available)
- [ ] T005 Create index for filtered queries: `CREATE INDEX idx_clips_intensity_detected ON clips (intensity, detected_at DESC);` (MANUAL: run when DB available)
- [x] T006 Update ClipResult dataclass to include intensity field in services/flink-job/clip_detector_job.py
- [x] T007 Calculate intensity Z-score in AnomalyDetector.process_element() in services/flink-job/clip_detector_job.py
- [x] T008 Update insert_clip() to store intensity value in services/flink-job/clip_detector_job.py
- [ ] T009 Redeploy Flink job and verify new clips have intensity values (MANUAL: requires deployment)

**Checkpoint**: New clips now have intensity values stored - frontend work can begin

---

## Phase 3: User Story 1 - Filter Clips by Anomaly Intensity (Priority: P1) 🎯 MVP

**Goal**: Users can filter clips by intensity threshold (Warming Up, Getting Good, Popping Off, Unhinged, Legendary)

**Independent Test**: Select different thresholds from dropdown and verify only matching clips appear with correct count displayed

### Implementation for User Story 1

- [x] T010 [P] [US1] Add INTENSITY_LEVELS constant with threshold-label mapping in services/api-frontend/static/app.js
- [x] T011 [P] [US1] Update GET /v1.0/clip endpoint to accept min_intensity query parameter in services/api-frontend/api_frontend_service.py
- [x] T012 [US1] Update SQL query to filter by intensity >= min_intensity in services/api-frontend/api_frontend_service.py
- [x] T013 [US1] Add intensity field to clip response JSON in services/api-frontend/api_frontend_service.py
- [x] T014 [US1] Create filter dropdown UI component in services/api-frontend/static/index.html
- [x] T015 [US1] Implement filter change handler that reloads clips with selected threshold in services/api-frontend/static/app.js (Note: JS state persists filter until page refresh, satisfying FR-003)
- [x] T016 [US1] Display total count of matching clips in UI in services/api-frontend/static/app.js
- [x] T017 [US1] Add intensity label badge to each clip card in services/api-frontend/static/app.js
- [x] T018 [US1] Handle empty results with "no clips match" message in services/api-frontend/static/app.js
- [x] T019 [US1] Set default filter to "Getting Good" (5+) on page load in services/api-frontend/static/app.js
- [x] T020 [US1] Style filter dropdown and count display in services/api-frontend/static/styles.css

**Checkpoint**: Filter functionality complete - users can select intensity threshold and see filtered clips

---

## Phase 4: User Story 2 - Inline Clip Playback (Priority: P1)

**Goal**: Clicking a clip plays it inline without opening a modal

**Independent Test**: Click any clip thumbnail and verify video plays in place; click another clip and verify first stops

### Implementation for User Story 2

- [x] T021 [US2] Remove modal open logic from clip click handler in services/api-frontend/static/app.js
- [x] T022 [US2] Implement inline iframe player that replaces thumbnail on click in services/api-frontend/static/app.js (Note: Twitch embed provides native playback controls per FR-008; clips continue playing when scrolled out of view per spec edge case)
- [x] T023 [US2] Add allow="autoplay" attribute to iframe for browser autoplay permission in services/api-frontend/static/app.js
- [x] T024 [US2] Track currently playing clip ID in state variable in services/api-frontend/static/app.js
- [x] T025 [US2] Stop previous clip (remove iframe) when new clip is clicked in services/api-frontend/static/app.js
- [x] T026 [US2] Add play icon overlay on hover for clip cards in services/api-frontend/static/app.js
- [x] T027 [US2] Style inline player container with proper aspect ratio in services/api-frontend/static/styles.css
- [x] T028 [US2] Remove modal HTML and CSS from codebase in services/api-frontend/static/index.html and styles.css

**Checkpoint**: Inline playback complete - single-click plays clips in grid

---

## Phase 5: User Story 3 - Infinite Scroll (Priority: P2)

**Goal**: Clips load automatically as user scrolls, with pagination instead of 7-day window

**Independent Test**: Scroll to bottom and verify more clips load; apply filter and verify list resets

### Implementation for User Story 3

- [x] T029 [US3] Add offset parameter to GET /v1.0/clip endpoint in services/api-frontend/api_frontend_service.py
- [x] T030 [US3] Add total_count query (COUNT with same filter) to API response in services/api-frontend/api_frontend_service.py
- [x] T031 [US3] Add has_more boolean to API response based on offset + count < total in services/api-frontend/api_frontend_service.py
- [x] T032 [US3] Remove 7-day time window constraint from default query in services/api-frontend/api_frontend_service.py
- [x] T033 [US3] Track offset and hasMore state variables in frontend in services/api-frontend/static/app.js
- [x] T034 [US3] Implement scroll event listener with debounce (detect near-bottom) in services/api-frontend/static/app.js
- [x] T035 [US3] Implement loadMoreClips() that fetches next page and appends to grid in services/api-frontend/static/app.js
- [x] T036 [US3] Add loading spinner at bottom during fetch in services/api-frontend/static/app.js
- [x] T037 [US3] Display "No more clips" message when hasMore is false in services/api-frontend/static/app.js
- [x] T038 [US3] Reset offset to 0 and clear clips when filter changes in services/api-frontend/static/app.js
- [x] T039 [US3] Style loading spinner and end-of-list message in services/api-frontend/static/styles.css

**Checkpoint**: Infinite scroll complete - clips load continuously as user scrolls

---

## Phase 6: User Story 4 - Modernized Page Design (Priority: P3)

**Goal**: Clean, contemporary UI with smooth loading states

**Independent Test**: Visual inspection of page layout, responsive behavior, and loading transitions

### Implementation for User Story 4

- [x] T040 [P] [US4] Update page header and layout structure in services/api-frontend/static/index.html
- [x] T041 [P] [US4] Implement skeleton placeholder cards for initial load in services/api-frontend/static/app.js
- [x] T042 [US4] Add CSS custom properties for consistent spacing and colors in services/api-frontend/static/styles.css
- [x] T043 [US4] Update clip card styling with shadows, rounded corners, hover effects in services/api-frontend/static/styles.css
- [x] T044 [US4] Improve typography hierarchy (headings, metadata, labels) in services/api-frontend/static/styles.css
- [x] T045 [US4] Add smooth transitions for filter changes and clip loading in services/api-frontend/static/styles.css
- [x] T046 [US4] Ensure responsive grid works from 320px to 2560px width in services/api-frontend/static/styles.css
- [x] T047 [US4] Style filter controls to be prominent and accessible in services/api-frontend/static/styles.css

**Checkpoint**: Modern UI complete - page looks polished and professional

---

## Phase 7: Polish & Edge Cases

**Purpose**: Handle edge cases and finalize implementation

- [x] T048 Handle clips with NULL intensity (show "score unavailable" badge) in services/api-frontend/static/app.js
- [x] T049 Add error handling for failed API requests with retry option in services/api-frontend/static/app.js
- [x] T050 Handle clip video load errors with error state on card in services/api-frontend/static/app.js (Note: Thumbnail errors handled via onerror; iframe errors not detectable due to Twitch embed limitations)
- [x] T051 Add error response handling for invalid query params in services/api-frontend/api_frontend_service.py
- [ ] T052 Verify all acceptance scenarios from spec.md pass manually, including performance goals: initial load <2s (SC-003), filter response <1s (SC-002), infinite scroll preloads before reaching bottom (SC-004) (MANUAL: requires running application)
- [ ] T053 Update quickstart.md with any changes to setup or testing steps

---

## Phase 8: Observability

**Purpose**: Add metrics, dashboard panels, and alerts for clip detection monitoring

### Prometheus Metrics

- [x] T054 [P] Add prometheus-client dependency to services/flink-job/requirements.txt
- [x] T055 [P] Add METRICS_PORT environment variable to flink-taskmanager in docker-compose.yml
- [x] T056 Add lazy-initialized Prometheus metrics (_init_metrics function) in services/flink-job/clip_detector_job.py
- [x] T057 Increment anomalies_detected_total counter in AnomalyDetector.process_element() in services/flink-job/clip_detector_job.py
- [x] T058 Increment clips_created_success_total/clips_created_failed_total counters in ClipCreator.process_element() in services/flink-job/clip_detector_job.py
- [x] T059 Record clip_creation_duration_seconds gauge in ClipCreator.process_element() in services/flink-job/clip_detector_job.py
- [x] T060 Add clip-detector scrape job to configs/prometheus.yml

### Grafana Dashboard

- [x] T061 Add "Anomaly Detection & Clips" time series panel to configs/grafana/dashboards/streamscout-overview.json

### Alerting

- [x] T062 Add ClipDetectorMetricsDown alert rule to configs/prometheus/alert_rules.yml
- [x] T063 Add ClipCreationFailureSpike alert rule to configs/prometheus/alert_rules.yml
- [x] T064 Add AnomalyDetectionStalled alert rule to configs/prometheus/alert_rules.yml
- [x] T065 Add NoClipsCreated alert rule to configs/prometheus/alert_rules.yml
- [x] T066 Add Alertmanager service to docker-compose.yml
- [x] T067 Create configs/alertmanager.yml with Discord webhook configuration
- [x] T068 Update prometheus depends_on to include alertmanager in docker-compose.yml
- [x] T069 Mount alert_rules.yml in prometheus service in docker-compose.yml

**Checkpoint**: Clip detection metrics visible in Grafana, alerts firing to Discord

---

## Phase 9: Performance — Thumbnail-first Rendering & Prefetching

**Purpose**: Eliminate unnecessary Twitch embed iframes on page load and add speculative prefetching for near-instant infinite scroll

### Thumbnail-first Rendering

- [x] T070 Fix renderClips() to show thumbnail images with play overlay instead of Twitch embed iframes for non-playing clips in services/api-frontend/static/app.js
- [x] T071 Fix appendClipsToGrid() to show thumbnail images with play overlay instead of iframes, and add missing onclick handler in services/api-frontend/static/app.js

### API Data Prefetching

- [x] T072 Add prefetch state variables (prefetchedData, prefetchPromise) in services/api-frontend/static/app.js
- [x] T073 Implement prefetchNextPage() that fetches next page in background after each successful load in services/api-frontend/static/app.js
- [x] T074 Implement consumePrefetchedData() for instant rendering when scrolling triggers loadMoreClips() in services/api-frontend/static/app.js
- [x] T075 Discard prefetched data on filter change in onFilterChange() in services/api-frontend/static/app.js

### Thumbnail Image Preloading

- [x] T076 Implement preloadThumbnails() that creates Image objects to cache thumbnails for prefetched clips in services/api-frontend/static/app.js

### Batch Size

- [x] T077 Increase frontend batch size from 5 to 24 (matching API DEFAULT_CLIP_LIMIT) in loadClips() and loadClipsAppend() in services/api-frontend/static/app.js

**Checkpoint**: Page loads with lightweight thumbnails instead of iframes; scrolling triggers near-instant rendering from prefetched data

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies - start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 - **BLOCKS all user stories**
- **Phase 3-6 (User Stories)**: All depend on Phase 2 completion
  - US1 and US2 can proceed in parallel (independent)
  - US3 depends on US1 (filter reset behavior)
  - US4 can proceed in parallel with US1/US2/US3
- **Phase 7 (Polish)**: Depends on all user stories complete
- **Phase 8 (Observability)**: Independent - can proceed in parallel with user stories
- **Phase 9 (Performance)**: Depends on US2 (inline playback) and US3 (infinite scroll)

### User Story Dependencies

- **US1 (Filter)**: Depends on Foundational only - no other story dependencies
- **US2 (Inline Playback)**: Depends on Foundational only - fully independent
- **US3 (Infinite Scroll)**: Best after US1 (integrates filter reset), but can be developed independently
- **US4 (Modern UI)**: Independent - can be done in parallel with any story

### Within Each User Story

- Backend API changes before frontend implementation
- Core functionality before edge cases
- Functionality before styling

### Parallel Opportunities

- T010 and T011 (US1 frontend constant and backend param) can run in parallel
- T040 and T041 (US4 HTML and skeleton JS) can run in parallel
- US1 and US2 can be developed in parallel by different developers
- US4 styling can proceed in parallel with any functionality work

---

## Parallel Example: User Story 1

```bash
# Launch backend and frontend foundation in parallel:
Task: "Add INTENSITY_LEVELS constant in services/api-frontend/static/app.js"
Task: "Update GET /v1.0/clip to accept min_intensity in services/api-frontend/api_frontend_service.py"

# Then sequentially:
Task: "Update SQL query to filter by intensity"
Task: "Create filter dropdown UI"
Task: "Implement filter change handler"
# etc.
```

---

## Implementation Strategy

### MVP First (US1 + US2)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (database + Flink)
3. Complete Phase 3: User Story 1 (Filter)
4. Complete Phase 4: User Story 2 (Inline Playback)
5. **STOP and VALIDATE**: Core functionality works
6. Proceed with US3 (Infinite Scroll) and US4 (Modern UI)

### Incremental Delivery

1. Foundation → Clips have intensity data
2. Add US1 (Filter) → Users can filter clips → **Demo-able**
3. Add US2 (Inline Playback) → Better UX → **Demo-able**
4. Add US3 (Infinite Scroll) → Full browsing experience
5. Add US4 (Modern UI) → Polished appearance
6. Each story adds value independently

---

## Notes

- [P] tasks = different files, no dependencies
- Manual testing only per plan.md (no test framework)
- Flink job redeploy (T009) may take time - plan accordingly
- Existing clips will have NULL intensity - handled in T048
- All file paths are relative to repository root

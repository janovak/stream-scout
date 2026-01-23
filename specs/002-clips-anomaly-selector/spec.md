# Feature Specification: Clips Anomaly Selector & Enhanced Browser

**Feature Branch**: `002-clips-anomaly-selector`
**Created**: 2026-01-20
**Status**: Draft
**Input**: User description: "Selector for picking clips for specific anomaly range. Users can filter clips by standard deviation thresholds (3 std dev above mean, 5 above mean, etc). Modernize the clips page UI while keeping the clip scrolling bar. Fix loading and autoplay - clips should play directly in the scrolling bar without loading a separate player pane. Change from loading 7 days of clips to loading last X clips with infinite scrolling support."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Filter Clips by Anomaly Intensity (Priority: P1)

As a user browsing clips, I want to filter clips by how significant the chat activity spike was, so I can find the most exciting moments without sifting through less remarkable clips.

**Why this priority**: This is the core new functionality that differentiates the feature. Users explicitly want to find clips that represent truly exceptional moments (5+ standard deviations) versus moderately interesting ones (2-3 standard deviations). This directly addresses user frustration with finding quality content.

**Independent Test**: Can be fully tested by selecting different anomaly thresholds and verifying only clips meeting that threshold appear. Delivers immediate value by helping users find high-quality clips faster.

**Acceptance Scenarios**:

1. **Given** I am on the clips page with clips loaded, **When** I select "3+ standard deviations" from the anomaly filter, **Then** only clips where the chat activity was 3 or more standard deviations above the mean are displayed.
2. **Given** I have filtered clips to "5+ standard deviations", **When** I change the filter to "7+ standard deviations", **Then** the clip list updates to show fewer clips only including those with higher anomaly scores.
3. **Given** I select an anomaly threshold, **When** no clips match that threshold, **Then** I see a clear message indicating no clips match the current filter.
4. **Given** I have applied an anomaly filter, **When** I scroll to load more clips, **Then** newly loaded clips also respect the current filter setting.

---

### User Story 2 - Inline Clip Playback (Priority: P1)

As a user browsing clips, I want to click on a clip and have it play immediately within the scrolling interface, so I can quickly preview clips without context-switching to a separate player.

**Why this priority**: This directly addresses user frustration with the current two-click playback flow. Single-click inline playback dramatically improves browsing speed and user experience.

**Independent Test**: Can be fully tested by clicking any clip thumbnail and verifying video plays inline without opening a modal or separate pane. Delivers immediate UX improvement.

**Acceptance Scenarios**:

1. **Given** I am viewing the clips grid, **When** I click on a clip thumbnail, **Then** the video begins playing immediately in place without opening a separate player pane.
2. **Given** a clip is playing inline, **When** I click on a different clip, **Then** the first clip stops and the new clip begins playing.
3. **Given** a clip is playing inline, **When** I click on the playing clip again or click a pause control, **Then** the video pauses.
4. **Given** I am viewing clips, **When** I hover over a clip, **Then** I see a visual indication that the clip is playable (play icon overlay or similar).

---

### User Story 3 - Infinite Scroll with Initial Batch Loading (Priority: P2)

As a user browsing clips, I want clips to load automatically as I scroll down, starting with a reasonable initial batch, so I can continuously discover content without clicking pagination buttons.

**Why this priority**: Infinite scrolling improves content discovery and engagement. However, it builds on basic clip display functionality and is a navigation enhancement rather than core functionality.

**Independent Test**: Can be fully tested by scrolling to the bottom of the initial clip batch and verifying more clips load automatically. Delivers seamless browsing experience.

**Acceptance Scenarios**:

1. **Given** I load the clips page, **When** the page finishes loading, **Then** I see an initial batch of clips (not all clips from the last 7 days).
2. **Given** I have scrolled near the bottom of the current clips, **When** more clips are available, **Then** additional clips load automatically without requiring a button click.
3. **Given** clips are loading due to scrolling, **When** the load is in progress, **Then** I see a loading indicator at the bottom of the list.
4. **Given** I have scrolled and loaded more clips, **When** I apply an anomaly filter, **Then** the list resets to show filtered clips from the beginning with infinite scroll still working.
5. **Given** I have loaded all available clips, **When** I reach the bottom, **Then** I see a message indicating no more clips are available.

---

### User Story 4 - Modernized Page Design (Priority: P3)

As a user visiting the clips page, I want a clean, contemporary interface, so the browsing experience feels polished and professional.

**Why this priority**: Visual improvements enhance user perception and satisfaction but don't add new functionality. The current scrolling grid is already functional; this story focuses on refinement.

**Independent Test**: Can be tested by comparing the updated design against modern UI standards (spacing, typography, visual hierarchy). Delivers improved aesthetic appeal.

**Acceptance Scenarios**:

1. **Given** I visit the clips page, **When** the page loads, **Then** I see a clean, well-organized layout with clear visual hierarchy.
2. **Given** I am viewing the clips page, **When** I observe the interface, **Then** the filter controls are easily discoverable and clearly labeled.
3. **Given** I am viewing the clips page on different screen sizes, **When** I resize the browser, **Then** the layout adapts responsively while remaining usable.
4. **Given** I am browsing clips, **When** clips are loading, **Then** I see smooth loading states (skeleton placeholders or subtle animations) rather than jarring content shifts.

---

### Edge Cases

- What happens when the anomaly score data is missing for older clips? Display them with a "score unavailable" indicator and exclude them from filtered results.
- What happens when network connectivity is lost during infinite scroll? Show an error message with a retry option without losing already-loaded clips.
- What happens when a clip's video source becomes unavailable? Display an error state on that clip card with appropriate messaging.
- What happens when the user scrolls very quickly? Debounce scroll detection to prevent excessive loading requests.
- What happens when a clip is playing and the user scrolls it out of view? The clip continues playing; audio remains audible until user pauses or clicks another clip.

## Requirements *(mandatory)*

### Functional Requirements

**Anomaly Filtering**
- **FR-001**: System MUST display an anomaly threshold selector with labeled intensity levels:
  | Std Dev Threshold | Label |
  |-------------------|-------|
  | 3+ | Warming Up |
  | 5+ | Getting Good |
  | 7+ | Popping Off |
  | 9+ | Unhinged |
  | 11+ | Legendary |
- **FR-001a**: The threshold-to-label mapping MUST be stored as a frontend constant (single JavaScript object) for easy modification.
- **FR-002**: System MUST filter displayed clips to only show those meeting or exceeding the selected anomaly threshold.
- **FR-002a**: System MUST always have a threshold selected; "no filter" or "show all" is NOT a valid option.
- **FR-002b**: System MUST default to "Getting Good" (5+ std dev) when a user first visits the page.
- **FR-003**: System MUST persist the selected filter during the browsing session (until page refresh).
- **FR-004**: System MUST show the total count of clips matching the current filter.

**Inline Playback**
- **FR-005**: System MUST play video clips inline within the scrolling grid when clicked.
- **FR-006**: System MUST NOT open a separate modal, pane, or overlay for clip playback.
- **FR-007**: System MUST stop any currently playing clip when a new clip is clicked.
- **FR-008**: System MUST display playback controls (play/pause, mute/unmute, progress) on the inline player.
- **FR-009**: System MUST autoplay the clip immediately upon click without requiring a second interaction.

**Infinite Scrolling**
- **FR-010**: System MUST load an initial batch of the most recent clips when the page loads (default: 24 clips).
- **FR-011**: System MUST automatically load additional clips when the user scrolls near the bottom of the current list.
- **FR-012**: System MUST NOT use the previous 7-day time window as the primary loading constraint.
- **FR-013**: System MUST display a loading indicator while fetching additional clips.
- **FR-014**: System MUST indicate when all available clips have been loaded.

**UI Modernization**
- **FR-015**: System MUST display the clips in a responsive grid that adapts to screen size.
- **FR-016**: System MUST show clip metadata (streamer name, relative timestamp, intensity label) on each clip card.
- **FR-017**: System MUST display smooth loading states during initial load and infinite scroll operations.

### Key Entities

- **Clip**: Represents a captured stream moment. Key attributes: unique identifier, streamer information, thumbnail image, video source, intensity (standard deviations above mean), detection timestamp.
- **Intensity**: A measure of how significant a chat activity spike was, expressed in standard deviations above the baseline mean. Stored as a float in the database and mapped to display labels (Warming Up, Getting Good, Popping Off, Unhinged, Legendary) in the frontend.
- **Filter State**: The user's current anomaly threshold selection, used to determine which clips are displayed.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can find and play a clip with a single click (reduced from current two-click flow).
- **SC-002**: Users can filter to high-anomaly clips (5+ std dev) and see results within 1 second of selection.
- **SC-003**: Initial page load displays clips within 2 seconds on standard broadband connections.
- **SC-004**: Infinite scroll loads additional clips before the user reaches the bottom of the current list (no visible waiting at scroll end).
- **SC-005**: 90% of users can successfully use the anomaly filter on first attempt without guidance.
- **SC-006**: Page layout renders correctly on screens from 320px to 2560px width.
- **SC-007**: Playing a clip does not cause page layout shifts or require scrolling to view the player.

## Clarifications

### Session 2026-01-20

- Q: What should the default anomaly filter be when a user first visits the clips page? → A: "Getting Good" (5+ std dev). "No filter" is not an available option; users must always have a threshold selected.
- Q: Where should the threshold-to-label mapping be stored? → A: Frontend constant (hardcoded JavaScript object). Simple to find and modify without infrastructure overhead.

## Assumptions

- Intensity values (standard deviation multiples) are calculated in the Flink job and stored with each clip. The `clips` table includes an `intensity FLOAT` column. The frontend maps these values to display labels.
- The initial batch size of 24 clips provides a good balance between content density and load time. This can be adjusted based on user feedback.
- "Modern UI" means: consistent spacing, clear typography hierarchy, subtle shadows/borders for depth, smooth transitions, and contemporary color palette consistent with the existing application.
- Clips continue to be sourced from Twitch and use Twitch's embed player for inline playback.
- The existing clip scrolling bar layout (horizontal grid) is preserved and enhanced rather than replaced.

## Observability

This feature adds clip-specific metrics, dashboard panels, and alerts. For generic infrastructure observability (Prometheus scrape targets, Alertmanager setup, base dashboard panels), see [001-twitch-stream-highlights/spec.md](../001-twitch-stream-highlights/spec.md#observability).

### Prometheus Metrics

The Flink clip detector job exposes the following custom Prometheus metrics on port 9250 (scraped by the `clip-detector` job):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `anomalies_detected_total` | Counter | `broadcaster_id` | Total anomalies detected per broadcaster |
| `clips_created_success_total` | Counter | `broadcaster_id` | Total clips created successfully per broadcaster |
| `clips_created_failed_total` | Counter | `broadcaster_id`, `reason` | Total clip creation failures (reasons: `api_error`, `max_retries`, `metadata_fetch`, `unknown`) |
| `clip_creation_duration_seconds` | Gauge | `broadcaster_id` | Time taken to create the most recent clip per broadcaster |

### Grafana Dashboard

Added to the StreamScout Overview dashboard (`configs/grafana/dashboards/streamscout-overview.json`):

- **Anomaly Detection & Clips**: Time series showing anomalies detected, clips created, and clip failures (5-minute windows)

### Alerts

Feature-specific alert rules added to `configs/prometheus/alert_rules.yml`:

| Alert | Condition | Severity | Description |
|-------|-----------|----------|-------------|
| `ClipDetectorMetricsDown` | down for 5m | warning | Clip detector metrics endpoint unreachable |
| `ClipCreationFailureSpike` | > 30% failure rate over 1h | warning | High clip failure rate |
| `AnomalyDetectionStalled` | no anomalies in 24h with active streams | warning | Detection may be broken |
| `NoClipsCreated` | no clips in 24h despite anomalies | warning | Twitch API or token issue |

### Logging

Key log events for this feature:

- `ANOMALY DETECTED for broadcaster {id}`: Includes count, threshold, mean, std, intensity
- `CLIP CREATION START for broadcaster {id}`: Beginning of clip creation flow
- `CLIP CREATION COMPLETE for broadcaster {id}`: Success with clip_id and duration
- `CLIP CREATION FAILED for broadcaster {id}`: Failure with error details

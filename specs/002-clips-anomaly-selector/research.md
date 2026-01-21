# Research: Clips Anomaly Selector & Enhanced Browser

**Feature**: 002-clips-anomaly-selector
**Date**: 2026-01-20

## Research Tasks Completed

### 1. Intensity Storage in Flink Job

**Question**: How to calculate and store anomaly intensity for clips?

**Decision**: Calculate intensity as Z-score and store in new `intensity` column.

**Rationale**:
- The Flink job already calculates all necessary values during anomaly detection:
  - `message_count` (window_sum) - messages in 5-second window
  - `baseline_mean` - average from 300-second baseline
  - `baseline_std` - standard deviation from baseline
- Z-score formula: `intensity = (message_count - baseline_mean) / baseline_std`
- This value directly represents "standard deviations above the mean" which matches our filter labels
- Current threshold is 3.0 std dev, so all stored clips will have intensity >= 3.0

**Alternatives Considered**:
- Store raw values (message_count, baseline_mean, baseline_std) - rejected as unnecessary complexity; Z-score is sufficient
- Store as integer bucket (3, 5, 7, etc.) - rejected as loses precision for sorting/display

**Implementation Location**:
- `services/flink-job/flink_job.py` lines 555-560 (anomaly dict creation)
- `services/flink-job/flink_job.py` lines 421-430 (insert_clip method)
- `infrastructure/postgres/init.sql` (schema update)

---

### 2. Twitch Inline Embed Playback

**Question**: How to implement inline autoplay for Twitch clips?

**Decision**: Use Twitch iframe embed with `autoplay=true` and `allow="autoplay"` attribute.

**Rationale**:
- Current embed URL format is already correct: `https://clips.twitch.tv/embed?clip={clip_id}`
- Adding `&parent={hostname}&autoplay=true` enables autoplay
- Browser requires `allow="autoplay"` iframe attribute for autoplay permission
- Twitch embed handles muted autoplay internally (browser policy compliant)
- Twitch player provides built-in controls (play/pause, mute, progress, fullscreen)

**Alternatives Considered**:
- Custom video player with direct video URL - rejected; Twitch doesn't expose direct video URLs
- Twitch Interactive Embed SDK (JavaScript API) - rejected as overkill; simple iframe sufficient for this use case

**Implementation**:
```javascript
// Inline player iframe
`<iframe
  src="${embedUrl}&parent=${hostname}&autoplay=true"
  allow="autoplay"
  allowfullscreen>
</iframe>`
```

---

### 3. Infinite Scroll Pagination

**Question**: What pagination approach for infinite scroll?

**Decision**: Offset-based pagination with `offset` and `limit` query parameters.

**Rationale**:
- Simple to implement with existing PostgreSQL setup
- Clips are ordered by `detected_at DESC` (newest first)
- No complex cursor encoding needed
- Sufficient for hobby project scale
- Filter changes reset offset to 0

**Alternatives Considered**:
- Cursor-based pagination (encode last clip ID) - rejected as unnecessary complexity for this scale
- Keyset pagination (WHERE detected_at < last_timestamp) - rejected; offset simpler for random access

**API Changes**:
- Add `offset` parameter (default: 0)
- Add `min_intensity` parameter for filtering
- Return `total_count` for "X clips matching" display
- Return `has_more` boolean for infinite scroll logic

---

### 4. Handling Existing Clips Without Intensity

**Question**: What to do with existing clips that don't have intensity values?

**Decision**: Set existing clips to `NULL` intensity, exclude from filtered results, show "score unavailable" in UI.

**Rationale**:
- Migration adds nullable `intensity` column
- Existing clips can't be backfilled (original baseline data is gone)
- New clips will have intensity populated by Flink job
- UI handles NULL gracefully per edge case in spec

**Alternatives Considered**:
- Default to 3.0 (minimum threshold) - rejected; misleading since we don't know actual value
- Delete old clips - rejected; loses historical data

---

### 5. Frontend Architecture

**Question**: Framework or vanilla JS for frontend updates?

**Decision**: Continue with vanilla JavaScript.

**Rationale**:
- Existing codebase uses vanilla JS
- Changes are contained to single page
- No complex state management needed
- Faster iteration for hobby project
- No build process required

**Alternatives Considered**:
- Add React/Vue - rejected; overkill for single page, adds build complexity
- Alpine.js - considered but unnecessary given scope

---

## Summary of Technical Decisions

| Area | Decision | Key Detail |
|------|----------|------------|
| Intensity storage | Z-score in FLOAT column | `(msg_count - mean) / std_dev` |
| Inline playback | Twitch iframe embed | `allow="autoplay"` attribute |
| Pagination | Offset-based | `offset` + `limit` params |
| Old clips | NULL intensity | Excluded from filters |
| Frontend | Vanilla JS | No framework change |

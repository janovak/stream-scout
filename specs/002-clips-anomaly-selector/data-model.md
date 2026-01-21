# Data Model: Clips Anomaly Selector

**Feature**: 002-clips-anomaly-selector
**Date**: 2026-01-20

## Entities

### Clip (modified)

Represents a captured stream highlight moment detected by anomaly in chat activity.

**Table**: `clips`

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| id | SERIAL | NO | Primary key |
| broadcaster_id | BIGINT | NO | Twitch broadcaster ID |
| clip_id | VARCHAR(255) | NO | Twitch clip ID (unique) |
| embed_url | TEXT | NO | Twitch embed URL |
| thumbnail_url | TEXT | NO | Clip thumbnail image URL |
| detected_at | TIMESTAMP | NO | When anomaly was detected |
| created_at | TIMESTAMP | NO | Row creation timestamp (default NOW()) |
| **intensity** | **FLOAT** | **YES** | **NEW: Standard deviations above mean (Z-score)** |

**Constraints**:
- `clip_id` is UNIQUE
- `intensity` is nullable to support existing clips without values

**Indexes** (existing + new):
- Primary key on `id`
- Unique index on `clip_id`
- **NEW**: Index on `(intensity, detected_at DESC)` for filtered queries

### Schema Migration

```sql
-- Add intensity column to existing clips table
ALTER TABLE clips ADD COLUMN intensity FLOAT;

-- Create index for efficient filtered queries
CREATE INDEX idx_clips_intensity_detected ON clips (intensity, detected_at DESC);
```

---

## Flink Job Data Structures

### ClipResult (modified)

Dataclass passed through Flink pipeline to clip creator.

| Field | Type | Description |
|-------|------|-------------|
| broadcaster_id | int | Twitch broadcaster ID |
| detected_at | int | Detection timestamp (milliseconds) |
| **intensity** | **float** | **NEW: Z-score value** |

**Calculation** (in AnomalyDetector):
```python
intensity = (window_sum - mean) / std_dev
```

---

## Frontend State

### Filter State (new)

Client-side state for the intensity filter selector.

| Field | Type | Description |
|-------|------|-------------|
| selectedThreshold | number | Currently selected minimum intensity (3, 5, 7, 9, or 11) |
| availableThresholds | array | `[{value: 3, label: "Warming Up"}, ...]` |

**Default**: `selectedThreshold = 5` ("Getting Good")

### Clips State (modified)

| Field | Type | Description |
|-------|------|-------------|
| clips | array | Loaded clip objects |
| offset | number | Current pagination offset |
| hasMore | boolean | Whether more clips available |
| isLoading | boolean | Loading state for infinite scroll |
| totalCount | number | Total clips matching current filter |
| playingClipId | string | Currently playing clip ID (null if none) |

---

## Intensity Labels Mapping

Frontend constant for threshold-to-label mapping.

```javascript
const INTENSITY_LEVELS = [
  { threshold: 3, label: "Warming Up" },
  { threshold: 5, label: "Getting Good" },
  { threshold: 7, label: "Popping Off" },
  { threshold: 9, label: "Unhinged" },
  { threshold: 11, label: "Legendary" }
];
```

**Usage**:
- Filter selector dropdown options
- Display label on clip cards
- Map clip's intensity value to nearest lower threshold label

---

## Relationships

```
┌─────────────┐       ┌─────────────┐
│  streamers  │──────<│    clips    │
└─────────────┘       └─────────────┘
 broadcaster_id        broadcaster_id
                       + intensity (NEW)
```

Existing JOIN on `broadcaster_id` to get `streamer_login` for display.

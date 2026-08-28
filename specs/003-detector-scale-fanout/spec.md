# Feature Specification: Detector State Cost at Fan-Out Scale

**Feature Branch**: `003-detector-scale-fanout`
**Created**: 2026-08-27
**Status**: Draft
**Input**: User description: "Scale chat ingestion and anomaly detection from 30 to 2000 concurrent broadcasters"

## Overview

StreamScout monitors 15 to 30 broadcasters today. The goal is 2000 and beyond.

A spike on 2026-08-27 proved that the *ingestion* ceiling can be removed (see
`research.md`). The remaining blocker is the *detector*. `AnomalyDetector`
reads its full bucket map from Flink keyed state once per broadcaster per
second. That is about 305 state accesses per broadcaster-second. At 30
broadcasters this costs about 9,150 state accesses per second. At 2000
broadcasters it costs about **610,000 per second**, across 4 subtasks, through
PyFlink's Python-to-Java boundary, on a 2048 MB TaskManager.

This feature removes that cost. It must not change what the detector detects.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Detector runs at 2000 broadcasters (Priority: P1)

As the operator, I want the detector to keep up when the system watches 2000
broadcasters, so that clips are still created from real chat spikes.

**Why this priority**: This is the blocking constraint. No other scaling work
delivers value while the detector cannot process the load.

**Independent Test**: Replay a corpus at 2000 synthetic keys and confirm the
job keeps its watermark current and does not fall behind.

**Acceptance Scenarios**:

1. **Given** a detector at the current bucket count, **When** state accesses
   are counted per broadcaster-second, **Then** the count is 1 or 2, not ~305.
2. **Given** 2000 active keys, **When** the job runs for 10 minutes,
   **Then** the operator watermark stays within `WATERMARK_OUT_OF_ORDERNESS`
   of the source watermark.

### User Story 2 - Detection output does not change (Priority: P1)

As the operator, I want the new detector to report exactly the anomalies the
old one reported, so that I can deploy the change without re-tuning.

**Why this priority**: Equal priority to US1. A faster detector that detects
different things is a regression, not an improvement. The tuning in
`DetectorConfig` came from corpus evidence and must stay valid.

**Independent Test**: Replay `chat-corpus.jsonl` through the old and new code
paths. Diff the anomaly streams.

**Acceptance Scenarios**:

1. **Given** `chat-corpus.jsonl`, **When** replayed through old and new code,
   **Then** the emitted anomalies are identical in broadcaster, second, count,
   mean, std, and intensity.
2. **Given** `chat-corpus-2026-08-17.jsonl`, **When** replayed through both,
   **Then** the anomaly streams are identical.
3. **Given** the existing `test_spike_detector.py` suite, **When** run against
   the new code, **Then** every test passes unmodified.

### User Story 3 - State stays bounded per key (Priority: P2)

As the operator, I want per-key state to stay bounded and to expire, so that
memory does not grow without limit at 2000 keys.

**Why this priority**: Lower than P1 because the current code already bounds
`message_counts`. The new representation must not lose that property, and TTL
behaviour must be preserved.

**Independent Test**: Run a key to silence and confirm its state is removed
within the configured TTL.

**Acceptance Scenarios**:

1. **Given** a broadcaster that stops chatting, **When** `retained_seconds`
   passes, **Then** its detector state is empty or expired.
2. **Given** the TTL configuration, **When** the new state is used,
   **Then** no bucket that the baseline still needs is deleted early.

### Edge Cases

- A late message arrives for a bucket the detector already counted. The new
  representation must accept the update, exactly as `MapState.put` does today.
- A message arrives for a bucket newer than the evaluated second. The detector
  must still exclude it from the current evaluation, and must still register
  its own timer.
- A watermark jump moves the cursor by more than one second. Bucket eviction
  and timer chaining must behave as they do today.
- A key restarts after silence. The warm-up gate must still measure observed
  time from the oldest live bucket.
- Two buckets tie during eviction. Eviction order must stay sorted, so that
  replay output does not depend on map iteration order.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The detector MUST reduce **timer-path** keyed-state accesses per
  broadcaster-second from ~305 to a small constant, without raising total
  measured cost. See SC-001a and SC-001b.
- **FR-002**: The detector MUST emit the same anomalies as the current code for
  the same input. "Same" means identical broadcaster, peak second, message
  count, baseline mean, baseline standard deviation, and intensity.
- **FR-003**: The detector MUST keep treating an absent bucket as a count of
  zero.
- **FR-004**: The detector MUST keep the two-pass mean and sample standard
  deviation, or MUST use a method proven exact against it. See
  `_mean_and_sample_stdev`, which rejects sum-of-squares for precision.
- **FR-005**: The detector MUST keep evicting buckets older than
  `baseline_start`, in sorted order.
- **FR-006**: The detector MUST keep the peak-hold, cooldown, warm-up gate, and
  hold-regression guard behaviours unchanged.
- **FR-007**: Per-key state MUST stay bounded and MUST keep a TTL that is
  longer than `retained_seconds`.
- **FR-008**: `tools/replay.py` MUST stay a faithful mirror of the operator, so
  that replay remains valid evidence.

### Key Entities

- **Bucket**: one event-time second, holding a message count for one
  broadcaster.
- **Window**: the most recent `window_seconds` buckets.
- **Baseline**: the `baseline_seconds` buckets before the window.
- **Hold**: an open elevated period, carrying the peak intensity and its
  second.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: **Timer-path** keyed-state accesses per broadcaster-second drop
  from ~305 to at most 2.
- **SC-001a**: **Message-path** keyed-state accesses stay at no more than one
  per message. See `plan.md` C1: the design trades a per-message
  read-modify-write for the removal of the per-second scan.
- **SC-001b**: Measured **total** cost — timer path plus message path together
  — is lower than the current total at the message rates in `research.md`. A
  win on the timer path alone does not satisfy this feature.
- **SC-002**: Replay of both corpus files produces byte-identical anomaly
  streams before and after the change.
- **SC-003**: `test_spike_detector.py` and `test_replay.py` pass without
  modification to their assertions.
- **SC-004**: At 2000 keys, the job holds its watermark for 10 minutes without
  falling behind.
- **SC-005**: TaskManager heap stays within the 2048 MB budget at 2000 keys.

## Out of Scope

These are real and known. They are separate features.

- Migrating chat ingestion from IRC to EventSub. `research.md` records the
  evidence. That is a later feature.
- The unbounded thread spawn in `ClipCreator`, and the absence of a global clip
  budget. At 2000 broadcasters the system will detect far more anomalies than
  Twitch permits clips. That needs anomaly ranking, not just a limiter.
- Kafka partition count and Flink parallelism re-provisioning.
- The delivery-lag tail measured in `research.md`, which exceeds the current
  1-second watermark tolerance.

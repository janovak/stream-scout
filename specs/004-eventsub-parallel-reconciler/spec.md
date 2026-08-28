# Feature Specification: EventSub Ingestion with a Parallel Reconciler

**Feature Branch**: `004-eventsub-parallel-reconciler`
**Created**: 2026-08-27
**Status**: Draft
**Input**: "Migrate chat ingestion to EventSub with an out-of-poll parallel subscription reconciler"

## Overview

Chat ingestion runs over IRC today. The JOIN rate limit is **20 per 10 seconds
per Twitch account**, and it blocks rather than fails. That is a hard ceiling on
how many channels the system can watch, and no amount of extra connections or
extra machines moves it.

A spike (recorded in `specs/003-detector-scale-fanout/research.md` §1) proved
EventSub `channel.chat.message` removes that ceiling: arbitrary channels work
with a plain user token, subscriptions cost **zero** against the budget, and
394 ran concurrently across 2 sockets.

**But the transport swap alone buys almost nothing for cold start.** Measured
sequential EventSub subscription creation is **2.1/s** — effectively identical
to IRC's 2/s. The difference is that EventSub creation is *latency-bound* (p50
421 ms per POST, zero 429s) and therefore parallelizes, while IRC's bucket
blocks no matter how joins are issued.

So this feature is two changes that only deliver value together:

1. **Transport**: IRC → EventSub, removing the ceiling.
2. **Structure**: subscription management moves out of the poll job into a
   long-lived parallel reconciler, so cold start is not bounded by a scheduler
   tick.

Doing (1) without (2) leaves the cold-start problem exactly where it is.

## Scope decision: no incremental compatibility

The operator does not need a working system at each intermediate step. So the
IRC path is **removed**, not kept behind a flag. No dual-transport period, no
migration dance. This is a deliberate simplification and it removes most of the
complexity this feature would otherwise carry.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Cold start is fast at large channel counts (Priority: P1)

As the operator, I want the system to reach its full monitored set quickly from
a cold start, so that raising the channel count does not mean waiting minutes
with partial coverage.

**Why this priority**: This is the point of the feature. Without it the
transport swap is cosmetic.

**Independent Test**: Start from zero subscriptions with a target of 500
channels and measure time to full coverage.

**Acceptance Scenarios**:

1. **Given** a cold start targeting 500 channels, **When** the reconciler runs,
   **Then** full coverage is reached in well under the IRC equivalent (~250 s),
   with a target of under 60 s.
2. **Given** subscription creation is in progress, **When** a poll tick occurs,
   **Then** the poll completes normally and is not blocked or skipped.

### User Story 2 - The poll job never blocks on network work (Priority: P1)

As the operator, I want the poller to stay fast regardless of how many channels
change, so that Redis online keys keep being refreshed and streamers do not
flap offline.

**Why this priority**: Equal to US1. This is the failure mode that breaks the
current design at ~240 channels, and it is transport-independent.

**Independent Test**: Force a large desired-set change and confirm poll duration
stays flat.

**Acceptance Scenarios**:

1. **Given** a desired-set change of 500 channels, **When** the poller runs,
   **Then** its duration is unchanged from a zero-change poll.
2. **Given** a reconciler that is stalled or dead, **When** polls continue,
   **Then** polls still succeed and the condition is visible in metrics.

### User Story 3 - Chat data keeps its meaning (Priority: P1)

As the operator, I want the anomaly detector to behave the same after the
transport change, so that the tuning derived from corpus evidence stays valid.

**Why this priority**: A transport swap that silently changes event-time
semantics would corrupt detection without failing loudly.

**Independent Test**: Compare the Kafka message stream shape and timestamp
distribution before and after.

**Acceptance Scenarios**:

1. **Given** an EventSub message, **When** it is published to Kafka, **Then**
   its schema matches what the Flink job consumes today.
2. **Given** measured delivery lag, **When** the watermark tolerance is set,
   **Then** late-event drops stay below an agreed threshold.

### User Story 4 - Subscriptions survive restarts and drift (Priority: P2)

As the operator, I want the reconciler to converge from any starting state, so
that restarts, partial failures, and Twitch-side revocations self-heal.

**Independent Test**: Kill the service mid-ramp, restart, confirm convergence.

**Acceptance Scenarios**:

1. **Given** a restart with subscriptions already live, **When** the reconciler
   starts, **Then** it adopts them rather than duplicating them.
2. **Given** a revoked subscription, **When** the next reconcile runs,
   **Then** it is recreated.

### Edge Cases

- A channel refuses subscription with `subscription missing proper
  authorization`. Measured at ~1.5% of channels (6 of 400). These must be
  remembered and skipped, like clipping-disabled streamers already are, not
  retried every cycle.
- A websocket connection dies holding up to 300 subscriptions.
- Subscriptions linger as `websocket_disconnected` after a socket closes, and
  `DELETE` on them returns "not found".
- Events arrive for a subscription the library has not yet registered — the
  library logs `received event for unknown subscription with ID ...` during a
  ramp, and drops them.
- The desired set changes while a reconcile is in flight.
- A channel appears in the desired set, then leaves before its subscription
  completes.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Chat ingestion MUST use EventSub `channel.chat.message`. The IRC
  `Chat` client MUST be removed.
- **FR-002**: Subscription creation and deletion MUST run outside the poll job.
- **FR-003**: The poller MUST only compute the desired set and publish it. Its
  duration MUST NOT scale with the size of the change.
- **FR-004**: The reconciler MUST create subscriptions concurrently, with a
  bounded concurrency limit.
- **FR-005**: The reconciler MUST converge from any starting state, adopting
  existing subscriptions rather than duplicating them.
- **FR-006**: The system MUST distribute subscriptions across websocket
  connections, respecting the measured **300 per connection** cap.
- **FR-007**: Channels that refuse authorization MUST be recorded and skipped
  on later cycles.
- **FR-008**: Published Kafka messages MUST keep the schema the Flink job
  consumes today, including a `sent_at` field.
- **FR-009**: The event-time source for `sent_at` MUST be defined explicitly,
  given that `ChannelChatMessageData` carries no timestamp and the envelope's
  `metadata.message_timestamp` is a dispatch time, not IRC's `tmi-sent-ts`.
- **FR-010**: The watermark out-of-orderness tolerance MUST be set from
  measured EventSub delivery lag, not inherited from the IRC value.
- **FR-011**: Hysteresis (join at `JOIN_THRESHOLD`, leave at `LEAVE_THRESHOLD`)
  MUST be preserved.
- **FR-012**: Prometheus metrics MUST cover subscription count, reconcile
  duration, creation failures, and per-connection utilisation.

### Key Entities

- **Desired set**: the channels the poller says should be monitored, with rank.
- **Actual set**: the subscriptions that currently exist.
- **Reconciler**: the component driving actual toward desired.
- **Connection pool**: websocket connections, each holding up to 300 subs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Cold start to 500 channels completes in under 60 s (IRC baseline
  ~250 s).
- **SC-002**: Poll duration does not scale with desired-set change size.
- **SC-003**: No `Bucket channel_join got rate limited` — the IRC bucket is
  gone entirely.
- **SC-004**: Sustained 500 channels with subscription count stable and no
  unexplained drift.
- **SC-005**: Late-event drop rate at the chosen watermark tolerance stays
  below an agreed threshold.
- **SC-006**: A restart mid-ramp converges without duplicate subscriptions.

## Out of Scope

- **`ClipCreator`'s unbounded thread spawn and the absence of a clip budget.**
  This is real and will bite at scale — clip creation is capped per account
  much as JOIN was, so a larger monitored set will detect far more anomalies
  than can be acted on. It needs anomaly *ranking* against a scarce budget,
  which is a design change. Candidate for 005.
- The detector state work in 003, which measurement deflated.
- Kafka partition and Flink parallelism re-provisioning.
- Webhook transport, unless websocket proves insufficient (see `plan.md`).

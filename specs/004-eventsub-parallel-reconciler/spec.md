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

## Clarifications

### Session 2026-08-27

- Q: What is the acceptable late-event drop rate at the chosen watermark? → A:
  Set `WATERMARK_OUT_OF_ORDERNESS_SECONDS` to 2 (from 1). A 2 s tolerance
  clears the measured 1243 ms maximum delivery lag at 394 channels with margin.
  Record the residual late-drop rate. It MUST stay below 0.1%.
- Q: Is the 60 s cold-start figure a hard gate or a target? → A: 60 s is the
  target. 120 s is the hard ceiling and the fail line. The primary bar is to
  beat the ~250 s IRC baseline by a wide margin.
- Q: How does an authorization refusal leave the skip cache? → A: A 7-day
  re-check. Store the time of the refusal. Retry a refusal once it is more than
  7 days old. Apply the same 7-day re-check to the existing `allows_clipping`
  skip, so both conditions self-heal.
- Q: What is the event-time source for `sent_at`? → A: The envelope field
  `metadata.message_timestamp` (dispatch time). Cut over only if Phase 0 task
  T001 shows the median dispatch-versus-receive offset is within the 2 s
  watermark tolerance.
- Q: Is a ramp-window warm-up gate required? → A: Only if Phase 0 task T004
  shows the "received event for unknown subscription" loss is large enough to
  distort detection. If the loss is small, accept it.

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
   **Then** full coverage — every channel that does not refuse authorization —
   is reached in under 60 s (target) and never more than 120 s (hard ceiling),
   against the IRC equivalent of ~250 s.
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
   the JSON has the same keys and the same value types as an IRC-sourced
   message today — in particular `sent_at` is an integer or `null`, never a
   string (`contracts/chat-messages.schema.md`).
2. **Given** the watermark tolerance is set to 2 s, **When** the late rate is
   measured on live traffic, **Then** it stays below 0.1%. See SC-005 for what
   "late" means for this job and for the channel count it was taken at.

### User Story 4 - Subscriptions survive restarts and drift (Priority: P2)

As the operator, I want the reconciler to converge from any starting state, so
that restarts, partial failures, and Twitch-side revocations self-heal.

**Independent Test**: Kill the service mid-ramp, restart, confirm convergence.

**Acceptance Scenarios**:

1. **Given** a restart with subscriptions already live, **When** the reconciler
   starts, **Then** it adopts them rather than duplicating them.
2. **Given** a revoked subscription, **When** the next reconcile runs,
   **Then** it is recreated.
3. **Given** a channel marked as authorization-refused more than 7 days ago,
   **When** the next cycle runs, **Then** the subscription is attempted once
   more, and a success clears the mark.
4. **Given** a streamer marked as clipping-disabled more than 7 days ago,
   **When** the next poll runs, **Then** the streamer is back in the ranking,
   and the next real clip attempt either clears the mark or sets it again
   (FR-013).

### Edge Cases

- A channel refuses subscription with `subscription missing proper
  authorization`. The 003 spike measured ~1.5% of channels (6 of 400); the
  Phase 5 run at a real 500 saw 1 (0.2%). Record the refusal
  with its time and skip the channel, like clipping-disabled streamers already
  are. Retry the refusal only once it is more than 7 days old.
- A websocket connection dies holding up to 300 subscriptions.
- Subscriptions linger as `websocket_disconnected` after a socket closes, and
  `DELETE` on them returns "not found".
- Events arrive for a subscription the library has not yet registered — the
  library logs `received event for unknown subscription with ID ...` during a
  ramp, and drops them. Phase 0 task T004 measures this loss. A warm-up gate is
  added only if the loss is large enough to distort detection.
- The desired set changes while a reconcile is in flight.
- A channel appears in the desired set, then leaves before its subscription
  completes.
- Subscription creation returns 429. The reconciler backs off and lowers
  concurrency; it does not drop the channel (see `research.md` D2/R3).
- An envelope arrives with `metadata.message_timestamp` missing or unparseable.
  The message still publishes, with `sent_at` set to `null` so the Flink
  assigner falls back to record time (see `contracts/chat-messages.schema.md`).
- The desired set is larger than the current pool can hold. The reconciler
  grows the pool before it reports the channels uncovered.

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
  existing subscriptions rather than duplicating them. The diff MUST resolve in
  one reconcile cycle; full coverage from a cold start MUST land inside the
  SC-001 bound.
- **FR-006**: The system MUST distribute subscriptions across websocket
  connections, respecting the measured **300 per connection** cap and Twitch's
  limit of **3 websocket connections** with enabled subscriptions per
  client-id/user-id pair. The two together put a hard ceiling of **900
  channels** on this transport; the system MUST refuse to open a fourth
  connection rather than let Twitch refuse the subscriptions on it
  (added 2026-08-29 — see `research.md` D1). The pool
  MUST start with no connections and grow on demand when the desired set needs
  more than the open connections can hold. Routing MUST be stable across
  reconciles and across restarts, and growing the pool MUST NOT move a channel
  that is already placed.
- **FR-007**: Channels that refuse authorization MUST be recorded with the time
  of the refusal and skipped on later cycles. A refusal MUST be retried once it
  is more than 7 days old. The refusal record MUST NOT be written when the
  token has no `user:read:chat` scope. Without that scope every channel refuses
  for one reason that is not the broadcaster's, so persisting the refusals
  would turn a token mistake into a 7-day skip of the whole monitored set
  (added during implementation, task T025).
- **FR-008**: Published Kafka messages MUST keep the schema the Flink job
  consumes today, including a `sent_at` field.
- **FR-009**: The event-time source for `sent_at` MUST be the envelope field
  `metadata.message_timestamp` (dispatch time), because `ChannelChatMessageData`
  carries no timestamp. Cutover MUST proceed only if Phase 0 task T001 shows the
  median dispatch-versus-receive offset is within the 2 s watermark tolerance.
- **FR-010**: `WATERMARK_OUT_OF_ORDERNESS_SECONDS` MUST be set to 2, replacing
  the IRC-era value of 1. The residual late rate MUST be measured on live
  traffic and MUST stay below 0.1%. **Corrected during implementation**: the
  measurement is taken at the production operating point, not at 500, and the
  quantity is not a Flink drop counter. See SC-005.
- **FR-011**: Hysteresis (join at `JOIN_THRESHOLD`, leave at `LEAVE_THRESHOLD`)
  MUST be preserved.
- **FR-012**: Prometheus metrics MUST cover subscription count, reconcile
  duration, creation failures, per-connection occupancy, and the time of the
  last successful reconcile (so a stalled reconciler is visible while polls
  keep succeeding).
- **FR-013**: The existing `allows_clipping` skip MUST also self-heal on the
  same 7-day re-check, so a streamer that re-enables clipping is returned to
  ranking rather than skipped forever. **Clarified during implementation**: for
  `allows_clipping` this is an un-skip, not a bounded single retry. The
  reconciler does not call Twitch to test the flag. The streamer re-enters the
  ranking, and the next real clip attempt in the Flink job either clears the
  mark or sets it again, exactly as `data-model.md` specifies. A streamer who
  never spikes therefore holds a slot until they do.
- **FR-014**: A per-channel warm-up gate that withholds a channel's baseline
  until its subscription is settled MUST be added only if Phase 0 task T004
  shows the ramp-window event loss exceeds ~1% of a channel's first-window
  messages, or shifts a channel's opening baseline mean by more than the
  detector's own noise. "Settled" means the subscription has reached `enabled`
  and one message has been received on it.

### Non-Functional Requirements

- **NFR-001**: The reconciler's steady-state resource use MUST be bounded. It
  MUST NOT spawn one task or thread per channel. Concurrency is capped by
  FR-004's limit, and idle channels cost no ongoing work. (This is the
  discipline `ClipCreator` lacks — see `research.md` §out-of-scope.)
- **NFR-002**: A Redis or Postgres outage during a reconcile MUST degrade
  gracefully: the reconciler logs, skips the affected work, and retries on the
  next cycle. It MUST NOT crash the process or drop live subscriptions.
- **NFR-003**: Reconciler start-up adoption (FR-005) MUST tolerate a partial
  enumeration: if `get_eventsub_subscriptions` pagination fails mid-list, the
  reconciler MUST NOT treat unseen subscriptions as absent and recreate them.

### Key Entities

- **Desired set**: the channels the poller says should be monitored, with rank,
  plus the login-to-broadcaster-id map that goes with them. The poller ranks by
  login and EventSub subscribes by id, so the map has to cross the seam. Both
  are written in one transaction, so they can never disagree
  (`chat:desired` and `chat:desired:ids` in `data-model.md`).
- **Actual set**: the subscriptions that currently exist.
- **Reconciler**: the component driving actual toward desired.
- **Connection pool**: websocket connections, each holding up to 300 subs.
- **Skip record**: a streamer marked unusable for a reason (clipping disabled,
  or EventSub authorization refused), with the time the mark was set. A mark
  older than 7 days is retried.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Cold start to 500 channels completes in under 60 s (target) and
  in no more than 120 s (hard ceiling), against an IRC baseline of ~250 s.
  "Complete" means every channel that does not refuse authorization, on the
  same allowance SC-004 makes.
- **SC-002**: Poll duration does not scale with desired-set change size.
- **SC-003**: No `Bucket channel_join got rate limited` — the IRC bucket is
  gone entirely.
- **SC-004**: Sustained 500 channels for at least 30 minutes with the
  subscription count within ±1% of the desired-set size. Any gap is
  attributable to a logged refusal, a socket reconnect in progress, or a
  desired-set change — nothing unexplained.
- **SC-005**: With `WATERMARK_OUT_OF_ORDERNESS_SECONDS` at 2, fewer than 0.1%
  of `chat-messages` records arrive after the timer for their own bucket has
  fired.
  **Corrected during implementation.** This criterion first named Flink's
  `numLateRecordsDropped` on the source. That metric does not exist for this
  job. Flink emits it from window operators, and `clip_detector_job.py` is a
  `KeyedProcessFunction` with per-second timers. A late record is therefore not
  dropped. It re-fires a second that the job has already evaluated, and it can
  regress an open hold. The rate above is the equivalent quantity, measured
  directly on the live topic (`research.md` T038).
  **This is also the one criterion measured at the production operating point
  of 21-23 broadcasters, not at 500.** A 500-channel figure needs Flink and
  `ClipCreator` in the path. That is the change this spec defers to 005. See
  `research.md` "What was NOT measured at 500" for the gap and for the argument
  that closes it.
- **SC-006**: A restart mid-ramp converges without duplicate subscriptions.
- **SC-007**: Every metric FR-012 lists is present on the `/metrics` endpoint
  and populated with a non-placeholder value while the reconciler runs.

## Assumptions & Dependencies

- **PR #41** (configurable channel thresholds, `resolve_thresholds`) is merged
  and on this branch. The hysteresis band feeds the desired-set computation.
- **Token scope**: EventSub needs `user:read:chat`, which the current token
  lacks. A superset token already exists from the spike. Re-seeding the token
  does **not** invalidate the running production token — verified during the
  spike with production running throughout (`research.md`).
- **Measured to 394, then measured at 500 — except SC-005.** The
  300-per-connection cap, the 0 cost against `max_total_cost`, and the absence
  of a per-user subscription ceiling were first measured at 394 channels. Phase
  5 then measured SC-001, SC-002, SC-004 and SC-006 at a real 500 through a
  synthetic driver running the shipped `Reconciler` and `EventSubPoolTransport`
  against Twitch, so those assumptions are now results. **What stays an
  assumption is SC-005 alone**: the late rate is measured at 21-23
  broadcasters, and the claim that it holds at 500 rests on three delivery-lag
  measurements at three scales that do not grow with channel count. That is an
  argument, not a measurement. See `research.md` "What was NOT measured at
  500".
- **Cutover coverage gap**: the operator accepts a bounded loss of chat
  coverage during the IRC→EventSub switch. There is no dual-transport period
  (`research.md` R5). `git revert` of the branch is the fallback.
- The `chat-messages` Kafka topic keeps 4 partitions and `FLINK_PARALLELISM`
  stays 4. Re-provisioning either is out of scope.

## Out of Scope

- **`ClipCreator`'s unbounded thread spawn and the absence of a clip budget —
  this is now the next ceiling, and it is spec 005.** Not a candidate: this
  feature removed the ingestion ceiling and put the next one directly in front
  of the operator.

  The numbers say where it lands. Measured anomaly density is roughly **2
  anomalies per broadcaster-hour** — 2.09 in the T039 pre-cutover replay, and
  2.4 to 2.7 across the 17 broadcasters of the Phase 2 live gate
  (`research.md` T039 and "Detection is unchanged on EventSub data"). At the
  15/30 thresholds production runs today, ~20 channels give ~45 anomalies an
  hour, which the current design absorbs. **At the 500 channels this feature
  now reaches in 51 s, the same density gives roughly 1,100 anomalies an
  hour**, about 18 a minute.

  Two properties of `ClipCreator` turn that into a failure
  (`specs/003-detector-scale-fanout/research.md` §3):

  - **Unbounded threads.** `ClipCreator.process_element` starts a raw
    `threading.Thread` per anomaly, with no global limiter. Each one can live
    "the better part of half an hour" by its own comment, because it waits out
    `ClipPolicy`'s initial delay plus the clip and metadata retries. At 18
    anomalies a minute the live thread count runs into the hundreds. This is
    exactly the discipline NFR-001 imposed on the reconciler and that
    `ClipCreator` does not have.
  - **A per-account clip cap.** Clip creation is capped per Twitch account,
    much as JOIN was. A 500-channel monitored set detects far more anomalies
    than one account may act on, so the excess cannot be absorbed by retrying:
    429 is currently marked retryable, which converts the cap into permanent
    backoff held open by those same threads.

  So the fix is not a thread-pool size. It is **ranking anomalies against a
  scarce clip budget** — deciding which of the 1,100 are worth the few hundred
  clips available — and that is a design change with its own spec. **Spec 005
  starts here.** Until it lands, raising `JOIN_THRESHOLD` / `LEAVE_THRESHOLD`
  toward 500 is what makes the failure appear, and a rising clip-creation
  failure rate under that ramp is the expected signal, not a regression in this
  feature.
- The detector state work in 003, which measurement deflated.
- Kafka partition and Flink parallelism re-provisioning.
- Webhook transport, which is **required past 900 channels**, not optional.
  Twitch allows 3 websocket connections of 300 enabled subscriptions each per
  client-id/user-id pair, so 900 is this transport's hard ceiling — see
  `research.md` D1, corrected 2026-08-29. Every number in this spec sits below
  it (500 needs 2 sockets), so it does not affect the feature as delivered; it
  is the wall the ramp meets after spec 005's clip budget.

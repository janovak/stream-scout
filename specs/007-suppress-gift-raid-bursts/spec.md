# Feature Specification: Suppress Gift and Raid Chat Bursts

**Feature Branch**: `007-suppress-gift-raid-bursts`
**Created**: 2026-09-03
**Status**: Clarified and planned — amended 2026-09-04 by an artifact remediation
pass that added NFR-007 and SC-011, bounded the auxiliary-coverage exception in
FR-001/NFR-003/SC-001, and defined delivery-lag semantics in NFR-005/SC-010
(see [autonomous-decisions.md](./autonomous-decisions.md) §17-§21), then by an
implementation-review correction that made windows notice-bounded and added a
fixed 30-second future-time trust bound (§22-§23)
**Input**: Suppress gift- and raid-driven chat bursts from clip emission while
preserving complete chat counting, dual notification coverage for every
monitored channel, and safe operation within existing account capacity.

## Overview

Stream Scout identifies highlight-worthy moments from unusual increases in chat
activity. Two predictable events can create large but uninteresting increases:
viewers thanking a broadcaster after gifted subscriptions and viewers greeting
a channel after a raid. Those bursts currently look like highlight candidates
and can produce low-value clips.

This feature gives every monitored channel both ordinary chat-message coverage
and chat-notification coverage. Relevant gift or raid notifications start a
short suppression window for that channel. During that window, chat continues
to contribute fully to message counts, rolling baselines, and other
message-derived detector state, but a spike cannot emit a clip. This preserves
future detector accuracy while preventing known false-positive clips.

Adding a second subscription for every monitored channel doubles subscription
use. The existing account allowance is three sessions of 300 subscriptions
each. The monitored-set ceiling therefore changes from the current 800/900
ramp to a firm 400 channels, with effective join and leave thresholds both set
to 400. Four hundred channels consume 800 subscriptions and retain 100
subscription slots as reconnect and adoption safety headroom.

## Clarifications

### Session 2026-09-03

- Q: What suppression-window values and policy apply to gift and raid notices? → A: Operator-configured windows default to 120 seconds for gifts and 180 seconds for raids; raid viewer count does not affect duration.
- Q: Which notice types trigger suppression, and is overlapping genuine hype an accepted false negative? → A: Only `community_sub_gift`, `sub_gift`, and `raid` trigger suppression; `unraid`, plain `sub`, and `resub` are excluded, and genuine hype whose spike peak falls at or after the notice and before the active deadline is an accepted false negative. A spike that peaked before the notice is not an overlap and remains eligible even if its hold reports later.

### Session 2026-09-04

- Q: When notification coverage is absent or lagging, or the suppression topic is delayed, how should clip detection behave? → A: Fail open by continuing normal clip eligibility, expose the degraded condition operationally, and never retroactively retract an emitted clip.
- Q: What operator visibility is required when an otherwise qualifying spike is suppressed? → A: Every suppressed would-have-clipped spike emits both an operator-visible metric and a structured log; it does not create a clip.
- Q: What final monitored-set capacity and ramp thresholds apply? → A: The locked firm maximum is 400 monitored channels, with effective join and leave thresholds both set to 400; this feature does not revisit the existing session-capacity assumptions.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Avoid low-value gift and raid clips (Priority: P1)

As a highlight viewer, I want predictable thank-you and greeting floods to be
excluded from clip creation so that the clip feed emphasizes genuine,
interesting stream moments.

**Why this priority**: Preventing these false-positive clips is the feature's
primary user value.

**Independent Test**: Replay chat and qualifying gift/raid notifications for
one channel, including spikes inside and outside active suppression windows,
and verify which spikes emit clips.

**Acceptance Scenarios**:

1. **Given** a relevant gift or raid notification for a monitored channel,
   **When** a chat spike would otherwise emit a clip during that channel's
   suppression window, **Then** no clip is emitted and that suppressed
   would-have-clipped spike emits both an operator-visible metric and a
   structured log.
2. **Given** the same chat sequence without an active suppression window,
   **When** the sequence meets the existing clip threshold, **Then** the clip
   remains eligible for emission.
   A spike whose peak is before a later notice likewise remains eligible even
   if its hold reports after that notice arrives.
3. **Given** an unrelated chat notification, **When** a qualifying chat spike
   occurs, **Then** the unrelated notification does not suppress the clip.
4. **Given** an active gift or raid suppression window overlaps a genuine hype
   moment, **When** its spike peak is at or after the notice occurrence and
   strictly before the active deadline and it would otherwise emit a clip,
   **Then** no clip is emitted; this is an accepted false negative.
5. **Given** notification coverage or suppression delivery is absent or
   lagging and no suppression signal has established an active window,
   **When** a spike qualifies, **Then** normal clip eligibility continues and
   the degraded condition is operationally visible.
6. **Given** a qualifying clip has already been emitted, **When** its relevant
   suppression signal arrives late, **Then** the emitted clip is not
   retroactively retracted.

---

### User Story 2 - Preserve detector learning during suppression (Priority: P1)

As an operator, I want every chat message to remain part of the channel's
rolling activity history during suppression so that avoiding one false-positive
clip does not distort later anomaly decisions.

**Why this priority**: Discarding messages would lower or otherwise corrupt the
rolling baseline and create further false positives after suppression ends.

**Independent Test**: Process identical message sequences with and without
emission gating and compare message counts, rolling baseline, and all
message-derived detection state after the window.

**Acceptance Scenarios**:

1. **Given** an active suppression window, **When** chat messages arrive,
   **Then** every message contributes to the same counts and rolling baseline
   it would have contributed without suppression.
2. **Given** a suppressed would-have-clipped spike, **When** the suppression
   window ends, **Then** subsequent decisions use state that includes all
   messages from the suppressed period.
3. **Given** a message outside an active suppression window, **When** it
   contributes to a qualifying spike, **Then** normal clip eligibility applies.

---

### User Story 3 - Maintain complete, capacity-safe channel coverage (Priority: P1)

As an operator, I want every monitored channel to have both chat-message and
chat-notification coverage without exhausting subscription capacity, so that
suppression is consistently available and reconnects can recover safely.

**Why this priority**: Partial coverage would make suppression unpredictable,
while using all available subscription slots would remove the safety margin
needed for adoption and reconnect operations.

**Independent Test**: Audit a desired set of 400 channels and verify two
distinct active coverage types per channel, no deliberate partial rollout, no
more than 800 steady-state subscriptions, and refusal of a 401st monitored
channel.

**Acceptance Scenarios**:

1. **Given** any channel in the monitored set, **When** coverage converges,
   **Then** that channel has both chat-message and chat-notification coverage.
2. **Given** 400 monitored channels, **When** coverage is complete, **Then**
   exactly 800 channel subscriptions are in use and 100 of the 900 allowed
   slots remain available for safe adoption and reconnect operations.
3. **Given** 400 monitored channels and another eligible channel, **When** the
   system evaluates adding or retaining that channel under the effective
   400/400 join and leave thresholds, **Then** the monitored set remains at 400
   or fewer and the additional channel is not admitted until capacity is
   available.
4. **Given** only one coverage type exists for a monitored channel, **When**
   coverage is reconciled, **Then** the missing type is restored without
   duplicating the existing type.
5. **Given** the provider refuses chat-notification coverage for a channel
   whose chat-message coverage is live, **When** reconciliation continues,
   **Then** chat coverage is preserved, the channel is reported as
   covered-but-degraded rather than as complete, further notification attempts
   for that channel are held off for a bounded period, and the channel becomes
   repairable again once that period expires or the channel's connection is
   reconnected or retired.

---

### User Story 4 - Extend overlapping suppression predictably (Priority: P2)

As an operator, I want closely spaced gift and raid notifications to extend a
channel's suppression period deterministically so that a later related burst is
not exposed by an earlier window ending.

**Why this priority**: Multiple notices can generate one sustained chat burst;
ending at the first deadline would reintroduce the false-positive behavior.

**Independent Test**: Apply notices before, at, and after an existing deadline
and verify the resulting half-open interval: overlapping extensions retain the
earliest start and later deadline, earlier/equal candidates change no state,
and notices at or after the deadline start a new interval at their occurrence.

**Acceptance Scenarios**:

1. **Given** an active suppression interval, **When** another relevant notice
   occurs before its deadline and its occurrence time plus applicable window
   is later, **Then** the deadline extends to that later time and the interval
   start remains the earliest start in the overlapping chain.
2. **Given** an active suppression deadline, **When** another relevant notice
   would produce an earlier or equal deadline, **Then** the complete existing
   suppression state remains unchanged.
3. **Given** gift and raid notices overlap, **When** their applicable window
   lengths differ, **Then** the channel remains suppressed until the later
   candidate deadline using the operator-configured gift and raid durations.
4. **Given** a prior suppression interval, **When** another relevant notice
   occurs at or after its deadline, **Then** a new half-open interval begins at
   the new notice occurrence rather than extending the old interval backward.

### Edge Cases

- A notification occurs exactly at the current suppression deadline. Because
  the old interval is half-open, it starts a new interval at that occurrence.
- A spike peaks before a relevant notice but its hold reports after the notice
  arrives. The peak remains outside the notice-bounded interval and is eligible
  for normal clip emission; this is not the accepted overlap false negative.
- A spike peaks exactly at the notice occurrence and is inside the interval. A
  spike peaks exactly at the suppression deadline and is outside it.
- A relevant notification arrives late, after a spike has already been
  evaluated. Clip eligibility before receipt remains unchanged, an already
  emitted clip is never retracted, and any unexpired deadline established by
  the late signal applies only to subsequent decisions.
- Chat-message coverage exists while chat-notification coverage is absent, or
  vice versa. The system treats the channel as partially covered and attempts
  to restore the missing coverage without duplicating the existing coverage.
- Chat-notification coverage is refused for a channel whose chat coverage is
  live. Chat is preserved and never evicted, the channel is reported as
  covered-but-degraded so reconciliation does not retry in a tight loop, and
  the refusal expires after a bounded period — or immediately on connection
  reconnect or retirement — after which the channel is partially covered again
  and ordinary repair resumes. Successful creation or adoption of the missing
  coverage clears the degraded state.
- No relevant notice occurs for an extended period, so the suppression signal
  path is legitimately silent. Delivery health is reported as idle/unknown for
  that observation window rather than as healthy or as lagging, and complete
  coverage together with silence is not treated as proof that delivery is
  working.
- A notification is duplicated. Applying it again cannot shorten the active
  deadline or create a second independent window for the same occurrence.
- Different relevant notice types occur at the same time. Their candidate
  deadlines are evaluated independently and the latest deadline wins.
- An unrelated notification occurs during an active window. It neither starts
  nor extends suppression.
- A genuine hype moment overlaps an active gift or raid window. Its otherwise
  qualifying spike is suppressed as an accepted false negative, emits both
  required operator signals, and does not create a clip.
- A channel leaves the monitored set while suppressed. Its coverage and
  channel-specific suppression state no longer justify a monitored slot.
- A channel later re-enters the monitored set. A previous, already-expired
  suppression window does not suppress new activity.
- A 401st channel qualifies while 400 are monitored. It is not admitted by
  exceeding the firm ceiling.
- A relevant notification lacks a trustworthy channel identity or occurrence
  time. It cannot create a guessed suppression deadline; the malformed input
  is made operationally visible.
- A decoded notice claims an occurrence time at most 30 seconds ahead of the
  consumer receipt clock. It is accepted, its delivery age is clamped to zero,
  and the existing clock-skew diagnostic is emitted. A notice even one
  millisecond farther ahead is rejected as malformed fields, counted and
  logged, and produces no delivery observation or suppression-state write.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every channel in the monitored set MUST have both ordinary
  chat-message coverage and chat-notification coverage. The feature MUST NOT
  intentionally provide notification coverage to only a subset of monitored
  channels. One temporary, explicitly bounded exception is permitted: when the
  provider refuses notification coverage for a channel whose chat coverage is
  live, chat MUST be preserved and the channel MUST be reported as
  covered-but-degraded rather than complete. That degraded state MUST NOT be
  permanent — notification coverage MUST become repairable again after at most
  one hour, and immediately on reconnect or retirement of the channel's
  connection, and successful creation or adoption MUST clear it.
- **FR-002**: Coverage state MUST distinguish the two coverage types for each
  channel so that either missing type can be restored without duplicating the
  type that already exists.
- **FR-003**: Relevant gift and raid notifications MUST produce a suppression
  signal associated with the correct channel and containing the notice
  category and trustworthy occurrence time.
- **FR-004**: One chat-notification coverage type MUST supply both gift and raid
  notices; the feature MUST NOT require separate raid coverage.
- **FR-005**: Only `community_sub_gift`, `sub_gift`, and `raid` notifications
  MUST create or extend suppression. `unraid`, plain `sub`, `resub`, and every
  other notification category MUST leave suppression unchanged.
- **FR-006**: Suppression MUST be a half-open interval from notice occurrence
  through, but not including, its deadline. A notice before the current
  deadline belongs to the current overlapping chain; if it extends the
  deadline, the state MUST retain the earliest chain start and the later
  deadline. A notice at or after the current deadline MUST begin a new interval
  at its occurrence. A notice whose candidate deadline is earlier than or
  equal to the current deadline MUST leave the complete state unchanged. Gift
  and raid windows MUST default to 120 seconds and 180 seconds, respectively.
  Raid viewer count MUST NOT affect the window duration.
- **FR-007**: The system MUST prevent an otherwise qualifying spike from
  emitting a clip exactly when its peak is at or after the active interval's
  notice-bounded start and strictly before its deadline. A spike that peaked
  before the notice MUST remain eligible even if reported after the notice;
  a peak exactly at the deadline MUST remain eligible.
- **FR-008**: Suppression MUST gate only clip emission. Every chat message
  received during suppression MUST continue to update rolling message counts,
  the rolling baseline, and all other message-derived state used by later
  detection decisions exactly as it would without suppression.
- **FR-009**: When no suppression window is active, existing clip eligibility
  behavior MUST remain unchanged.
- **FR-010**: Duplicate or out-of-order suppression signals MUST NOT shorten a
  channel's current suppression deadline.
- **FR-011**: Missing or lagging chat-notification coverage and delayed
  suppression delivery MUST fail open: absence of a suppression signal MUST
  NOT block normal clip eligibility, and the degraded condition MUST be
  operationally distinguishable from healthy dual coverage.
- **FR-012**: Every suppressed would-have-clipped spike MUST emit both an
  operator-visible metric and a structured log and MUST NOT emit a clip.
- **FR-013**: The monitored set MUST contain no more than 400 channels. The
  effective join and leave thresholds MUST both be 400 so no operating mode
  exceeds that ceiling.
- **FR-014**: At the 400-channel ceiling, complete dual coverage MUST use no
  more than 800 of the existing 900 allowed subscription slots, preserving at
  least 100 slots for reconnect and adoption safety.
- **FR-015**: Capacity and coverage reporting MUST distinguish monitored
  channels from individual subscriptions so operators can verify both the
  400-channel ceiling and two-coverages-per-channel invariant.
- **FR-016**: The feature MUST continue using the existing application identity
  and operator authorization, whose current permissions already cover both
  chat coverage types. It MUST NOT require authorization reseeding or expanded
  permissions.
- **FR-017**: A malformed suppression input that lacks a trustworthy channel
  identity or occurrence time MUST NOT create a guessed deadline and MUST be
  operationally visible. `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS` MUST be a fixed
  contract bound of 30 seconds, not an environment setting. After schema and
  field decoding, a record is trustworthy only when
  `occurred_at_ms <= consumer_receipt_ms + 30_000`; a record one millisecond
  beyond that bound MUST be rejected as malformed fields, counted and logged,
  with no delivery observation and no state write. Accepted future skew within
  the bound MUST use delivery age zero and emit the existing clock-skew
  diagnostic.
- **FR-018**: A suppression signal received after a clip was emitted MUST NOT
  retroactively retract that clip. If the signal establishes a suppression
  interval that has not expired, it MUST apply only to subsequent clip
  decisions whose peaks fall inside that notice-bounded interval.

### Non-Functional Requirements

- **NFR-001**: The system MUST preserve the existing three-session,
  300-subscription-per-session limit and MUST refuse growth beyond the
  400-channel monitored ceiling rather than consume the reconnect/adoption
  safety headroom.
- **NFR-002**: Suppression MUST be isolated by channel; a notice for one channel
  MUST NOT alter clip eligibility or detector state for another channel.
- **NFR-003**: Coverage reconciliation MUST converge safely from either partial
  state: chat-message-only or chat-notification-only. The covered-but-degraded
  state produced by a refused notification subscription MUST also converge:
  it MUST hold off repeated notification creates for at most one hour, MUST be
  re-eligible immediately after a connection reconnect or retirement, and MUST
  return to ordinary partial-state repair when it expires.
- **NFR-004**: Operational status MUST make incomplete dual coverage,
  malformed suppression inputs, and capacity refusal distinguishable from
  healthy operation.
- **NFR-005**: Operational status MUST also distinguish lagging suppression
  delivery from healthy delivery while clip detection continues fail-open.
  Delivery health MUST be classified from received suppression records against
  an operator-configured warning threshold defaulting to 30 seconds: a record
  whose age at receipt is at or below the threshold is healthy, a record older
  than the threshold is lagging, and an observation window containing no
  received record is idle/unknown — neither healthy nor lagging. Coverage
  status separately identifies missing notification subscriptions. Complete
  coverage combined with topic silence MUST NOT be reported as proven-healthy
  delivery, and the feature does not claim to detect a stalled delivery path
  during a period in which no relevant notice occurred. The fixed
  `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` trust check MUST run after decode
  and field validation but before any delivery-health observation or state
  mutation; over-bound records are malformed, not healthy clock skew.
- **NFR-006**: The metric and structured log for each suppressed
  would-have-clipped spike MUST be attributable to the affected channel and
  distinguishable from coverage, delivery, malformed-input, and capacity
  signals.
- **NFR-007**: Monitored-set churn caused by the locked zero-width 400/400
  hysteresis band MUST be bounded and measured, not assumed. Desired-set
  entries plus departures attributable to that band, averaged per poll across a
  24-hour deployed observation, MUST NOT exceed 2% of the 400-channel ceiling —
  that is, 8 membership changes per poll. Exceeding that bound blocks enabling
  suppression gating and requires a specification change to a narrower join
  threshold inside the firm 400 ceiling; it MUST NOT be worked around by hidden
  code behaviour that differs from the configured thresholds.

### Key Entities

- **Monitored channel**: A broadcaster currently selected for chat monitoring;
  it occupies two subscription slots and is subject to the 400-channel ceiling.
- **Channel coverage**: The independently tracked chat-message and
  chat-notification coverage associated with one monitored channel.
- **Suppression signal**: A relevant gift or raid notice associated with a
  channel, occurrence time, and one of the triggering categories
  `community_sub_gift`, `sub_gift`, or `raid`. Raid audience size does not
  affect suppression duration.
- **Suppression window**: Channel-specific half-open interval
  `[suppress_from_ms, suppress_until_ms)` during which clip emission is gated
  while message-derived detector state continues to advance. Its start is the
  earliest notice occurrence retained for the current overlapping chain; its
  duration is determined from operator-configured category windows defaulting
  to 120 seconds for gifts and 180 seconds for raids.
- **Would-have-clipped spike**: A detection decision that meets normal clip
  eligibility but does not emit because a suppression window is active.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An audit at any supported monitored-set size finds both coverage
  types for 100% of channels that have completed convergence, with partial
  states explicitly identified until restored. Channels inside an active,
  time-bounded notification-refusal hold-off are the one identified exception:
  they are reported as covered-but-degraded rather than complete, they retain
  chat coverage, and each becomes eligible for repair again within one hour or
  immediately after a connection reconnect or retirement.
- **SC-002**: In validation containing `community_sub_gift`, `sub_gift`, `raid`,
  `unraid`, plain `sub`, and `resub`, 100% of the first three categories create
  channel-correct suppression signals and 0% of the excluded categories create
  or extend suppression.
- **SC-003**: Across deterministic gift, raid, and overlapping-notice
  scenarios, zero otherwise qualifying spikes whose peaks are inside
  `[suppress_from_ms, suppress_until_ms)` emit clips, and 100% of those
  suppressed would-have-clipped spikes emit both an operator-visible metric
  and a structured log. Spikes peaking before the notice or exactly at the
  deadline retain normal eligibility.
- **SC-004**: For identical replayed message sequences, message counts, rolling
  baseline, and other message-derived state after suppression are identical to
  a run without emission gating; only clip emission and the required
  would-have-clipped metric and structured log may differ.
- **SC-005**: For every overlapping-notice scenario, an extending notice before
  the current deadline preserves the earliest start and advances the deadline
  to its later candidate, an earlier/equal candidate leaves all state
  unchanged, and a notice at or after the deadline starts a new interval at its
  occurrence. Default-duration validation uses 120 seconds for gifts and 180
  seconds for raids.
- **SC-006**: At maximum capacity, exactly 400 channels receive complete dual
  coverage using 800 subscriptions, the effective join and leave thresholds
  are both 400, at least 100 subscription slots remain as safety headroom, and
  attempts to admit a 401st channel do not exceed the ceiling.
- **SC-007**: Existing clip-eligible scenarios with no active suppression
  produce the same emission decisions as before this feature.
- **SC-008**: Operators can distinguish complete dual coverage, partial
  coverage, malformed suppression input, and capacity refusal from the
  feature's operational status without inspecting individual chat messages.
- **SC-009**: Deployment requires zero new application identities, zero
  authorization reseeds, and zero added authorization permissions.
- **SC-010**: In deterministic degraded-coverage and delayed-delivery
  validation, 100% of otherwise eligible clips continue normal emission,
  degradation is operationally visible, and no emitted clip is later
  retracted. Delivery health is reported as healthy, lagging, or idle/unknown
  strictly from received-record age against the 30-second default warning
  threshold, and an observation window of legitimate silence is reported as
  idle/unknown rather than as either healthy or lagging. Records at the fixed
  30-second future-skew boundary are accepted with age zero and a clock-skew
  diagnostic; records one millisecond beyond are rejected, counted, and logged
  before any delivery observation or state write.
- **SC-011**: Across a 24-hour deployed observation with the 400/400
  thresholds in force, desired-set entries plus departures attributable to the
  zero-width band average at most 8 membership changes per poll — 2% of the
  400-channel ceiling. A higher observed rate blocks enabling suppression
  gating and is resolved by a specification change, not by code. This outcome
  is produced only on the deployed system and MUST NOT be claimed from offline
  tests, fixtures, replay, or reasoning.

## Assumptions & Dependencies

- The existing Twitch application identity and operator authorization remain
  valid and already permit both required channel coverage types.
- The account continues to allow three concurrent sessions with up to 300
  enabled subscriptions each. The 400-channel ceiling deliberately does not
  consume the final 100 slots. This feature does not revisit those
  session-capacity assumptions.
- Existing ranking and desired-set behavior determines which channels qualify
  for monitoring; this feature changes the maximum admitted count, not ranking
  order or eligibility.
- Existing chat-message ingestion, spike qualification, and clip creation
  behavior remain the comparison baseline except where suppression explicitly
  gates emission.
- Gift and raid suppression durations are independently operator-configurable,
  default to 120 and 180 seconds respectively, and are not derived from raid
  viewer count.
- Suppression deliberately accepts the false negative when a genuine hype
  spike peaks inside the notice-bounded gift or raid interval. A spike that
  peaked before the notice remains eligible even if reported afterward.
- Missing or lagging auxiliary coverage and delayed suppression delivery fail
  open; they remain operationally visible and never cause retroactive clip
  retraction.
- A refused chat-notification subscription is a temporary condition, not a
  permanent one. The affected channel keeps chat, is reported as
  covered-but-degraded, and becomes repairable again within at most one hour or
  on the next reconnect or connection retirement.
- Suppression delivery health can only be judged from records that actually
  arrive. A silent window is idle/unknown; the feature deliberately does not add
  a heartbeat or synthetic traffic to make silence provably healthy.
- The locked 400/400 thresholds remove the hysteresis band, so a bounded amount
  of desired-set churn is expected; the accepted bound is measured on the
  deployed system rather than assumed (NFR-007, SC-011).
- Every suppressed would-have-clipped spike produces both an operator-visible
  metric and a structured log without creating a clip.

### Execution and Testing Constraint

This workstation is limited to code-and-unit-test work. Validation here may use
unit tests, deterministic offline replay, static checks, imports, and
compilation, but MUST NOT start application services or infrastructure, use
credentials, or make live Twitch calls. Live integration and deployed evidence
remain a later execution gate; this limitation does not relax any product
requirement or success criterion above.

## Out of Scope

- A second Twitch application identity, operator-token reseeding, or new
  authorization permissions.
- A separate raid subscription in addition to chat-notification coverage.
- Suppression for polls, predictions, plain `sub`, `resub`, `unraid`, or any
  notification category other than `community_sub_gift`, `sub_gift`, and
  `raid`.
- Viewer-count-derived raid suppression durations.
- Thresholded, sampled, opt-in, or otherwise partial chat-notification rollout;
  every monitored channel receives both coverage types.
- Raising the monitored ceiling above 400 or consuming the 100-slot
  reconnect/adoption safety headroom.
- A heartbeat, keep-alive, or synthetic-record protocol on the suppression path
  to make legitimate silence distinguishable from a stalled delivery path.
- A configurable future-time allowance; the 30-second maximum future skew is a
  fixed contract bound and introduces no environment variable.
- A hidden code-level hysteresis band or any other in-code deviation from the
  configured 400/400 thresholds.
- Changing channel ranking, clipping eligibility, anomaly thresholds, or clip
  content selection beyond gating emission during suppression.
- Weakening acceptance requirements because live-service validation is not
  available on this workstation.

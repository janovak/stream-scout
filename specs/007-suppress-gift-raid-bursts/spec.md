# Feature Specification: Suppress Gift and Raid Chat Bursts

**Feature Branch**: `007-suppress-gift-raid-bursts`
**Created**: 2026-09-03
**Status**: Clarified and planned — amended 2026-09-04 by an artifact remediation
pass that added NFR-007 and SC-011, bounded the auxiliary-coverage exception in
FR-001/NFR-003/SC-001, and defined delivery-lag semantics in NFR-005/SC-010
(see [autonomous-decisions.md](./autonomous-decisions.md) §17-§21), then by an
implementation-review correction that made windows notice-bounded and added a
fixed 30-second future-time trust bound (§22-§23), then by final code-review
corrections that apply that bound before source watermark generation as well
as in the operator (§24), attach the timestamp assigners after `from_source`
on both streams so they run at all (§25), and preserve the assigners through
the required builder order while hardening chat event time (§26), then by an
approved capacity amendment on 2026-09-05 that replaces the firm 400/400
ceiling with an entry threshold of 400, a retention-and-maximum threshold of
450, and exact 900-subscription capacity conditioned on exact-capacity
engineering (§27-§28)
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
each, so 900 subscriptions in total. The monitored-set thresholds therefore
change from the current 800/900 single-subscription ramp to an **entry
threshold of 400** and a **retention-and-maximum threshold of 450**. A channel
that is not already monitored enters only inside the top 400 by rank; a channel
already monitored is retained through rank 450; beyond 450 it leaves. At the
450-channel maximum, dual coverage consumes all 900 subscriptions — capacity is
exact and no free slot is guaranteed.

That is deliberately the same shape as the 800/900 ramp it replaces: a deep
entry gate, a retention band above it, and a ceiling that consumes the account
exactly. It is halved because every channel now costs two subscriptions instead
of one. Reconnect and adoption do not normally need spare slots — a closed
session's subscriptions are disabled and stop counting, and adopting an
existing subscription creates nothing — so the exceptional cases are what
zero slack exposes. Operating at exact capacity is therefore accepted only
alongside the placement and capacity-visibility requirements below, and only on
deployed evidence.

## Clarifications

### Session 2026-09-03

- Q: What suppression-window values and policy apply to gift and raid notices? → A: Operator-configured windows default to 120 seconds for gifts and 180 seconds for raids; raid viewer count does not affect duration.
- Q: Which notice types trigger suppression, and is overlapping genuine hype an accepted false negative? → A: Only `community_sub_gift`, `sub_gift`, and `raid` trigger suppression; `unraid`, plain `sub`, and `resub` are excluded, and genuine hype whose spike peak falls at or after the notice and before the active deadline is an accepted false negative. A spike that peaked before the notice is not an overlap and remains eligible even if its hold reports later.

### Session 2026-09-04

- Q: When notification coverage is absent or lagging, or the suppression topic is delayed, how should clip detection behave? → A: Fail open by continuing normal clip eligibility, expose the degraded condition operationally, and never retroactively retract an emitted clip.
- Q: What operator visibility is required when an otherwise qualifying spike is suppressed? → A: Every suppressed would-have-clipped spike emits both an operator-visible metric and a structured log; it does not create a clip.
- Q: What final monitored-set capacity and ramp thresholds apply? → A: The locked firm maximum is 400 monitored channels, with effective join and leave thresholds both set to 400; this feature does not revisit the existing session-capacity assumptions. *(Superseded 2026-09-05 — see the capacity amendment below.)*

### Capacity amendment, 2026-09-05

The approved amendment replaces the 400/400 capacity answer above. It changes
capacity numbers only; suppression, window, event-time, and topic behavior are
untouched, and the suppression event contract is capacity-independent.

| Superseded text | Replaced by |
|---|---|
| FR-013 "no more than 400 channels … effective join and leave thresholds MUST both be 400" | FR-013 entry 400 / retention-and-maximum 450 |
| FR-014 "no more than 800 of the existing 900 allowed subscription slots, preserving at least 100 slots" | FR-014 exactly 900 of 900 at the maximum, no reserve guarantee |
| FR-015 "verify both the 400-channel ceiling and two-coverages-per-channel invariant" | FR-015 entry boundary 400, maximum 450, ≤300 per session, ≤900 total |
| NFR-001 "refuse growth beyond the 400-channel monitored ceiling rather than consume the reconnect/adoption safety headroom" | NFR-001 refuse growth beyond 450, exact capacity permitted only with placement and capacity observability |
| NFR-007 "MUST NOT exceed 2% of the 400-channel ceiling — that is, 8 membership changes per poll … Exceeding that bound blocks enabling suppression gating" | NFR-007 advisory bounded-label churn telemetry with no numeric release gate |
| SC-006 "exactly 400 channels … using 800 subscriptions … at least 100 subscription slots remain … a 401st channel" | SC-006 entry 400, retained band to 450, exact 450/900, 451st excluded, split-fragment replacement converges |
| SC-011 "Across a 24-hour deployed observation … average at most 8 membership changes per poll" | SC-011 churn-metric correctness and visibility, no 24-hour bound |

The original wording of each superseded requirement is preserved in the git
history of this file and in autonomous decisions 5, 14, and 19, which are
marked superseded rather than rewritten. No superseded requirement keeps an
active identifier here.

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
chat-notification coverage without exceeding subscription capacity, so that
suppression is consistently available and the pool stays correct and legible
even when the account is exactly full.

**Why this priority**: Partial coverage would make suppression unpredictable,
while an over-committed or falsely-full pool would strand capacity the capacity
model depends on.

**Independent Test**: Audit a desired set at the 450-channel maximum and verify
two distinct active coverage types per channel, no deliberate partial rollout,
exactly 900 steady-state subscriptions with no session above 300, refusal of a
451st monitored channel, and that a channel newly ranked 401-450 does not enter
while an incumbent at that rank is retained.

**Acceptance Scenarios**:

1. **Given** any channel in the monitored set, **When** coverage converges,
   **Then** that channel has both chat-message and chat-notification coverage.
2. **Given** 450 monitored channels, **When** coverage is complete, **Then**
   exactly 900 channel subscriptions are in use, no session holds more than
   300, and no free subscription slot is promised or reserved.
3. **Given** a monitored set of 450 channels and another eligible channel,
   **When** the system evaluates adding it, **Then** the monitored set remains
   at 450 or fewer and the additional channel is not admitted.
4. **Given** a channel that is not currently monitored and ranks between 401
   and 450, **When** membership is evaluated, **Then** it does not enter;
   **and given** a currently monitored channel at the same rank, **Then** it is
   retained until its rank falls beyond 450.
5. **Given** only one coverage type exists for a monitored channel, **When**
   coverage is reconciled, **Then** the missing type is restored without
   duplicating the existing type.
6. **Given** no single connection has two free subscription slots but at least
   two free slots exist across the pool, **When** a channel's pair is placed,
   **Then** one slot is reserved on each of two connections as a single
   all-or-nothing action, and a failure after reservation keeps the coverage
   half that succeeded.
7. **Given** the account is exactly full, **When** another subscription is
   required, **Then** the refusal is reported as a capacity condition
   distinguishable from a provider refusal and from a transient failure, it
   does not evict existing coverage, and it does not impose a transient wait
   before the next legitimate placement opportunity.
8. **Given** a connection reports itself full below the 300-subscription cap,
   **When** that condition exists, **Then** it is operationally visible, and it
   is cleared and re-evaluated when that connection reconnects or is retired.
9. **Given** the provider refuses chat-notification coverage for a channel
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
- A 451st channel qualifies while 450 are monitored. It is not admitted by
  exceeding the maximum.
- A channel that is not monitored reaches rank 401-450. It does not enter,
  because entry requires rank inside the top 400; an already-monitored channel
  at the same rank is retained until its rank falls beyond 450.
- The account is exactly full at 900 subscriptions and one more subscription is
  required. The condition is reported as capacity, distinctly from a provider
  refusal and from a transient failure; existing coverage is never evicted to
  make room, and no transient wait is imposed before the next legitimate
  placement opportunity.
- Two free subscription slots exist but on two different connections, so no
  single connection can hold a whole pair. The pair is placed by reserving one
  slot on each connection in a single all-or-nothing action; if one half then
  fails to be created, the successful half is kept and the channel converges
  through ordinary partial-coverage repair.
- A connection reports itself full at an occupancy below the 300-subscription
  cap. That stranded capacity is operationally visible rather than silent, and
  the condition is cleared and re-evaluated when the connection reconnects or
  is retired.
- An enabled subscription exists on the account that this feature did not
  create, or a delete that was reported as failed left one behind. At exact
  capacity it consumes a slot the model has allocated, so it is treated as a
  capacity fault to be found and removed rather than as headroom to absorb it.
- A relevant notification lacks a trustworthy channel identity or occurrence
  time. It cannot create a guessed suppression deadline; the malformed input
  is made operationally visible.
- A decoded notice claims an occurrence time at most 30 seconds ahead of the
  source/consumer receipt wall clock. At exactly +30,000 ms the source assigns
  `occurred_at_ms` as event time and the operator accepts it, clamps delivery
  age to zero, and emits the existing clock-skew diagnostic. At +30,001 ms the
  source assigns the Kafka record timestamp for watermark purposes, without
  rewriting the payload, and the operator rejects the original occurrence time
  as malformed fields, counted and logged, with no delivery observation or
  suppression-state write.

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
  category and trustworthy occurrence time. That occurrence time MUST be the
  event time the detector actually uses for the suppression interval, and the
  chat side MUST likewise use its own message timestamp, so both sides of the
  suppression comparison are evaluated on Twitch's shared clock rather than on
  a broker ingestion clock. The chat timestamp assigner MUST apply the fixed
  `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` source-event-time bound and accept
  only a plain integer (not boolean) `sent_at` at or before
  `source_clock_ms + 30_000`; missing, null, string, float, boolean, or
  +30,001 ms values MUST use Kafka record time for event-time assignment
  without rewriting, rejecting, or dropping the chat message (autonomous
  decisions 25-26).
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
- **FR-013**: The monitored set MUST be bounded by two distinct thresholds: an
  entry threshold of 400 and a retention-and-maximum threshold of 450. A
  channel that is not already monitored MUST enter only while its rank is
  inside the top 400. A channel already monitored MUST be retained while its
  rank remains inside the top 450, and MUST leave once its rank falls beyond
  450. Consequently a freshly qualifying channel ranked 401-450 MUST NOT enter,
  while a retained incumbent at those ranks MAY remain. The monitored set MUST
  NEVER exceed 450 channels.
- **FR-014**: At the 450-channel maximum, complete dual coverage MAY consume
  all 900 allowed subscription slots. The system MUST NOT promise, reserve, or
  depend on any guaranteed free subscription slot. Admission beyond 450
  channels MUST be refused at the intent layer that computes the monitored set,
  so the transport is never asked for a 901st subscription; transport-level
  refusal remains a loud second line of defence, not the primary control.
- **FR-015**: Capacity and coverage reporting MUST distinguish monitored
  channels from individual subscriptions, so operators can verify the
  400-channel entry boundary, the 450-channel maximum, per-connection occupancy
  of at most 300 subscriptions, total occupancy of at most 900 subscriptions,
  and the two-coverages-per-channel invariant.
- **FR-016**: The feature MUST continue using the existing application identity
  and operator authorization, whose current permissions already cover both
  chat coverage types. It MUST NOT require authorization reseeding or expanded
  permissions.
- **FR-017**: A malformed suppression input that lacks a trustworthy channel
  identity or occurrence time MUST NOT create a guessed deadline and MUST be
  operationally visible. `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS` MUST be a fixed
  contract bound of 30 seconds, not an environment setting, and MUST be
  enforced independently at two layers against the same injected/current
  receipt/source wall-clock basis. Before source watermark generation, a
  parsed `occurred_at_ms <= source_clock_ms + 30_000` MUST be assigned as event
  time; a value beyond that bound MUST use the Kafka `record_timestamp`
  instead, as missing or unreadable occurrence time already does. This source
  fallback protects event time only: it MUST NOT rewrite the payload or make
  the record valid. After schema and field decoding, `process_element2` MUST
  validate the original payload against
  `occurred_at_ms <= consumer_receipt_ms + 30_000`; one millisecond beyond
  MUST be rejected as `reason="fields"`, counted and logged, with no delivery
  observation, state access, or state write. Equality at +30,000 ms uses
  `occurred_at_ms` as source event time and is accepted downstream with age
  zero and the existing clock-skew diagnostic; +30,001 ms uses
  `record_timestamp` upstream and remains operationally visible through
  downstream rejection.
- **FR-018**: A suppression signal received after a clip was emitted MUST NOT
  retroactively retract that clip. If the signal establishes a suppression
  interval that has not expired, it MUST apply only to subsequent clip
  decisions whose peaks fall inside that notice-bounded interval.

### Non-Functional Requirements

- **NFR-001**: The system MUST preserve the existing three-connection,
  300-subscription-per-connection limit — a maximum of 900 subscriptions and
  therefore at most 450 dual-covered channels — and MUST refuse growth beyond
  the 450-channel maximum rather than over-commit the account. Operating at
  exact capacity is permitted only while the supporting behaviour exists: pair
  placement that can reserve one slot on each of two connections when no single
  connection can hold the pair, a capacity refusal that is distinguishable from
  a provider refusal and from a transient failure, and observable per-connection
  full state including a connection that reports itself full below the
  300-subscription cap.
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
  `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` trust check MUST protect source
  timestamp assignment before watermark generation and MUST run again after
  decode and field validation but before any delivery-health observation or
  state mutation. Untrustworthy occurrence time MUST neither advance
  suppression event time using that value nor create state; over-bound records
  are malformed, not healthy clock skew.
- **NFR-006**: The metric and structured log for each suppressed
  would-have-clipped spike MUST be attributable to the affected channel and
  distinguishable from coverage, delivery, malformed-input, and capacity
  signals.
- **NFR-007**: Monitored-set churn attributable to the 400/450 entry-retention
  band MUST be observable as bounded-label telemetry: desired-set entries plus
  departures per poll, with no per-channel label growth. This telemetry is
  **advisory**. It carries no numeric release gate, it MUST NOT block enabling
  suppression gating, and no observation window is required before release. Any
  change to the configured entry or retention thresholds remains a
  specification change and MUST NOT be worked around by hidden code behaviour
  that differs from the configured thresholds.

### Key Entities

- **Monitored channel**: A broadcaster currently selected for chat monitoring;
  it occupies two subscription slots, enters only inside the top 400 by rank,
  and is retained through rank 450, which is also the maximum size of the
  monitored set.
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
- **SC-006**: Capacity behaviour holds across the entry band and at the
  maximum. From an empty monitored set, admission stops at 400 channels because
  entry requires a rank inside the top 400. Retained incumbents extend the set
  through rank 450, so it can reach but never exceed 450 channels. At 450
  channels, exactly 900 subscriptions are in use, no connection holds more than
  300, and no free slot is promised. A 451st qualifying channel is excluded.
  When a pair split across two connections loses one fragment, replacement of
  the missing half converges back to complete coverage without exceeding 900
  subscriptions.
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
  30-second future-skew boundary use occurrence time for source watermark
  assignment and are accepted with age zero and a clock-skew diagnostic.
  Records one millisecond beyond use the Kafka record timestamp for source
  watermark assignment, without payload rewriting, and are rejected, counted,
  and logged downstream before any delivery observation or state access.
- **SC-011**: The desired-set churn signal is correct and operator-visible: it
  increments by entered plus departed channels for each successful desired-set
  publication, keeps bounded labels with no per-channel growth, and can be read
  against poll history. No numeric bound, observation window, or release gate
  is attached to it.

## Assumptions & Dependencies

- The existing Twitch application identity and operator authorization remain
  valid and already permit both required channel coverage types.
- The account continues to allow three concurrent connections with up to 300
  enabled subscriptions each, so 900 subscriptions in total. At the
  450-channel maximum, dual coverage consumes all 900: there is no reserve.
  Reconnect and adoption normally consume no new slots — a closed session's
  subscriptions are disabled and stop counting against the limit, and adopting
  an existing subscription creates nothing — so the exceptional cases are what
  exact capacity exposes: parity fragmentation across connections, in-flight
  create/delete overlap, enabled subscriptions this feature did not create,
  failed deletes, and a connection reporting itself full below the cap. Those
  are accepted only with the placement and capacity-visibility behaviour in
  NFR-001 and with deployed evidence.
- Existing ranking and desired-set behavior determines which channels qualify
  for monitoring; this feature changes the entry and retention thresholds, not
  ranking order or eligibility.
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
- The 400/450 entry-retention band produces the same kind of desired-set
  behaviour as the pre-007 800/900 ramp: a boundary-rank channel is retained
  rather than re-admitted each poll. The resulting churn is observed as
  advisory telemetry rather than gated on a number (NFR-007, SC-011).
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
- Raising the monitored maximum above 450 channels, exceeding 900
  subscriptions, or opening a fourth websocket connection.
- Migrating or compacting existing subscriptions between connections to
  defragment the pool; a pair may be split across connections instead.
- A heartbeat, keep-alive, or synthetic-record protocol on the suppression path
  to make legitimate silence distinguishable from a stalled delivery path.
- A configurable future-time allowance; the 30-second maximum future skew is a
  fixed contract bound and introduces no environment variable.
- A hidden code-level hysteresis band or any other in-code deviation from the
  configured 400/450 entry and retention thresholds.
- Changing channel ranking, clipping eligibility, anomaly thresholds, or clip
  content selection beyond gating emission during suppression.
- Weakening acceptance requirements because live-service validation is not
  available on this workstation.

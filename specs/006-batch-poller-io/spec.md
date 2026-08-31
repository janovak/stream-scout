# Feature Specification: Batched Poller Datastore I/O

**Feature Branch**: `006-batch-poller-io`
**Created**: 2026-08-30
**Status**: Draft
**Input**: Remove channel-count-dependent remote datastore latency from the
stream poller while preserving desired-set, hysteresis, lifecycle, and
reconciler behavior.

## Overview

The stream poller and the EventSub reconciler share one process and one event
loop. The poller currently performs remote online-state reads and refreshes and
a metadata transaction once per ranked channel. Those calls are synchronous,
so the reconciler cannot run while the datastore loop is active.

Operator measurements supplied for this feature place normal remote request
latency around 40-110 ms. The resulting linear cost has already pushed a poll
to about 116 seconds near 500 channels under slower network conditions and
beyond its 120-second schedule near 800 channels. Once that happens, the
scheduler skips polls, 180-second online-state expirations are no longer
refreshed reliably, and the reconciler loses its opportunity to run.

The 10-second upper target at 900 leaves at least 110 seconds before the next
scheduled poll. Successful refreshes on consecutive ticks normally remain
about 60 seconds inside the online-state expiration, less schedule jitter and
any change in where the refresh lands within each poll. One missed refresh can
consume that entire margin. The target therefore protects both scheduler
availability and state freshness rather than merely improving average latency.

This feature makes remote datastore interactions a fixed set of batched phases
per poll. The amount of local iteration, data transferred, datastore-side row
processing, and ranking-source pagination may still grow with channel count.
The required outcome is bounded network request/response cycles, not constant
total work.

## Baseline Evidence

- A local call-count probe supplied for this feature recorded 104 online/intent
  store calls and 50 metadata upserts at 50 ranked channels, 604 and 300 at
  300, 1,004 and 500 at 500, and 1,604 and 800 at 800. These are lower bounds
  when no departed-login checks are needed.
- Production observations supplied for this feature on 2026-08-30 recorded
  about 67 seconds near 300 channels, about 116 seconds near 500 under the
  later network conditions, and more than 120 seconds near 800.
- [`OPERATIONS.md`](../../OPERATIONS.md), "Ramping the monitored channel
  count," and
  [`specs/004-eventsub-parallel-reconciler/research.md`](../004-eventsub-parallel-reconciler/research.md),
  "Post-merge: the 2026-08-30 channel-count ramp," independently document the
  800-channel scheduler skip and rollback. Their earlier statement that about
  500 channels was safe describes the successful ramp conditions, not a
  guarantee under the slower network conditions observed later.

## Scope Decision: Cold Start Remains Rate-Limit Bound

This feature removes poller starvation; it does not change subscription-create
rate limiting. A steady-state reconciler at 500-900 channels must remain
schedulable and healthy while polls run. A cold 800-900-channel start may still
take more than 200 seconds under the existing retry and backoff policy because
the external service accepts only about 360-420 subscription creates in a
burst.

There is no new hard cold-start convergence deadline in this feature. When the
external service resumes accepting requests, the existing reconciler must be
able to make progress on later retry rounds and passes without being starved by
the poller. Eventual convergence remains conditional on that existing policy
and external acceptance; this feature adds no new convergence algorithm or
deadline. The existing transport limit remains three connections of 300
subscriptions, exactly 900 with no headroom.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Poll on schedule at production scale (Priority: P1)

As the operator, I want polling time to stay comfortably below its schedule as
the ranked set grows, so that online state stays fresh and the reconciler keeps
running.

**Why this priority**: Poll starvation is the observed ceiling preventing a
safe ramp beyond the current production setting.

**Independent Test**: Run production-equivalent polls at 50, 500, and 900
ranked channels, count remote datastore interactions, measure total duration,
and observe reconciler completion signals.

**Acceptance Scenarios**:

1. **Given** the Performance Measurement Profile and exactly 500 eligible
   ranked channels, **When** stable and completely changed ranking profiles are
   measured separately, **Then** nearest-rank p95 poll duration is under 5
   seconds for each profile.
2. **Given** the same profile and exactly 900 eligible ranked channels,
   **When** stable and completely changed ranking profiles are measured
   separately, **Then** nearest-rank p95 poll duration is under 10 seconds for
   each profile.
3. **Given** otherwise equivalent polls at 50 and 500 ranked channels,
   **When** remote online-state and metadata interactions are counted, **Then**
   both sizes use the same bounded number of request/response cycles and
   neither contains a per-channel datastore request.
4. **Given** separate steady-state runs at 500 and 900 ranked channels after
   subscription convergence, **When** each run lasts 30 minutes, **Then** no
   poll is skipped because a prior poll is still running and the maximum gap
   between reconciler last-success advances is at most 15 seconds at 500 and
   20 seconds at 900.
5. **Given** a cold start targeting 900 channels that encounters at least one
   subscription-create backoff after service initialization is complete,
   **When** polls occur during the in-progress reconcile pass, **Then** no poll
   is skipped for overlap, every non-failing poll remains within the 10-second
   target, and accepted retry windows continue increasing subscription
   coverage even though the reconciler last-success signal may not advance
   until that pass completes.

---

### User Story 2 - Preserve ranking and lifecycle meaning (Priority: P1)

As the operator, I want batching to be behaviorally invisible to downstream
consumers, so that channel membership and online/offline events retain their
current meaning.

**Why this priority**: Faster polling is not acceptable if it changes
hysteresis, emits duplicate events, or loses broadcaster identity.

**Independent Test**: Replay empty, partial, stable, and completely changed
rankings from controlled previous desired sets and online-state snapshots, then
compare desired membership and lifecycle output with the current rules.

**Acceptance Scenarios**:

1. **Given** a ranked channel whose online state was absent before refresh and
   whose numeric rank is no greater than the configured entry threshold,
   **When** the poll succeeds, **Then** its state is refreshed and exactly one
   `online` event is emitted.
2. **Given** a newly observed channel outside the entry band, **When** the poll
   succeeds, **Then** its state is refreshed without an `online` event.
3. **Given** a login that leaves the desired set, **When** its online state had
   already expired before the poll's refresh phase, **Then** exactly one
   `offline` event is emitted with the broadcaster ID from the previous
   desired-set ID map.
4. **Given** a login that leaves the desired set but still had online state
   before refresh, **When** the poll succeeds, **Then** no `offline` event is
   emitted.
5. **Given** empty, partial, stable, or completely changed rankings, **When**
   polling succeeds, **Then** desired-set publication follows the existing
   entry/retention hysteresis rules and contains the matching broadcaster IDs.
6. **Given** a departed login has no valid broadcaster ID in the previous
   desired-set ID map, **When** its online state has expired, **Then** no
   placeholder-ID `offline` event is emitted and the data-integrity error is
   visible.

---

### User Story 3 - Persist streamer metadata atomically (Priority: P1)

As the operator, I want every observed streamer recorded efficiently and
consistently, so that increasing the ranked set does not trade latency for
partial or stale metadata.

**Why this priority**: The current per-streamer transactions are a principal
source of remote latency, while the metadata remains operationally important.

**Independent Test**: Persist a batch larger than 500 records against real
Postgres, including existing rows, login changes, duplicate identities, and a
deliberately malformed batch.

**Acceptance Scenarios**:

1. **Given** a valid metadata batch, **When** persistence succeeds, **Then**
   every unique streamer identity is inserted or updated, login changes are
   retained, and `last_seen_at` advances for every identity in that poll.
2. **Given** the same streamer identity appears more than once in one ranking,
   **When** the batch succeeds, **Then** one final row is stored using the login
   from the last occurrence in ranking order, matching the prior sequential
   result.
3. **Given** any malformed record or persistence failure, **When** the batch is
   attempted, **Then** no row from that batch is committed, the failure and
   batch size are logged, the connection is returned in a reusable state, and
   all current records are retried on the next poll.
4. **Given** a metadata batch failure but healthy online-state and desired-set
   stores, **When** the poll continues, **Then** online-state refresh,
   lifecycle evaluation, desired-intent publication, and reconciler
   notification may still complete; the metadata phase is not reported as
   successful.
5. **Given** one malformed record remains present across polls, **When** the
   whole batch continues to fail, **Then** the duration or count of consecutive
   metadata failures is visible and all metadata in that batch is explicitly
   understood to remain stale until the input or operational fault is resolved.

---

### User Story 4 - Ramp and roll back with evidence (Priority: P2)

As the operator, I want poll and phase timing to be visible while retaining the
current production thresholds, so that a later channel ramp can be judged and
rolled back independently.

**Why this priority**: The code should land at the already proven production
operating point before any separate scale change is attempted.

**Independent Test**: Deploy at the existing thresholds, inspect total and
phase timing signals over normal polls, and verify that reverting the feature
requires no threshold, schema, or dependency rollback.

**Acceptance Scenarios**:

1. **Given** a completed or failed poll, **When** telemetry and logs are
   inspected, **Then** total duration and meaningful phase durations identify
   ranking fetch, metadata persistence, online-state handling, and desired-set
   publication.
2. **Given** the feature deployment, **When** production configuration is
   compared before and after, **Then** entry threshold 150, retention threshold
   300, and the existing clipping-disabled fetch buffer are unchanged.
3. **Given** a regression requiring rollback, **When** the feature revision is
   reverted, **Then** the service returns to the previous poller behavior
   without a datastore schema reversal or a production threshold change.

### Edge Cases

- Both the current ranking and previous desired set are empty. The empty
  desired intent is still valid, but no metadata write, online-state read, or
  online-state refresh is issued.
- The current ranking is empty but the previous desired set is not. The poll
  publishes empty intent and performs only the online-state work needed to
  evaluate departed logins; it does not issue an empty metadata write or
  refresh batch.
- The ranking is partial because the ranking source returns fewer eligible
  channels than requested. Every returned channel is processed and the normal
  hysteresis rules determine the resulting desired set.
- The ranking changes completely. New and departed channels are evaluated from
  one consistent pre-refresh online-state snapshot.
- A streamer identity occurs more than once with different logins. The
  last-ranked occurrence determines the metadata result; desired membership
  remains keyed by normalized login under existing rules.
- The online-state snapshot read fails. No refresh, lifecycle event, desired
  intent, reconciler notification, or success-shaped completion signal is
  produced; the error remains visible and the next scheduled poll retries.
- The previous desired-set read fails. No state-dependent lifecycle or desired
  publication is attempted, no reconciler notification or successful poll is
  reported, and the next scheduled poll retries. An independently completed
  metadata batch does not need to be rolled back.
- The online-state refresh batch fails or its outcome is unknown. No
  speculative lifecycle event or desired intent is published, the poll is
  reported failed, and the next poll uses the state it can then observe. If
  the remote store applied the refresh but its acknowledgement was lost, no
  exactly-once lifecycle guarantee is made for that indeterminate outcome.
- The online-state refresh batch is acknowledged but reports an error for any
  individual refresh. The entire refresh phase is treated as failed under the
  same rules; partial command success is not a successful poll.
- A refresh phase fails and one or more online-state entries expire before the
  next scheduled poll. On recovery, the normal pre-refresh rules apply:
  currently ranked channels inside the entry band may emit `online` again,
  channels outside it refresh without an event, and the correlated
  re-emission is visible as following a failed refresh. Failure history is not
  used to invent state that is no longer present.
- Desired-intent publication fails after a successful online-state refresh.
  Lifecycle events already derived from the acknowledged refresh are not
  rolled back, but the reconciler is not notified and the poll is not reported
  successful. A rejection known to occur before commit leaves the previous
  intent; an indeterminate acknowledgement may leave either the previous or
  new atomic intent visible. The next poll re-reads and atomically replaces
  whatever version is present rather than assuming publication succeeded.
- Metadata persistence fails while online-state storage remains healthy. The
  whole metadata batch rolls back, but the remaining poll phases may complete
  and the next poll retries all current metadata.
- One malformed record persists across polls. All metadata records in each
  affected batch remain uncommitted and are retried together; there is no
  per-row fallback. Consecutive failure visibility tells the operator that this
  is a persistent metadata outage rather than a transient retry.
- A departed login is missing a valid ID in the previous desired-set map. The
  poll logs a data-integrity failure and suppresses that offline event rather
  than publishing a fabricated ID; other valid lifecycle events and desired
  intent may continue.
- A cold 900-channel start enters external rate-limit backoff. Polls continue
  within their own timing bound while subscription creation follows its
  existing retry policy; the reconciler's last-success signal is allowed to
  remain unchanged while that long pass is still in progress.
- The desired set exceeds 900 channels. That exceeds existing transport
  capacity and is not made supportable by this feature.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Polling MUST preserve current ranking, clipping-eligibility,
  entry/retention hysteresis, desired-set ordering, and login-to-broadcaster-ID
  publication for empty, partial, stable, and completely changed rankings.
- **FR-002**: Current desired membership and departed logins MUST be determined
  before online-state I/O. Online-state evaluation MUST then use one consistent
  pre-refresh view covering the union of currently ranked and departed logins,
  so event decisions do not depend on refresh order.
- **FR-003**: For a poll that has online-state work, evaluating all current and
  departed logins MUST use at most one remote read cycle and refreshing all
  current logins MUST use at most one remote batch-execution cycle. A non-empty
  metadata batch MUST dispatch exactly one bulk write statement within one
  transaction, regardless of row count; transaction completion remains its
  separately counted fixed round trip. This statement constraint enforces
  channel-count-independent network cycles rather than limiting proportional
  row processing on the datastore server. The existing bounded desired-set
  read, clipping-eligibility lookup, and atomic desired-set publication remain
  separately bounded and do not require redesign.
- **FR-004**: The number of remote datastore request/response cycles for the
  same non-empty phase set MUST be independent of whether 50, 500, or 900
  channels are ranked. Payload bytes, local iteration, server-side commands,
  row processing, and ranking-source pagination MAY grow with channel count.
- **FR-005**: Every currently ranked channel's online state MUST be refreshed
  with the existing expiration and broadcaster ID after a successful
  online-state phase.
- **FR-006**: An `online` lifecycle event MUST be emitted only when the
  channel's online state was absent in the pre-refresh view and its rank is
  within the configured entry boundary.
- **FR-007**: A newly observed channel outside the entry band MUST have its
  online state refreshed without emitting an `online` lifecycle event.
- **FR-008**: An `offline` lifecycle event MUST be emitted only when a login
  leaves the desired set and its online state was absent in the pre-refresh
  view. The event MUST use the real broadcaster ID from the previous
  desired-set ID map. If that map has no valid ID for the login, the event MUST
  be suppressed and a data-integrity failure MUST be logged; a placeholder ID
  MUST NOT be published.
- **FR-009**: A successful metadata batch MUST insert or update every unique
  observed streamer identity, retain current normalized login values including
  login changes, and advance `last_seen_at`.
- **FR-010**: Duplicate streamer identities in one metadata batch MUST resolve
  to one update using the last occurrence in ranking order, preserving the
  final state produced by the previous sequential behavior.
- **FR-011**: A malformed or failed metadata batch MUST roll back as a whole,
  log the failure with useful batch context, return its connection in a
  reusable state, and leave all current streamer metadata eligible for retry
  on the next poll. Consecutive whole-batch failures MUST expose their count or
  elapsed duration so persistent all-row metadata staleness is alertable. The
  feature MUST NOT fall back to per-row writes for a poison record.
- **FR-012**: Metadata batch failure MUST NOT abort otherwise healthy
  online-state, lifecycle, desired-publication, or reconciler-notification
  phases, and MUST NOT be logged or measured as metadata success.
- **FR-013**: Failure of the previous desired-set read, online-state snapshot
  read, or online-state refresh MUST be visible and MUST prevent subsequent
  state-dependent lifecycle publication, desired-intent publication,
  reconciler notification, and successful poll completion for that poll. An
  acknowledged refresh batch that reports any element-level error MUST be
  treated as a refresh failure, even if other elements succeeded.
- **FR-014**: Failure or indeterminate acknowledgement of desired-intent
  publication MUST be visible, MUST suppress reconciler notification, and MUST
  prevent a successful poll completion signal. The poll MUST NOT assume which
  atomic intent version is visible and the next poll MUST re-read and replace
  it. Lifecycle events already emitted from an acknowledged online-state
  refresh are not transactional with desired-intent publication and MUST NOT
  be represented as having been rolled back.
- **FR-015**: Empty metadata or online-state refresh batches MUST NOT issue
  unnecessary datastore operations. A state read remains permitted when
  departed logins must be evaluated, and empty desired intent MUST still be
  publishable.
- **FR-016**: The reconciler MUST be notified only after desired intent has
  been published successfully.
- **FR-017**: Poll observability MUST expose total wall-clock duration and
  meaningful phase duration for ranking fetch, metadata persistence,
  online-state read/refresh, and desired-set publication. Failures MUST identify
  the failed phase without producing a success-shaped poll signal. Validation
  of reconciler schedulability MUST capture every pass completion directly
  with timestamps accurate to one second or better; a 15-second production
  metrics scrape MUST NOT be used to infer a 15- or 20-second maximum gap.
- **FR-018**: Automated validation MUST compare remote operation counts at 50
  and 500 ranked channels, confirm the same ceiling at 900, cover stable and
  fully changed rankings, and fail if any per-channel datastore interaction is
  reintroduced. It MUST also cover an empty current and previous set and an
  empty current set with departures, asserting the operation omissions in
  FR-015.
- **FR-019**: Validation against real Postgres MUST exercise more than 500
  rows, insert and update behavior, login changes, duplicate identities,
  timestamp advancement, whole-batch rollback, connection reuse after failure,
  and next-poll retry semantics.
- **FR-020**: Lifecycle validation MUST cover entry-band and outside-band new
  channels, stable channels, departed channels with present and expired state,
  real and missing prior broadcaster IDs, whole-batch and element-level
  online-state failure paths, and recovery after online state expires during a
  failed refresh interval.
- **FR-021**: Deployment MUST retain production entry threshold 150, retention
  threshold 300, and the current clipping-disabled fetch buffer. Any later
  channel-count ramp MUST be a separate operational change.
- **FR-022**: A cold start up to the existing 900-channel transport capacity
  MUST leave the reconciler able to continue under its existing retry and
  backoff policy when the external service resumes accepting requests.
  Accepted retry windows MUST increase coverage until all externally accepted
  subscriptions have been created, including across later reconcile passes,
  while polling remains on schedule. Eventual full convergence is conditional
  on the existing reconciler policy and external acceptance; this feature MUST
  NOT impose a new cold-start deadline or rate-limit algorithm.

### Non-Functional Requirements

- **NFR-001**: Under the Performance Measurement Profile, p95 poll duration
  MUST be under 5 seconds at 500 ranked channels and under 10 seconds at 900
  ranked channels for both stable and completely changed rankings.
- **NFR-002**: During separate 30-minute steady-state runs beginning after
  subscription convergence at 500 and 900 channels, no poll MUST be skipped
  because a previous poll is still active. The maximum observed gap between
  successive reconciler last-success advances MUST be at most 15 seconds at
  500 and 20 seconds at 900.
- **NFR-003**: The poller and reconciler MUST continue to share one process and
  one event loop. This feature MUST solve the latency problem by batching
  remote work, not by relocating the current per-row calls to threads or an
  executor.
- **NFR-004**: Redis and Postgres failures MUST remain operationally visible.
  Unknown outcomes MUST NOT be represented as successful state transitions.
- **NFR-005**: The feature MUST NOT require package, Python, datastore-client,
  scheduler, Flink, or other dependency upgrades.

### Key Entities

- **Ranked channel**: A normalized broadcaster login, broadcaster ID, and rank
  returned for the current poll.
- **Desired set**: The ranked broadcaster logins and login-to-ID map that
  express subscription intent, including the previous set that carries
  hysteresis state.
- **Online-state snapshot**: The pre-refresh presence or absence of online
  state for every current and departed login needed for lifecycle decisions.
- **Streamer metadata batch**: The unique broadcaster identities observed by a
  poll, with final normalized login and a common observation time.
- **Lifecycle event**: An `online` or `offline` transition with broadcaster
  identity, normalized login, rank semantics, and event time.
- **Poll completion signal**: Logs and timing signals that distinguish full
  poll success, metadata-only failure with continued intent publication, and a
  state/intent failure that aborts publication.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: At exactly 500 eligible ranked channels, the nearest-rank p95 of
  20 stable polls and, separately, 20 complete-turnover polls is under 5
  seconds from poll start through desired-intent publication and reconciler
  notification.
- **SC-002**: At exactly 900 eligible ranked channels, the equivalent
  nearest-rank p95 for each 20-poll profile is under 10 seconds, with no
  individual poll reaching 120 seconds or causing an overlap skip.
- **SC-003**: Operation-count instrumentation reports the same number of remote
  online-state and metadata request/response cycles at 50 and 500 ranked
  channels for equivalent non-empty phases; a 900-channel confirmation also
  stays at that count. No counted interaction is issued once per channel.
- **SC-004**: In 30-minute post-convergence steady-state runs at 500 and 900
  ranked channels, every scheduled poll completes and the maximum gap between
  reconciler last-success advances is at most 15 and 20 seconds, respectively.
- **SC-005**: Golden lifecycle scenarios produce the same desired membership
  and `online`/`offline` outputs before and after batching, including no online
  event outside the entry band and real broadcaster IDs on offline events.
  The missing-ID corruption case is an intentional correction: it suppresses
  the prior placeholder-ID event as required by FR-008.
- **SC-006**: A successful real-Postgres batch of more than 500 input records
  leaves every unique streamer present with the expected final login and an
  advanced `last_seen_at`; a forced invalid batch commits zero rows and the
  same connection can be used successfully afterward.
- **SC-007**: Forced online-state read, online-state refresh, desired-intent
  publication, previous desired-set read, element-level refresh, and metadata
  failures each produce the specified visible failure outcome, suppress
  prohibited success signals, and recover on a later healthy poll. Repeated
  poison-record batches also expose a consecutive-failure count or duration.
- **SC-008**: Operators can determine total poll time and the time spent in
  each named phase from production telemetry during the post-merge ramp.
- **SC-009**: The feature deploys and can be reverted while production remains
  at entry threshold 150 and retention threshold 300, with no dependency,
  schema, or clipping-buffer change.
- **SC-010**: During a cold start targeting 900 channels with at least one
  observed subscription-create backoff, every scheduled poll starts without an
  overlap skip, every non-failing poll completes in under 10 seconds, and the
  subscription count increases after each retry window in which the external
  service accepts creates. This cold subscription state begins only after
  service initialization and datastore-pool establishment are complete.

## Performance Measurement Profile

- Measurements run in a production-equivalent stream-monitoring process where
  the poller and reconciler share one event loop.
- The measured ranking input contains exactly the nominal count of eligible
  channels after clipping-eligibility filtering. The observed eligible count
  is recorded, and a run with fewer than 500 or 900 eligible records is invalid
  for that scale point.
- Complete-turnover measurements use two controlled, disjoint paginated
  ranking fixtures. Stable measurements repeat one fixture. Each fixture uses
  production page sizing, and its per-page delay is set to at least the p95
  live ranking-page latency measured from the same environment. This makes
  complete turnover reproducible without pretending that live rankings turn
  over completely between polls.
- The raw fixture includes a recorded representative proportion of
  clipping-disabled channels and enough pages to leave exactly 500 or 900
  eligible records after filtering. Before each scale run, the report records
  raw record count, page count, same-environment live p95 delay per page,
  page-count-times-delay ranking budget, and the target time remaining for all
  other poll phases. This budget is diagnostic; the full end-to-end target
  remains the acceptance gate.
- In the complete-turnover profile, newly ranked and departed online-state
  entries begin absent so the run includes the maximum `online` and `offline`
  lifecycle output allowed by its test-only thresholds. In the stable profile,
  all current online-state entries begin present.
- Test-only entry, retention, and fetch-capacity overrides MAY be used to admit
  the exact 500- and 900-record fixtures. FR-021 freezes deployed production
  configuration, not measurement-only inputs.
- The online-state and metadata stores are remote over Tailscale. Their
  request round trips are recorded separately during the run. Each store's
  representative median must be within the operator-observed 40-110 ms range;
  otherwise the result is reported but does not satisfy the acceptance profile.
- The service process and pooled connections are warm. Process startup and
  deployment time are excluded. A "cold start" in SC-010 means zero EventSub
  subscriptions after service initialization, not a cold process or unopened
  datastore pool.
- Poll timing begins immediately before ranking retrieval and ends after
  desired intent is published and the reconciler has been notified. It
  includes ranking pagination, filtering, datastore phases, lifecycle
  publication, and desired-set publication; it excludes scheduler queue delay
  before the poll starts and subscription convergence after notification.
- Each scale point and ranking profile uses one warm-up followed by 20
  completed polls, for 40 measured polls per scale point. Nearest-rank p95 is
  the 19th value after the 20 durations are sorted from fastest to slowest, so
  at most one measured poll may meet or exceed the target.
- No datastore, ranking-source, or message-broker failure is injected for the
  latency measurement. A poll that encounters ranking-source throttling,
  timeout retry, or another excluded failure is removed as a whole and
  replaced; no time is subtracted from a measured poll.
- Operation-count validation counts client-visible remote request/response
  interactions at the datastore client/connection dispatch boundary. Each SQL
  statement dispatch and transaction completion, each standalone Redis command
  request, and each Redis batch execution counts; a repository helper call
  does not. Individual commands carried inside one acknowledged Redis batch do
  not count as separate network cycles.
- Reconciler-gap validation records every pass completion in-process using a
  monotonic timestamp with one-second-or-better resolution. It does not infer
  pass cadence from the production metrics scrape interval.

## Assumptions & Dependencies

- Production currently uses entry threshold 150 and retention threshold 300.
  The online-state expiration is 180 seconds and the poll interval is 120
  seconds.
- Redis and Postgres remain the online-state/desired-state and metadata stores.
  Their clients remain synchronous in this feature.
- The existing desired-set read and atomic publication already have bounded
  interaction counts and retain their current behavior.
- Lifecycle publication to the local Kafka producer remains per event because
  it is not the observed initial bottleneck.
- The ranking source continues to paginate by channel count. This proportional
  external work is included in wall-clock measurement but is not part of the
  datastore request-count assertion.
- The reconciler's last-success signal denotes a completed pass. It may remain
  unchanged during one long cold-start pass that is actively retrying external
  throttling; the steady-state advancement requirement does not apply to that
  exception.
- The steady-state pass-gap budgets assume the existing 5-second idle wake-up,
  periodic re-adoption every 300 seconds, and the observed approximately
  2-4-second pass duration near 500 channels. These inputs are recorded during
  each validation run; exceeding the 15- or 20-second gap remains a failure
  rather than silently widening the budget.
- EventSub websocket transport capacity remains exactly 900 subscriptions with
  no headroom. Testing at 900 demonstrates the poller boundary, not spare
  transport capacity for operational churn.

## Out of Scope

- Package, Python, Flink, or dependency upgrades.
- Migration to asynchronous datastore clients, psycopg 3, or another
  scheduler.
- Moving the existing per-row datastore calls into threads or an executor.
- Changes to the reconciler's subscription-create rate-limit algorithm,
  backoff policy, or cold-start deadline.
- EventSub transport capacity changes or support beyond 900 websocket
  subscriptions.
- Production changes to entry threshold, retention threshold, or the
  clipping-disabled fetch buffer in the implementation change.
- Flink, clip-budget, anomaly-ranking, or clip-creation changes reserved for
  feature 005.
- General datastore schema redesign.

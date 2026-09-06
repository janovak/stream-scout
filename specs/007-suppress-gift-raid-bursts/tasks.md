# Tasks: Suppress Gift and Raid Chat Bursts

**Input**: Design documents from `specs\007-suppress-gift-raid-bursts\`
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`,
`contracts\suppression-events.schema.md`, `quickstart.md`,
`autonomous-decisions.md`

**Tests**: Required. For every behavior change below, write the listed
deterministic test first and confirm that it fails for the intended reason
before implementing the behavior. All validation in this ledger is offline;
do not start services or infrastructure or make live Twitch/API calls.

**Organization**: Shared contract and pool primitives are foundational. User
Story 3 then completes the capacity proof before the 400/400 configuration
change. User Story 4 supplies the pure deadline arithmetic needed by User
Story 1. User Story 1 completes producer work before consumer work. User Story
2 proves that the resulting gate changes output only.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can be worked in parallel with another identified task because it
  touches a different file and has no unmet dependency.
- **[Story]**: User story from `spec.md`; setup, foundational, and polish tasks
  intentionally have no story label.
- Every checklist item names the exact repository-relative file it changes or
  validates.

---

## Phase 1: Setup (Shared Test Infrastructure)

**Purpose**: Extend existing offline doubles and fixtures without changing
runtime behavior.

- [x] T001 [P] Extend `FakeWebsocket`, `FakePoolTwitch`, `existing_subscription`, and notification-envelope builders to model both EventSub coverage types, independent subscription ids, per-type failures, reconnect id rotation, and two list walks in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T002 [P] Add reusable valid and invalid version-1 suppression record builders plus fake keyed-state, timer, and metric contexts derived from `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\flink-job\test_clip_detector.py`
- [x] T003 [P] Add tagged chat/suppression input builders and state-trace assertion helpers for merged offline replays in `services\flink-job\test_replay.py`

**Checkpoint**: Existing test suites have the doubles needed to express all
Feature 007 failure modes without a broker, socket, Flink cluster, or Twitch
call.

---

## Phase 2: Foundational (Blocking Contract and Pool Primitives)

**Purpose**: Establish the versioned producer contract and the two-slot
in-memory model that every user story relies on.

**Critical**: No user-story implementation starts until this phase is
complete. Tests precede each implementation task.

- [x] T004 Add failing producer contract tests for all three trigger categories, excluded and unknown categories, required and optional field types, diagnostic-only `viewer_count`, absence of a computed deadline, and malformed identity/time behavior against `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T005 Implement the version-1 trigger allow-list, schema constant, and pure `map_suppression_event()` mapping with trustworthy identity/time validation and no window policy, following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md`, in `services\stream-monitoring\eventsub_pool.py`
- [x] T006 Add failing tests for one slot per `(broadcaster_id, coverage_type)`, independent chat/notification indexes, and derived `complete`, `chat_only`, `notification_only`, `absent`, and `degraded_chat_only` channel coverage in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T007 Implement `CoverageType`, type-bearing `_Slot`, channel coverage derivation, per-type lookup/forget helpers, and auxiliary-refusal bookkeeping in `services\stream-monitoring\eventsub_pool.py`
- [x] T008 Add failing routing and reservation tests for two-slot new-channel placement, one-slot repair, preference for a channel's existing connection, legal pair splitting, subscription-counted load, concurrent reservation safety, and the three-connection limit in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T009 Implement pair-aware `route()` and `_reserve()` primitives that reserve the actual number of subscriptions, never exceed 300 subscriptions per connection or three connections, and preserve rendezvous fallback behavior in `services\stream-monitoring\eventsub_pool.py`

**Checkpoint**: The producer payload shape is fixed and the pool can represent
and reserve both halves of a channel without confusing channel and
subscription units.

---

## Phase 3: User Story 3 - Maintain Complete, Capacity-Safe Channel Coverage (Priority: P1)

**Goal**: Every monitored channel converges to chat-message plus
chat-notification coverage while the pool stays within three 300-subscription
sessions and the desired set stays at or below 400 channels.

**Independent Test**: With fake Twitch and websocket objects, converge 400
channels to 800 independently tracked subscriptions, prove no connection
exceeds 300, prove at least 100 global slots remain, repair both partial-state
directions without duplication, prove a refused notification subscription holds
off retries for a bounded hour and then becomes repairable again, and refuse a
401st desired channel.

### Tests for User Story 3

- [x] T010 [US3] Add failing pair-creation and repair tests proving an absent channel creates exactly one subscription of each type on the existing operator user id, while `chat_only` and `notification_only` each create only the missing type and preserve the surviving id and pre-call session stamp in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T011 [US3] Add failing enumeration tests proving `list()` joins independent chat and notification walks, requires both types to be enabled on sessions held by the pool, reports both partial directions and `degraded_chat_only`, and marks enumeration incomplete when either walk fails so reconciler drops remain held back in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T012 [US3] Add failing 409 tests proving chat and notification conflicts each adopt only the matching `(type, broadcaster)` subscription on a live held session and refuse subscriptions on foreign or dead sessions in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T013 [US3] Add failing deletion tests proving a channel drop deletes both coverage types, treats either already-gone id as success, follows reconnect-rotated live ids per type, and retains correct surviving indexes after a one-sided delete failure for retry on the next pass in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T014 [US3] Add failing revocation tests proving a known or reconnect-rotated unknown id resolves to the correct channel and type, removes exactly that slot, retains the sibling slot, reports one lost subscription, and leaves a repairable partial state in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T015 [US3] Add failing reconnect and retirement tests for per-type session staleness, the chat-only `_connection_holds()` notification false-positive, independent live-id lookup, co-located and split pairs on socket death, and mid-ramp rebalance without oversubscription in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T016 [US3] Add failing refusal tests proving a notification 403 with live chat records `degraded_chat_only`, does not propagate a channel refusal or evict chat, suppresses repeated auxiliary creates for `AUXILIARY_REFUSAL_RETRY_SECONDS`=3600 and no longer, reports the channel as actual-but-degraded during the hold-off so the reconciler does not hot-loop, returns it to a repairable partial state after expiry, forces re-eligibility immediately on websocket reconnect or connection retirement, clears the state on successful creation or 409 adoption, and remains distinguishable from a chat-subscription refusal in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T017 [US3] Add failing capacity and units tests proving 150 complete channels fill one 300-subscription session, channel 151 routes onward, 400 complete channels use 800 of 900 subscriptions with 100 free, a 401st candidate is not admitted, occupancy counts subscriptions, and coverage/desired metrics count channels in `services\stream-monitoring\test_stream_monitoring.py`

### Implementation for User Story 3

- [x] T018 [US3] Make `EventSubPoolTransport.create()` register `channel.chat.message` and `channel.chat.notification` with separate callbacks, create only missing types, and stamp each slot with the session read before its own listen call in `services\stream-monitoring\eventsub_pool.py`
- [x] T019 [US3] Make `EventSubPoolTransport.list()` perform and join two type-filtered walks, adopt only enabled subscriptions on held sessions, expose complete/partial/degraded channel coverage, and propagate either walk's incompleteness without changing the channel-keyed reconciler diff in `services\stream-monitoring\eventsub_pool.py`
- [x] T020 [US3] Make `_adopt_conflict()` type-aware for either 409 path and rebuild the correct slot and subscription indexes only from a matching broadcaster, type, and live held session in `services\stream-monitoring\eventsub_pool.py`
- [x] T021 [US3] Make `delete()` and `_live_subscription_ids()` delete both channel coverage types independently, preserve retryable partial state on one-sided failure, and remove only matching type-specific library callbacks and indexes in `services\stream-monitoring\eventsub_pool.py`
- [x] T022 [US3] Make `_forget_revoked()` and `_forget_unrecognised()` resolve broadcaster plus subscription type, remove exactly one slot even after reconnect id rotation, retain the sibling slot, and invalidate the reconciler by the number of subscriptions actually lost in `services\stream-monitoring\eventsub_pool.py`
- [x] T023 [US3] Make `_connection_holds()`, `_slot_is_current()`, reconnect cleanup, and `_retire()` type-aware so rotated ids and session changes are evaluated independently and all slots on a dead connection are removed without deleting split siblings elsewhere in `services\stream-monitoring\eventsub_pool.py`
- [x] T024 [US3] Classify an auxiliary notification refusal with live chat as pool-local degraded coverage bounded by `AUXILIARY_REFUSAL_RETRY_SECONDS` (default 3600, read from the environment): hold off further notification creates only until the deadline passes, report the channel as actual-but-degraded while it holds, return it to ordinary partial-state repair on expiry, force re-eligibility on websocket reconnect or connection retirement, clear the state on successful creation or re-adoption, keep chat refusal behavior unchanged, and expose the degradation without writing the reconciler's seven-day channel refusal in `services\stream-monitoring\eventsub_pool.py`
- [x] T025 [US3] Publish `eventsub_channel_coverage{state}` in channel units, keep `eventsub_connection_occupancy` in per-connection subscription units, derive `eventsub_subscription_count` from transport occupancy rather than channel count, keep `active_stream_count` channel-based, and retain distinct capacity-refusal signals in `services\stream-monitoring\eventsub_pool.py`, `services\stream-monitoring\reconciler.py`, and `services\stream-monitoring\stream_monitoring_service.py`
- [x] T026 [US3] Run the deterministic pool and desired-set capacity tests through the 400-channel, 800-subscription, 150-channels-per-session, reconnect, partial-state, and 401st-refusal cases in `services\stream-monitoring\test_stream_monitoring.py`; this passing proof is the hard prerequisite for T028
- [x] T027 [US3] After T026 passes, add failing static/runtime tests for checked-in 400/400 thresholds, a checked-in `AUXILIARY_REFUSAL_RETRY_SECONDS=3600`, a 400-entry maximum desired set, the accepted zero-width band behavior, and `desired_set_churn_total` incrementing by entered plus departed channels with no per-channel label growth so the NFR-007 bound of 8 changes per poll is directly computable in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T028 [US3] After T026 and T027, set `JOIN_THRESHOLD=400`, `LEAVE_THRESHOLD=400`, and `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` with the 400-channel/800-subscription/100-headroom explanation in `docker-compose.yml`, and implement bounded `desired_set_churn_total` accounting in `services\stream-monitoring\stream_monitoring_service.py`, recording in the compose comment that the NFR-007/SC-011 24-hour churn bound is deployed evidence (E2/B4) and is not claimed here

**Checkpoint**: User Story 3 is complete and independently testable. The
400/400 edit is forbidden until T026 has proved the two-slot pool's capacity
behavior.

---

## Phase 4: User Story 4 - Extend Overlapping Suppression Predictably (Priority: P2)

**Goal**: Relevant notices update a per-channel notice-bounded half-open
interval using operator-configured gift and raid windows.

**Independent Test**: Apply gifts and raids before, exactly at, and after an
existing deadline: overlapping extensions preserve the earliest retained
start, earlier/equal candidates are complete-state no-ops, and notices at/after
the deadline start new intervals. Duplicate application is idempotent and
arbitrary ordering preserves the maximum deadline without claiming an
identical lower bound. Defaults remain 120 seconds for gifts and 180 seconds
for raids, with no viewer-count influence.

### Tests for User Story 4

- [x] T029 [P] [US4] Add failing tests for `SuppressionConfig` defaults and environment overrides, `SuppressionState` JSON round trips including `suppress_from_ms` and `suppress_until_ms`, separate gift/raid windows, rejected unknown categories, the rule that raid `viewer_count` cannot affect duration, fixed non-environment `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`, and the pure `SuppressionSourceSettings` construct — `suppression-events` topic name, latest-offset mode, bounded out-of-orderness equal to `WATERMARK_OUT_OF_ORDERNESS_SECONDS`, `SUPPRESSION_IDLENESS_SECONDS=5` strictly below chat's 10 seconds, four expected partitions matching expected parallelism four, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` — plus static `docker-compose.yml` assertions for both Flink environment blocks, checked-in gating false, lag warning, partitions/parallelism, absence of a future-skew environment variable, and unchanged `FLINK_PYFILES`, all without importing PyFlink, in `services\flink-job\test_spike_detector.py`; author these failing compose assertions during US4, but do not close T029 until T046 writes the asserted Flink variables
- [x] T030 [US4] Add failing pure-function tests for notice-bounded half-open intervals: overlapping later-deadline extension preserves/minimizes the start, earlier/equal-deadline input leaves the complete state unchanged, occurrence exactly at or after the old deadline starts a new interval, duplicate application is idempotent, arbitrary ordering preserves the maximum deadline without asserting identical lower bounds, simultaneous gift/raid notices follow the same rules, absent state fails open, a pre-notice peak remains eligible even when reported later, a peak exactly at the notice is gated, and a peak exactly at the deadline is eligible in `services\flink-job\test_spike_detector.py`

### Implementation for User Story 4

- [x] T031 [US4] Implement `SuppressionConfig.from_env()` as the runtime reader with 120-second gift and 180-second raid defaults, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and a `SUPPRESSION_GATING_ENABLED` code default of `true`; define fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` with no environment lookup; add JSON-backed `SuppressionState` containing `suppress_from_ms` and `suppress_until_ms`; and create the pure `SuppressionSourceSettings` construct carrying topic, latest-offset mode, out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS=5`, expected partitions/parallelism, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` so the job builds its source from it instead of inline literals, all with no PyFlink import, in `services\flink-job\spike_detector.py`
- [x] T032 [US4] Implement pure `apply_notice()` interval transitions and `is_suppressed()` using `suppress_from_ms <= peak_ms < suppress_until_ms`: overlapping extension preserves/minimizes the start, earlier/equal candidate is a complete-state no-op, and notice occurrence at/after the old deadline starts a new interval, without timers, I/O, viewer-count logic, or changes to `evaluate()` in `services\flink-job\spike_detector.py`

**Checkpoint**: User Story 4's arithmetic and pure settings are independently
testable as pure Python and may be implemented in parallel with User Story 3
after the foundational phase. T029 authors the static compose assertions here,
but those assertions intentionally remain failing and T029 remains open until
T046 writes both Flink environment blocks.

---

## Phase 5: User Story 1 - Avoid Low-Value Gift and Raid Clips (Priority: P1)

**Goal**: Publish trustworthy trigger notices and consume them as a keyed,
fail-open, output-only clip gate with complete observability.

**Independent Test**: Feed contract-valid gift/raid notices and matching chat
spikes through deterministic producer and operator doubles. Spikes whose peaks
are inside notice-bounded half-open intervals produce no clip but do produce
exactly one suppression metric and structured log; excluded, malformed,
over-future, absent, disabled, or late notices do not retract or incorrectly
suppress output.

### Producer Tests for User Story 1

- [x] T033 [US1] Add failing pool-to-service callback and publisher tests for exact version-1 JSON, the producer invariant that the Kafka key is always `str(broadcaster_id)` and equals the payload `broadcaster_id` (asserted here because the producer is the only party that observes both), the three trigger types, ignored-category buckets in `suppression_notices_ignored_total`, malformed identity/time counters and structured logs, `received_at_ms`, delivery callback/poll behavior, and produce-exception containment against `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\test_stream_monitoring.py`
- [x] T034 [US1] Add failing static tests that `suppression-events` has four partitions and one-hour retention and that the producer uses that exact topic without modifying `chat-messages` in `services\stream-monitoring\test_stream_monitoring.py`

### Producer Implementation for User Story 1

- [x] T035 [US1] Wire the pool's notification callback into `_on_eventsub_notification()`, publish contract-valid events through `_publish_suppression_event()`, and add bounded ignored/malformed metrics and structured failure logs following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\stream_monitoring_service.py`
- [x] T036 [US1] After T034, create `suppression-events` with four partitions, replication factor one, and `retention.ms=3600000` beside the existing topics without changing `chat-messages` in `docker-compose.yml`

### Consumer Tests for User Story 1

- [x] T037 [US1] After T036, add failing consumer tests for valid version-1 records, invalid JSON, non-object JSON, missing/unknown `schema_version`, wrong field types, excluded categories, diagnostic-only optional fields, fixed future-time trust at exactly `consumer_receipt_ms + 30_000` and rejection one millisecond beyond as `reason="fields"`, routing and state keyed off the payload `broadcaster_id` because the value-only deserializer never exposes the Kafka key, and no exception escape, following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md`, in `services\flink-job\test_clip_detector.py`; do not assert key/payload agreement here — that invariant belongs to T033
- [x] T038 [US1] Add failing tests that the second `suppression-events` source is built from the pure `SuppressionSourceSettings` of T031 — payload-`broadcaster_id` keying, bounded out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS=5` strictly below chat's 10 seconds, latest offsets, and four topic partitions matching `FLINK_PARALLELISM=4` — and that `SuppressionTimestampAssigner` applies fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` before watermark generation using the same injectable/current receipt/source wall-clock basis as the operator: exact +30,000 ms assigns parsed `occurred_at_ms`, while +30,001 ms and missing/unreadable values assign Kafka `record_timestamp` without mutating the payload. Per autonomous decisions 25-26, assert for **both** chat and suppression that `from_source` receives `WatermarkStrategy.no_watermarks()`, the real strategy is built in the binding order bounded out-of-orderness → `with_idleness()` → `with_timestamp_assigner()` last, and the returned stream receives `assign_timestamps_and_watermarks(real_strategy)`. Assert `SentAtTimestampAssigner` accepts only plain `int` (not `bool`) `sent_at` through +30,000 ms and falls back to Kafka `record_timestamp` for missing/null/string/float/bool/+30,001 ms without mutating or dropping chat. Keep the four-partition/source-parallelism/assignment-parallelism one-to-one chain asserted statically. The guaranteed-offline half remains in `test_spike_detector.py`; the conditional fake-wiring half remains in `test_clip_detector.py`, starts no cluster, and must not be reported as proof that the real chain executes either assigner or of the two added Python stages' process/RSS cost, which remain E3
- [x] T039 [US1] Add failing operator tests proving `process_element2()` updates only the current broadcaster's suppression interval, writes only for a new interval or extension, registers no timer, emits no output, counts rejected records by reason, and captures `consumer_receipt_ms` from an injected clock at receipt. Prove the fixed future-time trust check runs after schema/field decode but before delivery observation or state access: exactly +30,000 ms is accepted, clamped to age zero, classified healthy, and clock-skew logged; +30,001 ms is rejected as malformed fields, counted/logged, and produces no delivery sample, consumed classification, state read, or state write. For other accepted records compute `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`, classify healthy at or below `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS`=30 and lagging above it, never use optional diagnostic-only `received_at_ms`, cover a small producer delay with consumer delay above 30 seconds, leave silence idle/unknown, use TTL with `NeverReturnExpired`, and treat absent/expired state as fail-open in `services\flink-job\test_clip_detector.py`
- [x] T040 [US1] Add failing `on_timer()` tests proving the gate uses the spike peak second and exact half-open predicate `suppress_from_ms <= peak_ms < suppress_until_ms`: a pre-notice peak remains eligible even when its hold reports after the notice, a peak at the notice is gated, and a peak at the deadline is eligible. Also prove the gate honors `SUPPRESSION_GATING_ENABLED`, preserves normal output when inactive, still updates `last_fire_second` and `anomalies_detected_total`, emits exactly one attributable suppression metric and structured log when active, and never retracts an output when a notice arrives late in `services\flink-job\test_clip_detector.py`

### Consumer Implementation for User Story 1

- [x] T041 [US1] Implement defensive version-1 decoding, payload field validation, trigger allow-list enforcement, and `SuppressionTimestampAssigner` following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\flink-job\clip_detector_job.py`. Before watermark generation, compare parsed `occurred_at_ms` with `source_clock_ms + SUPPRESSION_MAX_FUTURE_SKEW_SECONDS * 1000` using the same injectable/current receipt/source wall-clock basis as the operator; assign occurrence time through equality, otherwise assign Kafka `record_timestamp` as already done for missing/unreadable values, without filtering or rewriting the payload. Per contract §1.1.1 and autonomous decisions 25-26, build the suppression strategy bounded out-of-orderness → `with_idleness()` → `with_timestamp_assigner()` last, then make the assigner reachable by passing `WatermarkStrategy.no_watermarks()` to `env.from_source` and attaching the real strategy post-source. In `process_element2`, after schema/field decode and before delivery observation or state access, reject the unchanged original `occurred_at_ms > consumer_receipt_ms + SUPPRESSION_MAX_FUTURE_SKEW_SECONDS * 1000` as `reason="fields"` with the existing warning; derive routing and state from payload `broadcaster_id` and implement no Kafka-key comparison, because the value-only deserializer never surfaces the key to the operator
- [x] T042 [US1] Register the feature's consumer-side signals with bounded reason/category labels in `services\flink-job\clip_detector_job.py`: `suppression_records_rejected_total{reason}` including over-bound future timestamps as `reason="fields"` with a structured malformed log and no delivery/state side effects; `suppression_records_consumed_total{lag_class="healthy"|"lagging"}` plus `suppression_delivery_age_seconds`, observed once per trusted received record from `max(0, consumer_receipt_ms - occurred_at_ms)` using the injected/current consumer clock, and a structured log for lagging records; clamp accepted negative raw age to zero and emit a structured clock-skew diagnostic log; keep optional `received_at_ms` diagnostic-only; publish nothing for a window containing no record so silence reads as idle/unknown rather than healthy; and retain `clips_suppressed_total{broadcaster_id, notice_type}` with the `broadcaster_id` label NFR-006 requires under the same finite monitored-channel label policy as `anomalies_detected_total`. Preserve `anomalies_detected_total` unchanged and add the channel-attributable structured suppression log. Only reason/category labels need bounding; do not drop broadcaster attribution and do not add a continuously refreshed per-channel delivery gauge driven from `on_timer`
- [x] T043 [US1] Convert `AnomalyDetector` to `KeyedCoProcessFunction`, retain the chat body as `process_element1()`, register JSON `suppression` `ValueState` under the existing TTL policy, and implement no-timer/no-output `process_element2()` with writes only for a new interval or an overlapping extension and no write for an earlier/equal candidate in `services\flink-job\clip_detector_job.py`
- [x] T044 [US1] Add the post-state-write output gate at the final `yield`, keyed off `spike.detected_at_seconds` and the half-open notice-bounded interval `suppress_from_ms <= peak_ms < suppress_until_ms`, so pre-notice and exact-deadline peaks remain eligible, while preserving bucket expiry, hold writes, chain timers, `last_fire_second`, anomaly counting, fail-open behavior, and the kill switch in `services\flink-job\clip_detector_job.py`
- [x] T045 [US1] Build the second Kafka source from `SuppressionSourceSettings` with latest offsets and the suppression watermark/idleness strategy, key both inputs on payload `broadcaster_id`, and connect them into `AnomalyDetector` without changing `CommandFilter` or `ClipCreator` in `services\flink-job\clip_detector_job.py`. For both streams, build the real strategy bounded out-of-orderness → `with_idleness()` → `with_timestamp_assigner()` last, enter through `from_source(..., WatermarkStrategy.no_watermarks(), ...)`, and attach the real strategy post-source. Harden live `SentAtTimestampAssigner` to accept only plain `int` (not `bool`) `sent_at <= source_clock_ms + SUPPRESSION_MAX_FUTURE_SKEW_SECONDS * 1000`; missing/null/string/float/bool/+30,001 ms falls back to Kafka `record_timestamp` without rewriting, rejecting, or dropping chat. Keep the topology one-to-one at partitions = source parallelism = assignment parallelism = 4; the resulting two Python stages at parallelism four require deployed process-count/RSS evidence only (autonomous decisions 25-26, research §4.1.2, R13-R14)
- [x] T046 [US1] Add `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and `SUPPRESSION_GATING_ENABLED=false` to both `flink-jobmanager` and `flink-taskmanager` environment blocks while leaving `FLINK_PYFILES` unchanged and adding no environment variable for fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` in `docker-compose.yml`; the checked-in gating value is `false` even though the `SuppressionConfig` code default is `true`, with a comment stating it is changed to `true` operationally only after E1-E3 and the 24-hour E2 churn observation pass, then close T029 by making its previously authored static compose assertions pass
- [x] T047 [US1] Run the offline producer, contract, source-settings, malformed-input including exact/+1 ms future-trust boundaries and no-observation/no-state rejection, fail-open, late-notice and pre-notice-peak, delivery-classification, and half-open output-gate unit selections in `services\stream-monitoring\test_stream_monitoring.py`, `services\flink-job\test_spike_detector.py`, and `services\flink-job\test_clip_detector.py`; the non-skippable evidence for this checkpoint is the stream-monitoring selection plus `test_spike_detector.py`, which import no PyFlink, and `test_clip_detector.py` counts only when the pinned `apache-flink==1.18.0` is already installed — report it as pending rather than passed when it is absent, and never treat its absence as covered by the pure suites

**Checkpoint**: User Story 1 is independently testable without Kafka, Flink,
Twitch, or any application service. Producer contract work T004-T005 and
T033-T036 must be complete before consumer work T037-T046. The guaranteed
evidence for this checkpoint is PyFlink-free: the stream-monitoring selections
plus `test_spike_detector.py`. `test_clip_detector.py` adds the operator and
wiring assertions when the pinned `apache-flink==1.18.0` is installed, and is
reported pending when it is not.

---

## Phase 6: User Story 2 - Preserve Detector Learning During Suppression (Priority: P1)

**Goal**: Prove that suppression changes only clip emission and its required
signals, never message-derived detector state.

**Independent Test**: Replay the same messages twice, with and without notices
covering the same spikes, and compare every per-second count, baseline reading,
hold transition, expired-bucket sequence, and `last_fire_second` transition.
Only clip output and suppression metric/log records may differ.

### Tests for User Story 2

- [x] T048 [US2] Add failing tests that the replay harness accepts a deterministic merged sequence of chat and version-1 suppression records, maintains suppression independently per broadcaster, and applies notices at their delivery position rather than retroactively in `services\flink-job\test_replay.py`
- [x] T049 [US2] Add failing gated-versus-ungated equivalence tests for identical message counts, baseline readings, hold open/peak/close trajectories, sorted expired buckets, and `last_fire_second`, with differences limited to clip output plus one suppression metric/log per peak covered by `[suppress_from_ms, suppress_until_ms)`; include a pre-notice peak whose hold reports after the notice and remains identical and emitted in both runs in `services\flink-job\test_replay.py`
- [x] T050 [US2] Add failing deterministic acceptance cases for a suppression input quiet for the whole replay, sparse notices, an isolated notice arriving after a long silence that holds the harness's simplified two-input watermark by no more than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before the input is idle again while sustained notice traffic advances it normally, a late notice with no retraction but a later unexpired effect, exact notice/deadline and pre-notice-peak boundaries, overlapping-chain start/deadline transitions, and exact/+1 ms future-trust behavior. Replay must mirror source timestamp assignment before downstream validation: exact +30,000 ms uses occurrence time; +30,001 ms uses Kafka record time without payload mutation, is then rejected with no observation/state, and cannot advance the combined watermark from the untrusted value; the combined watermark remains monotonic through later chat idle/resume. Also cover cross-channel isolation, inactive/expired state, and byte-identical repeated output in `services\flink-job\test_replay.py`; the simplified model is offline evidence only, it invokes the assigner directly and therefore cannot show that the real job reaches its assigner through `assign_timestamps_and_watermarks`, and deployed timestamp/watermark behavior remains E3/E4

### Implementation for User Story 2

- [x] T051 [US2] Extend `EventTimeReplayer` with per-key `SuppressionState`, merged delivery-order notice application, and the same output-only peak-second gate used by the job while retaining all existing chat timer and state behavior in `services\flink-job\tools\replay.py`
- [x] T052 [US2] Expose deterministic state and suppression side-effect traces from the replay harness so tests can compare counts, readings, holds, expiry, cooldown, emitted clips, metrics, and logs without changing the existing human-readable unsuppressed output in `services\flink-job\tools\replay.py`

**Checkpoint**: User Story 2 supplies the decisive offline SC-004 proof and
the simplified quiet-input, sparse idle-to-active re-entry, and late-arrival
evidence without claiming PyFlink runtime watermark behavior.

---

## Phase 7: Polish and Cross-Cutting Closure

**Purpose**: Complete locally verifiable integration, runbook, rollback, and
scope guards without turning deployed evidence into local tasks.

- [x] T053 [P] Document the 400-channel ceiling, two coverage types, coverage/subscription units, the bounded auxiliary-refusal hold-off and its expiry/reconnect recovery, the suppression topic and windows, every new metric and log — `clips_suppressed_total{broadcaster_id, notice_type}`, `eventsub_channel_coverage{state}` including `degraded_chat_only`, `suppression_notices_ignored_total`, `suppression_notices_malformed_total`, `suppression_records_rejected_total`, `suppression_records_consumed_total{lag_class}`, `suppression_delivery_age_seconds` from `max(0, consumer_receipt_ms - occurred_at_ms)` with clock-skew clamp/log semantics and optional `received_at_ms` diagnostic decomposition, and `desired_set_churn_total` — the Prometheus windowed reading of healthy/lagging/idle-unknown delivery, fail-open diagnosis, the NFR-007 churn bound and its release disposition, the staged rollout with `SUPPRESSION_GATING_ENABLED=false` checked in and enabled only after E1-E3 and the 24-hour E2 churn observation, the operator kill switch, the capacity-safe rollback order (gating off, unwind the transport while thresholds stay at 400/400, wait until notification subscriptions are gone and total subscriptions fall to roughly the desired channel count with stable coverage metrics, only then raise thresholds, never above 400 while any notification subscription remains), and an explicitly pending operator-run E1-E5 checklist in `OPERATIONS.md`
- [x] T054 [P] Verify the feature adds no dependency, database, Redis-layout, chat-schema, application-identity, authorization-scope, `FLINK_PYFILES`, or Dockerfile module-copy change by reviewing `services\stream-monitoring\Dockerfile`, `services\stream-monitoring\requirements.txt`, `services\flink-job\Dockerfile`, `services\flink-job\requirements.txt`, and `docker-compose.yml`
- [x] T055 [P] Run the complete locally permitted stream-monitoring unit suite and resolve only Feature 007 regressions in `services\stream-monitoring\test_stream_monitoring.py` and `services\stream-monitoring\test_desired_set_store.py`
- [x] T056 [P] Run the complete locally permitted Flink pure/unit suite and resolve only Feature 007 regressions in `services\flink-job\test_spike_detector.py`, `services\flink-job\test_clip_detector.py`, `services\flink-job\test_replay.py`, and `services\flink-job\test_clip_attempt.py`; `test_spike_detector.py`, `test_replay.py`, and `test_clip_attempt.py` import no PyFlink and are the non-skippable evidence, while `test_clip_detector.py` runs only when the pinned `apache-flink==1.18.0` is already installed and is reported as pending, not passed, when it is absent
- [x] T057 Run the suppression replay twice over the same checked-in deterministic fixture and require byte-identical output plus identical state traces in `services\flink-job\tools\replay.py` and `services\flink-job\test_replay.py`
- [x] T058 Complete the final FR-001..FR-018, NFR-001..NFR-007, and SC-001..SC-011 traceability audit, including the final-review corrections that both real strategies use the binding order bounded out-of-orderness → idleness → assigner last and are attached post-source; suppression applies source fallback then downstream rejection, while chat accepts only plain-int/non-bool `sent_at` through +30 seconds and otherwise falls back to Kafka record time without dropping chat (autonomous decisions 25-26). Confirm T038/T041/T045/T050 cover attachment, builder order, both source assignments, suppression rejection, chat no-data-loss, and replay monotonicity; confirm partitions = source parallelism = assignment parallelism = 4 wherever idleness is claimed; reserve proof of the two parallelism-four Python stages and TaskManager process/RSS impact for E3; retain strict task syntax, all 58 task IDs and existing completion marks, and keep E1-E5 pending

**Checkpoint**: All local implementation, deterministic tests, replay, static
configuration checks, and operator documentation can be complete while E1-E5
remain pending.

---

## Phase 8: Approved Capacity Amendment (Decisions 27-28)

**Purpose**: Carry the approved capacity amendment — entry `JOIN_THRESHOLD=400`,
retention-and-maximum `LEAVE_THRESHOLD=450`, exact 900-of-900 subscription
occupancy with no guaranteed reserve, and the exact-capacity engineering that
makes it safe. This is plan phase 9; it depends on phases 1-7 and precedes the
deployed validation in plan phase 8.

**Tests**: Required, and first. T060 and T061 must be observed failing for the
intended reason before T062 and T063 change behaviour or configuration, exactly
as T027 preceded T028.

- [x] T059 Record the approved capacity amendment in the specification artifacts — autonomous decisions 27 and 28, supersession banners on decisions 5, 14, and 19, amendment banners on decisions 16 and 21, rewritten FR-013/FR-014/FR-015, NFR-001, NFR-007, SC-006, SC-011, overview, User Story 3, edge cases, entities, assumptions and out-of-scope text, the entry-400/maximum-450/900-steady/0-free capacity tables, the split-reservation placement rule, the capacity-error and `full_at` rules, the fragmentation/foreign-subscription/below-cap-`full_at`/failed-delete/exact-convergence risks, and the staged rollout and rollback — in `specs\007-suppress-gift-raid-bursts\spec.md`, `specs\007-suppress-gift-raid-bursts\plan.md`, `specs\007-suppress-gift-raid-bursts\research.md`, `specs\007-suppress-gift-raid-bursts\data-model.md`, `specs\007-suppress-gift-raid-bursts\quickstart.md`, and `specs\007-suppress-gift-raid-bursts\autonomous-decisions.md`
- [ ] T060 [US3] Add failing threshold and ceiling tests for entry 400 with retention-and-maximum 450: a channel absent from the previous set and ranked 401-450 does not enter, an incumbent at the same rank is retained, a channel beyond rank 450 leaves, the desired set never exceeds 450, 450 complete channels occupy exactly 900 subscriptions with no connection above 300 and zero free slots, and the 451st qualifying channel is excluded at the intent layer, in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T061 [US3] Add failing exact-capacity behaviour tests for slot fragmentation and legibility: a pair placed as one slot on each of two connections when no connection holds two free but the pool holds at least two, atomic all-or-nothing reservation of that pair under concurrency, retention of the successful half after a post-reservation failure, replacement of a lost split fragment converging back to complete without exceeding 900, reconnect and adoption consuming no additional slot, a hard capacity exhaustion raising the distinct capacity error with its own metric classification rather than a provider refusal or transient fault and without arming the transient growth backoff or writing the durable refusal cache, and `full_at` being cleared and re-evaluated on reconnect and retirement with below-cap full state exposed, in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T062 [US3] After T060 and T061 fail for the intended reasons, implement co-location-first placement with the atomic two-connection split-pair reservation fallback, the distinct `PoolCapacityError` and its capacity metric classification, suppression of transient growth backoff for a hard capacity condition, and `full_at` clearing, re-evaluation, and below-cap exposure, keeping `reconciler.py` channel-keyed and unchanged, in `services\stream-monitoring\eventsub_pool.py`
- [ ] T063 [US3] After T062 passes, set the checked-in `JOIN_THRESHOLD=400` and `LEAVE_THRESHOLD=450` with a comment stating the entry/retention split, the 450 × 2 = 900 exact ceiling, the absence of a guaranteed reserve, and the requirement to deploy with the retention threshold overridden to `400` until E1 and E2a pass, in `docker-compose.yml`, and align the runtime capacity constants and comments with the amended model in `services\stream-monitoring\eventsub_pool.py` and `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T064 Keep `desired_set_churn_total` as advisory bounded-label telemetry and remove every numeric release gate attached to it — the 2%/8-changes-per-poll bound, the 24-hour observation window, and any assertion or comment treating it as blocking — in `services\stream-monitoring\stream_monitoring_service.py` and `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T065 Update the operator runbook for the amended capacity model: entry 400 and maximum 450, exact 900-of-900 occupancy with no reserve, the connection-full and capacity-classification signals, the advisory churn reading with no release gate, the staged rollout (400/400 single-subscription convergence, dual transport at 400/400 with gating off, E1 and E2a, account-wide foreign-subscription sweep, ramp to the checked-in 400/450, E2b exact-capacity drills read as relational equality, then E3, E4, E5), and the capacity-safe rollback that lowers retention to 400 and reconverges before the transport is unwound, in `OPERATIONS.md`
- [x] T066 Update the requirements checklists and the requirements-to-task traceability for the amended requirements and the T059-T068 phase, including the supersession map from the original ledger, in `specs\007-suppress-gift-raid-bursts\checklists\requirements.md`, `specs\007-suppress-gift-raid-bursts\checklists\requirements-quality.md`, and `specs\007-suppress-gift-raid-bursts\tasks.md`
- [ ] T067 Run the complete locally permitted suites after the amendment and resolve only amendment regressions, then obtain fresh reviews of the changed surfaces, in `services\stream-monitoring\test_stream_monitoring.py`, `services\stream-monitoring\test_desired_set_store.py`, `services\flink-job\test_spike_detector.py`, `services\flink-job\test_replay.py`, and `services\flink-job\test_clip_detector.py`, reporting the PyFlink-importing file as pending rather than passed when the pinned `apache-flink==1.18.0` is absent
- [ ] T068 Update the pull request #55 summary and its decision log with the approved capacity amendment: the entry-400 / retention-450 model and its exact analogy to the pre-007 800/900 ramp, the removal of the guaranteed reserve, decision 28's exact-capacity engineering, the removal of decision 19's churn release gate, the staged rollout and amended rollback order, and the still-pending deployed evidence E1, E2a, E2b, E3, E4, and E5

**Checkpoint**: The amended capacity model is specified, proved by
deterministic tests, implemented, configured, documented, and reviewed, while
every deployed gate remains pending.

---

## Dependencies and Execution Order

### Phase and Story Dependency Graph

```text
Phase 1 Setup
    |
    v
Phase 2 Foundational contract + pool primitives
    | \
    |  \------------------------------.
    v                                 v
US3 complete pool + capacity proof   US4 pure suppression arithmetic
    |                                 |
    |-- T026 capacity proof           |
    |-- T027 config tests              |
    '-- T028 400/400 change            |
              \                       /
               v                     v
            US1 producer T033-T036
                       |
                       v
            US1 consumer T037-T047
                       |
                       v
                 US2 replay proof
                       |
                       v
              Polish/local closure
                       |
                       v
        Capacity amendment T059-T068 (decisions 27-28)
```

### Hard Safety Dependencies

- T004 precedes T005; the producer implementation is defined by
  `contracts\suppression-events.schema.md`, not by consumer assumptions.
- T006 precedes T007, and T008 precedes T009; tests establish the two-slot and
  subscription-unit invariants before the primitives change.
- T010-T017 precede T018-T025. T026 must pass after those implementations.
- T028 must not start until T026 has passed and T027 has introduced the failing
  400/400 and churn expectations.
- US4's T029-T032 may proceed alongside US3 after Phase 2, and T032 must finish
  before the US1 consumer uses the arithmetic. T029 authors its failing static
  compose assertions during US4, but **T046 is a hard closure dependency for
  T029** because only T046 writes the asserted Flink variables; T029 must not
  be marked complete while those assertions still fail.
- All producer and topic tasks T033-T036 precede every consumer task
  T037-T046.
- T037-T040 precede their consumer implementations T041-T046.
- US2 depends on completed US1 because it compares the implemented job gate
  with the offline harness.
- T053-T058 close only after the desired story phases are complete.
- T059 precedes T060-T068: the amended requirements are written before work is
  proved against them.
- T060 and T061 precede T062, and T062 precedes T063 — the amended capacity
  behaviour is proved by failing deterministic tests before the pool changes,
  and the pool change lands before the checked-in retention threshold moves to
  450. This is the same ordering rule that made T026/T027 precede T028, and it
  must not be relaxed.
- T064 may proceed in parallel with T060-T063; it touches the churn accounting
  and its assertions only.
- T065-T067 close after T063 and T064. T068 closes last.
- No amendment task closes any deployed evidence. E1, E2a, E2b, E3, E4, and E5
  remain pending operator runs, and the deployed ramp to retention 450 happens
  only after E1 and E2a.

### Logical Commit Boundaries

1. **EventSub pool**: T001, T006-T026.
2. **Producer, configuration, and capacity**: T004-T005, T027-T028,
   T033-T036.
3. **Flink detector**: T002, T029-T032, T037-T047.
4. **Replay, operations, and integration**: T003, T048-T058.
5. **Capacity amendment**: T059-T068 — artifacts (T059), failing tests
   (T060-T061), pool implementation (T062), configuration (T063), churn
   telemetry (T064), operations and closure (T065-T068).

---

## Parallel Execution Examples

### Setup

```text
Parallel: T001 in services\stream-monitoring\test_stream_monitoring.py
Parallel: T002 in services\flink-job\test_clip_detector.py
Parallel: T003 in services\flink-job\test_replay.py
```

### User Story 3 and User Story 4

```text
After Phase 2:
Worker A: T010-T026 for US3 in stream-monitoring
Worker B: author T029 and complete T030-T032 for US4 in
          spike_detector.py/test_spike_detector.py; leave T029 open on its
          expected failing compose assertions until T046

Do not begin T027-T028 until Worker A completes T026.
T033-T036 follow T028; T028 does not depend on them.
```

### User Story 1

```text
Sequential producer lane: T033 -> T034 -> T035 -> T036
Then sequential consumer lane: T037-T040 -> T041-T046 -> T047

The lanes are intentionally not parallel: producer contract/topic work must
precede consumer work.
```

### User Story 2 and Polish

```text
Replay TDD lane: write T048-T050, then implement T051-T052
After all stories: T053 OPERATIONS.md can run in parallel with T054 scope review.
T055 and T056 may run in parallel because they use separate service test suites.
T057 follows the replay implementation; T058 follows every local closure task.
```

---

## Requirements-to-Task Traceability

| Requirement | Tasks |
|---|---|
| FR-001 | T006-T007, T010-T011, T016, T018-T019, T024, T026 |
| FR-002 | T006-T007, T010-T015, T018-T024 |
| FR-003 | T004-T005, T033-T036 |
| FR-004 | T004-T005, T010, T018, T033-T036 |
| FR-005 | T004-T005, T029-T032, T033-T037, T041, T047 |
| FR-006 | T029-T032, T039-T044, T046, T048-T052 |
| FR-007 | T040, T044, T046-T047, T050-T052 |
| FR-008 | T040, T044, T049-T052 |
| FR-009 | T040, T044, T046, T050-T052 |
| FR-010 | T031-T032, T039, T043, T048-T052 |
| FR-011 | T011, T016, T019, T024-T025, T035, T039-T044, T046-T047, T050-T053 |
| FR-012 | T040, T042, T044, T047, T050-T053 |
| FR-013 | T017, T026-T028, T053 → **amended**: T059-T060, T063, T065-T067 |
| FR-014 | T008-T009, T017, T026, T028 → **amended**: T059-T063, T065-T067 |
| FR-015 | T017, T025-T026, T053 → **amended**: T059-T063, T065-T067 |
| FR-016 | T010, T018, T053-T054 |
| FR-017 | T004-T005, T029-T031, T033-T035, T037, T039, T041-T042, T046-T047, T050 |
| FR-018 | T040, T044, T048-T052 |
| NFR-001 | T008-T009, T017, T026-T028 → **amended**: T059-T063, T065-T067 |
| NFR-002 | T037-T045, T048-T052 |
| NFR-003 | T006-T007, T010-T011, T016, T018-T019, T024, T028 |
| NFR-004 | T016-T017, T024-T025, T033-T035, T037-T043, T047, T053 → **amended**: T061-T063, T065 |
| NFR-005 | T033-T035, T039, T042-T043, T046-T047, T050-T053 |
| NFR-006 | T040, T042, T044, T047, T050-T053 |
| NFR-007 | T027-T028, T053 → **amended**: T059, T064-T067 |
| SC-001 | T006-T007, T010-T011, T016-T019, T024-T026 |
| SC-002 | T004-T005, T029-T032, T033-T037, T041, T047 |
| SC-003 | T040, T042, T044, T047, T050-T052 |
| SC-004 | T040, T044, T048-T052, T055-T057 |
| SC-005 | T029-T032, T046, T048-T052, T057 |
| SC-006 | T017, T026-T028 → **amended**: T059-T063, T065-T067 |
| SC-007 | T040, T044, T046, T050-T052, T055-T056 |
| SC-008 | T016-T017, T024-T025, T033-T035, T039-T043, T053 |
| SC-009 | T010, T018, T053-T054 |
| SC-010 | T011, T016, T019, T024-T025, T039-T044, T046-T053 |
| SC-011 | T027-T028, T053 → **amended**: T059, T064-T067 |

NFR-007 and SC-011 no longer carry a deployed numeric gate. The amendment
retains `desired_set_churn_total` as advisory bounded-label telemetry, so the
local tasks pin only its accounting and T064 removes the 8-changes-per-poll,
24-hour release gate that decision 19 had attached to it.

SC-006 has an evidence boundary that the amendment widens rather than removes:
T017 and T026-T028 proved the original 400/400 arithmetic with deterministic
fakes, and T060-T063 extend that proof to entry 400 / retention 450 and exact
450 × 2 = 900 occupancy — still only with deterministic fakes. Live convergence
at 400 channels remains E2a/B4, and live exact-capacity behaviour at 450
channels remains E2b/B4b.
T040/T042/T044/T047/T050-T052 establish the
offline mechanics behind SC-003, but real-burst confirmation remains pending
E4/B6. T029-T032/T046/T048-T052/T057 establish deterministic overlap and
configuration behavior for SC-005, while tuning and adequacy of the window
defaults remain pending E4/B6. No live evidence is closed by this task ledger.

### Supersession map from the original ledger

Checked tasks keep their marks; the amendment does not unmark completed
history. Where an amendment task changes what a completed task established,
the pairing is recorded here.

| Completed task | What it established | Superseding amendment task |
|---|---|---|
| T017 | 150 complete channels per 300-subscription session; channel-versus-subscription units at the 400-channel ceiling | T060 (entry 400 / retention 450, exact 450 × 2 = 900, zero free slots, 451st excluded) |
| T026 | Deterministic capacity execution at 400 channels / 800 subscriptions with the 401st refused | T060, plus T061 for split placement, capacity classification, and `full_at` |
| T027 | Failing static/runtime assertions for checked-in 400/400 thresholds and churn accounting | T060 and T064 (400/450 assertions; churn assertions become advisory) |
| T028 | Checked-in `JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=400` and bounded churn accounting | T063 (checked-in 400/450 with the deploy-at-400 override note) and T064 |
| T008-T009 | Pair-aware `route()` and `_reserve()` that never exceed 300 per connection or three connections | T062 (atomic two-connection split-pair reservation, capacity error, no transient backoff, `full_at` handling) |
| T025 | `eventsub_channel_coverage{state}` in channel units alongside per-connection occupancy | T063 (adds connection-full and capacity-classification visibility) |
| T053 | The operator runbook for the 400-channel ceiling, signals, rollout, and rollback | T065 (amended capacity contract, staged rollout, amended rollback) |
| T058 | The FR/NFR/SC traceability audit against the pre-amendment requirement set | T066 and T067 (traceability and suites re-run against the amended requirements) |

---

## Independent Test Criteria by Story

| Story | Independently complete when |
|---|---|
| **US3** | Fake EventSub state converges both partial directions to complete dual coverage, a refused notification subscription is degraded for a bounded hour and repairable afterwards or on reconnect, every lifecycle path is type-aware, units remain distinct, and — after the capacity amendment — a fresh channel ranked 401-450 does not enter while an incumbent is retained, the monitored set never exceeds 450, 450 complete channels occupy exactly 900 subscriptions with no connection above 300 and zero free slots, the 451st channel is refused, a pair splits atomically across two connections when no connection holds two free slots, a lost split fragment is replaced, and a hard capacity exhaustion is classified distinctly without arming a transient backoff — all before the checked-in retention threshold moves to 450. |
| **US4** | Pure Python tests prove configurable 120/180 defaults, notice-bounded half-open transition behavior, duplicate idempotence, maximum-deadline ordering without a false full-state-ordering claim, exact start/deadline boundaries, pre-notice eligibility, viewer-count independence, fixed future-skew constant, and the full `SuppressionSourceSettings` value set without importing PyFlink. T029 authors the failing static compose assertions during US4, but the US4 configuration criterion and T029 close only after T046 writes both Flink blocks. |
| **US1** | Offline producer and keyed-operator tests prove only valid trigger notices publish with key/payload equality held at the producer, malformed/unknown/over-future payloads fail open visibly at the consumer before observation/state, the sparse second source is configured safely, delivery age is classified healthy/lagging per trusted received record with silence left as idle/unknown, and only qualifying output whose peak lies inside the notice-bounded interval is gated with the required metric/log. |
| **US2** | Paired replays have identical counts, baselines, holds, expiry, timers, and `last_fire_second`; only covered clip outputs and their suppression metric/log differ, including quiet, sparse, idle-to-active re-entry, late, and cross-channel cases. |

---

## Implementation Strategy

### Functional MVP

1. Complete Setup and Foundational phases.
2. Complete US3 and pass T026 before changing the ramp.
3. Complete US4's pure arithmetic.
4. Complete US1 producer first, then consumer.
5. Stop at T047 for an independently demonstrable false-positive suppression
   MVP. Do not treat it as release-ready until US2 proves state equivalence.

### Incremental Completion

1. **Coverage-safe base**: T001-T028.
2. **Deterministic window policy**: T029-T032.
3. **End-to-end offline suppression path**: T033-T047.
4. **No-learning-regression proof**: T048-T052.
5. **Operations and local closure**: T053-T058.
6. **Approved capacity amendment**: T059-T068.

No increment may claim deployed Twitch, Kafka, or PyFlink runtime behavior from
offline evidence.

---

## Deferred Deployed Evidence (Documentation, Not Local Tasks)

T053 writes this checklist into `OPERATIONS.md`. The entries below are
deliberately not checkbox tasks and do not block marking this local ledger
complete. They remain pending until an operator produces evidence on the
configured machine.

| Evidence | Pending operator proof |
|---|---|
| E1 | Both EventSub types coexist on live sessions and `total_cost` remains 0 of 10. Gates dual-coverage sign-off and enabling gating; it does **not** gate the preliminary 400/400 ramp-down on the single-subscription revision, which is 400 x 1 and safe on its own. |
| E2a | With the retention threshold still 400, live convergence reaches 400 complete channels and 800 subscriptions, no connection exceeds 300, roughly 100 slots remain free, and the 401st channel is not admitted. Gates the ramp to 450. |
| E2b | After the account-wide foreign-subscription sweep and the ramp to entry 400 / retention 450, exact capacity holds under relational equality: total subscriptions == 900, == 2 x complete-coverage channels, every connection <= 300 with their sum == 900, free slots == 0, and the 451st channel excluded. The split-placement, fragment-replacement, capacity-classification, and below-cap `full_at` drills all pass (SC-006, decisions 27-28). |
| E3 | A silent suppression topic does not worsen chat watermark lag for one hour; an isolated notice respects the hold bound; both real strategies retain assigners after idleness and drive event time from trusted payload time; exact/+1 ms and chat missing/null/string/float/bool fallbacks cannot poison the combined watermark or lose chat; each of the two Python assignment stages runs at parallelism four with one partition per subtask; and TaskManager Python process count/RSS are recorded. All are deployed-only measurements. |
| E4 | Real gift/raid capture confirms trusted-record `suppression_delivery_age_seconds` against the 30-second warning threshold, downstream malformed-future rejection/warning visibility after Kafka-record timestamp fallback without payload rewriting, real-burst notice-bounded suppression for SC-003, window-default tuning/adequacy for SC-005, trigger mapping, and unaffected out-of-window/pre-notice clips. |
| E5 | Disabling `SUPPRESSION_GATING_ENABLED` restores pre-007 emission, and the capacity-safe rollback order is executable: gating off, retention lowered to 400 and reconverged on a capacity incident, the transport unwound with thresholds at 400/400, a wait for the notification subscriptions to disappear and the subscription count to fall to the desired channel count, and only then the single-subscription ramp restored. |

---

## Notes

- This ledger was fixed at **58 tasks, T001-T058** for the original scope, and
  is extended once by the approved capacity amendment to **68 tasks,
  T001-T068**. Corrections inside a scope fold into existing IDs; do not add,
  split, or renumber them. T059-T068 are the amendment phase and start
  unchecked.
- A checked box records the work that was completed against the task text as it
  stood at the time. Decision 25 amended T038, T041, T045 and T050, and
  decision 26 further amended T038, T041 and T045 after they were checked;
  their existing marks are preserved, and the
  outstanding assignment-path work is carried by the still-open T058 rather
  than by unchecking completed history. Decisions 27-28 change what
  T008-T009, T017, T025-T028, T053, and T058 established; those marks are
  likewise preserved and the superseding work is carried by T059-T068 under
  the supersession map above.
- Do not pass a `WatermarkStrategy` carrying a Python `TimestampAssigner` into
  `env.from_source`; PyFlink 1.18 discards it silently. Attach it with
  `DataStream.assign_timestamps_and_watermarks()` instead, on both the chat and
  suppression streams, and keep topic partitions = source parallelism =
  assignment parallelism = 4 with no repartition between them.
- Build each real strategy bounded out-of-orderness → `with_idleness()` →
  `with_timestamp_assigner()` last. PyFlink 1.18's `with_idleness()` drops an
  assigner stored on the prior wrapper.
- Tests must be written and observed failing before their paired
  implementation task.
- `[P]` never permits concurrent edits to the same file.
- Do not add a new module, dependency, migration, token scope, application
  identity, or `FLINK_PYFILES` entry.
- Do not add a heartbeat or synthetic-record protocol to the suppression topic;
  a silent window is idle/unknown by design.
- Do not add a configuration or environment variable for future timestamp
  trust; `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` is a fixed contract bound.
- Do not start Docker, Kafka, Flink, Redis, Postgres, Twitch/API clients, or
  application services while completing this ledger.
- `docker-compose.yml` checks in `SUPPRESSION_GATING_ENABLED=false` while the
  `SuppressionConfig` code default stays `true`; flipping the compose value is
  an operator action taken only after E1-E3 and the 24-hour E2 churn
  observation.
- The suppression source settings live in `spike_detector.py` precisely so
  their assertions never depend on an optional package.
  `services\flink-job\test_clip_detector.py` is conditional only on the pinned
  `apache-flink==1.18.0` already being installed, uses fakes, starts no
  cluster, and is reported pending — never covered by proxy — when the package
  is absent.
- Postgres-backed tests remain skipped when `TEST_POSTGRES_URL` is absent.
- The gitignored dev-slice replay remains skipped when
  `services\flink-job\corpus\dev-slice.jsonl` is absent; that live capture is
  not a local completion requirement.
- Commit by the four logical boundaries above, not by individual checkbox.

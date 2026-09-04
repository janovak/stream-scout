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

- [ ] T001 [P] Extend `FakeWebsocket`, `FakePoolTwitch`, `existing_subscription`, and notification-envelope builders to model both EventSub coverage types, independent subscription ids, per-type failures, reconnect id rotation, and two list walks in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T002 [P] Add reusable valid and invalid version-1 suppression record builders plus fake keyed-state, timer, and metric contexts derived from `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\flink-job\test_clip_detector.py`
- [ ] T003 [P] Add tagged chat/suppression input builders and state-trace assertion helpers for merged offline replays in `services\flink-job\test_replay.py`

**Checkpoint**: Existing test suites have the doubles needed to express all
Feature 007 failure modes without a broker, socket, Flink cluster, or Twitch
call.

---

## Phase 2: Foundational (Blocking Contract and Pool Primitives)

**Purpose**: Establish the versioned producer contract and the two-slot
in-memory model that every user story relies on.

**Critical**: No user-story implementation starts until this phase is
complete. Tests precede each implementation task.

- [ ] T004 Add failing producer contract tests for all three trigger categories, excluded and unknown categories, required and optional field types, diagnostic-only `viewer_count`, absence of a computed deadline, and malformed identity/time behavior against `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T005 Implement the version-1 trigger allow-list, schema constant, and pure `map_suppression_event()` mapping with trustworthy identity/time validation and no window policy, following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md`, in `services\stream-monitoring\eventsub_pool.py`
- [ ] T006 Add failing tests for one slot per `(broadcaster_id, coverage_type)`, independent chat/notification indexes, and derived `complete`, `chat_only`, `notification_only`, `absent`, and `degraded_chat_only` channel coverage in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T007 Implement `CoverageType`, type-bearing `_Slot`, channel coverage derivation, per-type lookup/forget helpers, and auxiliary-refusal bookkeeping in `services\stream-monitoring\eventsub_pool.py`
- [ ] T008 Add failing routing and reservation tests for two-slot new-channel placement, one-slot repair, preference for a channel's existing connection, legal pair splitting, subscription-counted load, concurrent reservation safety, and the three-connection limit in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T009 Implement pair-aware `route()` and `_reserve()` primitives that reserve the actual number of subscriptions, never exceed 300 subscriptions per connection or three connections, and preserve rendezvous fallback behavior in `services\stream-monitoring\eventsub_pool.py`

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

- [ ] T010 [US3] Add failing pair-creation and repair tests proving an absent channel creates exactly one subscription of each type on the existing operator user id, while `chat_only` and `notification_only` each create only the missing type and preserve the surviving id and pre-call session stamp in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T011 [US3] Add failing enumeration tests proving `list()` joins independent chat and notification walks, requires both types to be enabled on sessions held by the pool, reports both partial directions and `degraded_chat_only`, and marks enumeration incomplete when either walk fails so reconciler drops remain held back in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T012 [US3] Add failing 409 tests proving chat and notification conflicts each adopt only the matching `(type, broadcaster)` subscription on a live held session and refuse subscriptions on foreign or dead sessions in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T013 [US3] Add failing deletion tests proving a channel drop deletes both coverage types, treats either already-gone id as success, follows reconnect-rotated live ids per type, and retains correct surviving indexes after a one-sided delete failure for retry on the next pass in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T014 [US3] Add failing revocation tests proving a known or reconnect-rotated unknown id resolves to the correct channel and type, removes exactly that slot, retains the sibling slot, reports one lost subscription, and leaves a repairable partial state in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T015 [US3] Add failing reconnect and retirement tests for per-type session staleness, the chat-only `_connection_holds()` notification false-positive, independent live-id lookup, co-located and split pairs on socket death, and mid-ramp rebalance without oversubscription in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T016 [US3] Add failing refusal tests proving a notification 403 with live chat records `degraded_chat_only`, does not propagate a channel refusal or evict chat, suppresses repeated auxiliary creates for `AUXILIARY_REFUSAL_RETRY_SECONDS`=3600 and no longer, reports the channel as actual-but-degraded during the hold-off so the reconciler does not hot-loop, returns it to a repairable partial state after expiry, forces re-eligibility immediately on websocket reconnect or connection retirement, clears the state on successful creation or 409 adoption, and remains distinguishable from a chat-subscription refusal in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T017 [US3] Add failing capacity and units tests proving 150 complete channels fill one 300-subscription session, channel 151 routes onward, 400 complete channels use 800 of 900 subscriptions with 100 free, a 401st candidate is not admitted, occupancy counts subscriptions, and coverage/desired metrics count channels in `services\stream-monitoring\test_stream_monitoring.py`

### Implementation for User Story 3

- [ ] T018 [US3] Make `EventSubPoolTransport.create()` register `channel.chat.message` and `channel.chat.notification` with separate callbacks, create only missing types, and stamp each slot with the session read before its own listen call in `services\stream-monitoring\eventsub_pool.py`
- [ ] T019 [US3] Make `EventSubPoolTransport.list()` perform and join two type-filtered walks, adopt only enabled subscriptions on held sessions, expose complete/partial/degraded channel coverage, and propagate either walk's incompleteness without changing the channel-keyed reconciler diff in `services\stream-monitoring\eventsub_pool.py`
- [ ] T020 [US3] Make `_adopt_conflict()` type-aware for either 409 path and rebuild the correct slot and subscription indexes only from a matching broadcaster, type, and live held session in `services\stream-monitoring\eventsub_pool.py`
- [ ] T021 [US3] Make `delete()` and `_live_subscription_ids()` delete both channel coverage types independently, preserve retryable partial state on one-sided failure, and remove only matching type-specific library callbacks and indexes in `services\stream-monitoring\eventsub_pool.py`
- [ ] T022 [US3] Make `_forget_revoked()` and `_forget_unrecognised()` resolve broadcaster plus subscription type, remove exactly one slot even after reconnect id rotation, retain the sibling slot, and invalidate the reconciler by the number of subscriptions actually lost in `services\stream-monitoring\eventsub_pool.py`
- [ ] T023 [US3] Make `_connection_holds()`, `_slot_is_current()`, reconnect cleanup, and `_retire()` type-aware so rotated ids and session changes are evaluated independently and all slots on a dead connection are removed without deleting split siblings elsewhere in `services\stream-monitoring\eventsub_pool.py`
- [ ] T024 [US3] Classify an auxiliary notification refusal with live chat as pool-local degraded coverage bounded by `AUXILIARY_REFUSAL_RETRY_SECONDS` (default 3600, read from the environment): hold off further notification creates only until the deadline passes, report the channel as actual-but-degraded while it holds, return it to ordinary partial-state repair on expiry, force re-eligibility on websocket reconnect or connection retirement, clear the state on successful creation or re-adoption, keep chat refusal behavior unchanged, and expose the degradation without writing the reconciler's seven-day channel refusal in `services\stream-monitoring\eventsub_pool.py`
- [ ] T025 [US3] Publish `eventsub_channel_coverage{state}` in channel units, keep `eventsub_connection_occupancy` in per-connection subscription units, derive `eventsub_subscription_count` from transport occupancy rather than channel count, keep `active_stream_count` channel-based, and retain distinct capacity-refusal signals in `services\stream-monitoring\eventsub_pool.py`, `services\stream-monitoring\reconciler.py`, and `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T026 [US3] Run the deterministic pool and desired-set capacity tests through the 400-channel, 800-subscription, 150-channels-per-session, reconnect, partial-state, and 401st-refusal cases in `services\stream-monitoring\test_stream_monitoring.py`; this passing proof is the hard prerequisite for T028
- [ ] T027 [US3] After T026 passes, add failing static/runtime tests for checked-in 400/400 thresholds, a checked-in `AUXILIARY_REFUSAL_RETRY_SECONDS=3600`, a 400-entry maximum desired set, the accepted zero-width band behavior, and `desired_set_churn_total` incrementing by entered plus departed channels with no per-channel label growth so the NFR-007 bound of 8 changes per poll is directly computable in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T028 [US3] After T026 and T027, set `JOIN_THRESHOLD=400`, `LEAVE_THRESHOLD=400`, and `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` with the 400-channel/800-subscription/100-headroom explanation in `docker-compose.yml`, and implement bounded `desired_set_churn_total` accounting in `services\stream-monitoring\stream_monitoring_service.py`, recording in the compose comment that the NFR-007/SC-011 24-hour churn bound is deployed evidence (E2/B4) and is not claimed here

**Checkpoint**: User Story 3 is complete and independently testable. The
400/400 edit is forbidden until T026 has proved the two-slot pool's capacity
behavior.

---

## Phase 4: User Story 4 - Extend Overlapping Suppression Predictably (Priority: P2)

**Goal**: Relevant notices update a per-channel monotone max-register using
operator-configured gift and raid windows.

**Independent Test**: Apply gifts and raids before, exactly at, and after an
existing deadline in duplicate and arbitrary orders; every order must produce
the same maximum candidate deadline, with 120-second gift and 180-second raid
defaults and no viewer-count influence.

### Tests for User Story 4

- [ ] T029 [P] [US4] Add failing tests for `SuppressionConfig` defaults and environment overrides, `SuppressionState` JSON round trips, separate gift/raid windows, rejected unknown categories, the rule that raid `viewer_count` cannot affect duration, and the pure `SuppressionSourceSettings` construct — `suppression-events` topic name, latest-offset mode, bounded out-of-orderness equal to `WATERMARK_OUT_OF_ORDERNESS_SECONDS`, `SUPPRESSION_IDLENESS_SECONDS=5` strictly below chat's 10 seconds, four expected partitions matching expected parallelism four, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` — plus static `docker-compose.yml` assertions for both Flink environment blocks, checked-in gating false, lag warning, partitions/parallelism, and unchanged `FLINK_PYFILES`, all without importing PyFlink, in `services\flink-job\test_spike_detector.py`; author these failing compose assertions during US4, but do not close T029 until T046 writes the asserted Flink variables
- [ ] T030 [US4] Add failing pure-function tests for later-deadline extension, earlier/equal no-op, exact-deadline arrival, duplicate idempotence, arbitrary ordering, simultaneous gift/raid notices, absent-state fail-open, and strict peak-second boundary gating in `services\flink-job\test_spike_detector.py`

### Implementation for User Story 4

- [ ] T031 [US4] Implement `SuppressionConfig.from_env()` as the runtime reader with 120-second gift and 180-second raid defaults, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and a `SUPPRESSION_GATING_ENABLED` code default of `true`; add JSON-backed `SuppressionState`; and create the pure `SuppressionSourceSettings` construct carrying topic, latest-offset mode, out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS=5`, expected partitions/parallelism, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` so the job builds its source from it instead of inline literals, all with no PyFlink import, in `services\flink-job\spike_detector.py`
- [ ] T032 [US4] Implement pure `apply_notice()` and `is_suppressed()` max-register arithmetic without timers, I/O, viewer-count logic, or changes to `evaluate()` in `services\flink-job\spike_detector.py`

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
spikes through deterministic producer and operator doubles. Spikes whose peak
seconds are inside active windows produce no clip but do produce exactly one
suppression metric and structured log; excluded, malformed, absent, disabled,
or late notices do not retract or incorrectly suppress output.

### Producer Tests for User Story 1

- [ ] T033 [US1] Add failing pool-to-service callback and publisher tests for exact version-1 JSON, the producer invariant that the Kafka key is always `str(broadcaster_id)` and equals the payload `broadcaster_id` (asserted here because the producer is the only party that observes both), the three trigger types, ignored-category buckets in `suppression_notices_ignored_total`, malformed identity/time counters and structured logs, `received_at_ms`, delivery callback/poll behavior, and produce-exception containment against `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T034 [US1] Add failing static tests that `suppression-events` has four partitions and one-hour retention and that the producer uses that exact topic without modifying `chat-messages` in `services\stream-monitoring\test_stream_monitoring.py`

### Producer Implementation for User Story 1

- [ ] T035 [US1] Wire the pool's notification callback into `_on_eventsub_notification()`, publish contract-valid events through `_publish_suppression_event()`, and add bounded ignored/malformed metrics and structured failure logs following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T036 [US1] After T034, create `suppression-events` with four partitions, replication factor one, and `retention.ms=3600000` beside the existing topics without changing `chat-messages` in `docker-compose.yml`

### Consumer Tests for User Story 1

- [ ] T037 [US1] After T036, add failing consumer tests for valid version-1 records, invalid JSON, non-object JSON, missing/unknown `schema_version`, wrong field types, excluded categories, diagnostic-only optional fields, routing and state keyed off the payload `broadcaster_id` because the value-only deserializer never exposes the Kafka key, and no exception escape, following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md`, in `services\flink-job\test_clip_detector.py`; do not assert key/payload agreement here — that invariant belongs to T033
- [ ] T038 [US1] Add failing tests that the second `suppression-events` source is built from the pure `SuppressionSourceSettings` of T031 — payload-`broadcaster_id` keying, `occurred_at_ms` timestamp assignment, bounded out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS=5` strictly below chat's 10 seconds, latest offsets, and four topic partitions matching `FLINK_PARALLELISM=4`. The guaranteed-offline half — the settings values themselves and the matching static `docker-compose.yml` assertions — lives in `services\flink-job\test_spike_detector.py` with no PyFlink import. The PyFlink wiring half, proving `clip_detector_job` builds its real source and watermark strategy from those settings, stays in `services\flink-job\test_clip_detector.py`, uses fakes only, starts no cluster or MiniCluster, and is conditional solely on the pinned `apache-flink==1.18.0` already being installed
- [ ] T039 [US1] Add failing operator tests proving `process_element2()` updates only the current broadcaster's suppression state, writes only on extension, registers no timer, emits no output, counts rejected records by reason, captures `consumer_receipt_ms` from an injected clock at receipt, computes `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`, classifies healthy at or below `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS`=30 and lagging above it, and never uses optional diagnostic-only `received_at_ms` for classification. Include a record with small Twitch-to-producer delay but injected consumer delay greater than 30 seconds that therefore classifies lagging, a negative raw age clamped to zero for observation/classification with a structured clock-skew diagnostic log, no delivery-health sample when no record arrives so a silent window stays idle/unknown, TTL with `NeverReturnExpired`, and absent or expired state as fail-open in `services\flink-job\test_clip_detector.py`
- [ ] T040 [US1] Add failing `on_timer()` tests proving the gate uses the spike peak second, is strict at the deadline, honors `SUPPRESSION_GATING_ENABLED`, preserves normal output when inactive, still updates `last_fire_second` and `anomalies_detected_total`, emits exactly one attributable suppression metric and structured log when active, and never retracts an output when a notice arrives late in `services\flink-job\test_clip_detector.py`

### Consumer Implementation for User Story 1

- [ ] T041 [US1] Implement defensive version-1 decoding, payload field validation, trigger allow-list enforcement, and `occurred_at_ms` timestamp assignment following `specs\007-suppress-gift-raid-bursts\contracts\suppression-events.schema.md` in `services\flink-job\clip_detector_job.py`; derive routing and state from the payload `broadcaster_id` and implement no Kafka-key comparison, because the value-only deserializer never surfaces the key to the operator
- [ ] T042 [US1] Register the feature's consumer-side signals with bounded reason/category labels in `services\flink-job\clip_detector_job.py`: `suppression_records_rejected_total{reason}`; `suppression_records_consumed_total{lag_class="healthy"|"lagging"}` plus `suppression_delivery_age_seconds`, observed once per received record from `max(0, consumer_receipt_ms - occurred_at_ms)` using the injected/current consumer clock, and a structured log for lagging records; clamp negative raw age to zero and emit a structured clock-skew diagnostic log; keep optional `received_at_ms` diagnostic-only; publish nothing for a window containing no record so silence reads as idle/unknown rather than healthy; and retain `clips_suppressed_total{broadcaster_id, notice_type}` with the `broadcaster_id` label NFR-006 requires under the same finite monitored-channel label policy as `anomalies_detected_total`. Preserve `anomalies_detected_total` unchanged and add the channel-attributable structured suppression log. Only reason/category labels need bounding; do not drop broadcaster attribution and do not add a continuously refreshed per-channel delivery gauge driven from `on_timer`
- [ ] T043 [US1] Convert `AnomalyDetector` to `KeyedCoProcessFunction`, retain the chat body as `process_element1()`, register JSON `suppression` `ValueState` under the existing TTL policy, and implement no-timer/no-output `process_element2()` with write-on-extension semantics in `services\flink-job\clip_detector_job.py`
- [ ] T044 [US1] Add the post-state-write output gate at the final `yield`, keyed off `spike.detected_at_seconds`, while preserving bucket expiry, hold writes, chain timers, `last_fire_second`, anomaly counting, fail-open behavior, and the kill switch in `services\flink-job\clip_detector_job.py`
- [ ] T045 [US1] Build the second Kafka source from `SuppressionSourceSettings` with latest offsets and the suppression watermark/idleness strategy, key both inputs on the payload `broadcaster_id`, and connect them into `AnomalyDetector` without changing `CommandFilter` or `ClipCreator` in `services\flink-job\clip_detector_job.py`
- [ ] T046 [US1] Add `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and `SUPPRESSION_GATING_ENABLED=false` to both `flink-jobmanager` and `flink-taskmanager` environment blocks while leaving `FLINK_PYFILES` unchanged in `docker-compose.yml`; the checked-in gating value is `false` even though the `SuppressionConfig` code default is `true`, with a comment stating it is changed to `true` operationally only after E1-E3 and the 24-hour E2 churn observation pass, then close T029 by making its previously authored static compose assertions pass
- [ ] T047 [US1] Run the offline producer, contract, source-settings, malformed-input, fail-open, late-notice, delivery-classification, and output-gate unit selections in `services\stream-monitoring\test_stream_monitoring.py`, `services\flink-job\test_spike_detector.py`, and `services\flink-job\test_clip_detector.py`; the non-skippable evidence for this checkpoint is the stream-monitoring selection plus `test_spike_detector.py`, which import no PyFlink, and `test_clip_detector.py` counts only when the pinned `apache-flink==1.18.0` is already installed — report it as pending rather than passed when it is absent, and never treat its absence as covered by the pure suites

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

- [ ] T048 [US2] Add failing tests that the replay harness accepts a deterministic merged sequence of chat and version-1 suppression records, maintains suppression independently per broadcaster, and applies notices at their delivery position rather than retroactively in `services\flink-job\test_replay.py`
- [ ] T049 [US2] Add failing gated-versus-ungated equivalence tests for identical message counts, baseline readings, hold open/peak/close trajectories, sorted expired buckets, and `last_fire_second`, with differences limited to clip output plus one suppression metric/log per covered spike in `services\flink-job\test_replay.py`
- [ ] T050 [US2] Add failing deterministic acceptance cases for a suppression input quiet for the whole replay, sparse notices, an isolated notice arriving after a long silence that holds the harness's simplified two-input watermark by no more than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before the input is idle again while sustained notice traffic advances it normally, a late notice with no retraction but a later unexpired effect, cross-channel isolation, inactive/expired state, and byte-identical repeated output in `services\flink-job\test_replay.py`; the simplified-model bound is offline evidence only and the deployed measurement remains E3

### Implementation for User Story 2

- [ ] T051 [US2] Extend `EventTimeReplayer` with per-key `SuppressionState`, merged delivery-order notice application, and the same output-only peak-second gate used by the job while retaining all existing chat timer and state behavior in `services\flink-job\tools\replay.py`
- [ ] T052 [US2] Expose deterministic state and suppression side-effect traces from the replay harness so tests can compare counts, readings, holds, expiry, cooldown, emitted clips, metrics, and logs without changing the existing human-readable unsuppressed output in `services\flink-job\tools\replay.py`

**Checkpoint**: User Story 2 supplies the decisive offline SC-004 proof and
the simplified quiet-input, sparse idle-to-active re-entry, and late-arrival
evidence without claiming PyFlink runtime watermark behavior.

---

## Phase 7: Polish and Cross-Cutting Closure

**Purpose**: Complete locally verifiable integration, runbook, rollback, and
scope guards without turning deployed evidence into local tasks.

- [ ] T053 [P] Document the 400-channel ceiling, two coverage types, coverage/subscription units, the bounded auxiliary-refusal hold-off and its expiry/reconnect recovery, the suppression topic and windows, every new metric and log — `clips_suppressed_total{broadcaster_id, notice_type}`, `eventsub_channel_coverage{state}` including `degraded_chat_only`, `suppression_notices_ignored_total`, `suppression_notices_malformed_total`, `suppression_records_rejected_total`, `suppression_records_consumed_total{lag_class}`, `suppression_delivery_age_seconds` from `max(0, consumer_receipt_ms - occurred_at_ms)` with clock-skew clamp/log semantics and optional `received_at_ms` diagnostic decomposition, and `desired_set_churn_total` — the Prometheus windowed reading of healthy/lagging/idle-unknown delivery, fail-open diagnosis, the NFR-007 churn bound and its release disposition, the staged rollout with `SUPPRESSION_GATING_ENABLED=false` checked in and enabled only after E1-E3 and the 24-hour E2 churn observation, the operator kill switch, the capacity-safe rollback order (gating off, unwind the transport while thresholds stay at 400/400, wait until notification subscriptions are gone and total subscriptions fall to roughly the desired channel count with stable coverage metrics, only then raise thresholds, never above 400 while any notification subscription remains), and an explicitly pending operator-run E1-E5 checklist in `OPERATIONS.md`
- [ ] T054 [P] Verify the feature adds no dependency, database, Redis-layout, chat-schema, application-identity, authorization-scope, `FLINK_PYFILES`, or Dockerfile module-copy change by reviewing `services\stream-monitoring\Dockerfile`, `services\stream-monitoring\requirements.txt`, `services\flink-job\Dockerfile`, `services\flink-job\requirements.txt`, and `docker-compose.yml`
- [ ] T055 [P] Run the complete locally permitted stream-monitoring unit suite and resolve only Feature 007 regressions in `services\stream-monitoring\test_stream_monitoring.py` and `services\stream-monitoring\test_desired_set_store.py`
- [ ] T056 [P] Run the complete locally permitted Flink pure/unit suite and resolve only Feature 007 regressions in `services\flink-job\test_spike_detector.py`, `services\flink-job\test_clip_detector.py`, `services\flink-job\test_replay.py`, and `services\flink-job\test_clip_attempt.py`; `test_spike_detector.py`, `test_replay.py`, and `test_clip_attempt.py` import no PyFlink and are the non-skippable evidence, while `test_clip_detector.py` runs only when the pinned `apache-flink==1.18.0` is already installed and is reported as pending, not passed, when it is absent
- [ ] T057 Run the suppression replay twice over the same checked-in deterministic fixture and require byte-identical output plus identical state traces in `services\flink-job\tools\replay.py` and `services\flink-job\test_replay.py`
- [ ] T058 Complete the final FR-001..FR-018, NFR-001..NFR-007, and SC-001..SC-011 traceability audit, confirm every checklist line retains strict task syntax, and record only locally completed work as checked in `specs\007-suppress-gift-raid-bursts\tasks.md`

**Checkpoint**: All local implementation, deterministic tests, replay, static
configuration checks, and operator documentation can be complete while E1-E5
remain pending.

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

### Logical Commit Boundaries

1. **EventSub pool**: T001, T006-T026.
2. **Producer, configuration, and capacity**: T004-T005, T027-T028,
   T033-T036.
3. **Flink detector**: T002, T029-T032, T037-T047.
4. **Replay, operations, and integration**: T003, T048-T058.

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
| FR-006 | T029-T032, T039, T043, T046 |
| FR-007 | T040, T044, T046-T047, T050-T052 |
| FR-008 | T040, T044, T049-T052 |
| FR-009 | T040, T044, T046, T050-T052 |
| FR-010 | T031-T032, T039, T043, T048-T052 |
| FR-011 | T011, T016, T019, T024-T025, T035, T039-T044, T046-T047, T050-T053 |
| FR-012 | T040, T042, T044, T047, T050-T053 |
| FR-013 | T017, T026-T028, T053 |
| FR-014 | T008-T009, T017, T026, T028 |
| FR-015 | T017, T025-T026, T053 |
| FR-016 | T010, T018, T053-T054 |
| FR-017 | T004-T005, T033-T035, T037, T041-T042, T047 |
| FR-018 | T040, T044, T048-T052 |
| NFR-001 | T008-T009, T017, T026-T028 |
| NFR-002 | T037-T045, T048-T052 |
| NFR-003 | T006-T007, T010-T011, T016, T018-T019, T024, T028 |
| NFR-004 | T016-T017, T024-T025, T033-T035, T037-T043, T047, T053 |
| NFR-005 | T033-T035, T039, T042-T043, T046-T047, T050-T053 |
| NFR-006 | T040, T042, T044, T047, T050-T053 |
| NFR-007 | T027-T028, T053 |
| SC-001 | T006-T007, T010-T011, T016-T019, T024-T026 |
| SC-002 | T004-T005, T029-T032, T033-T037, T041, T047 |
| SC-003 | T040, T042, T044, T047, T050-T052 |
| SC-004 | T040, T044, T048-T052, T055-T057 |
| SC-005 | T029-T032, T046, T048-T052, T057 |
| SC-006 | T017, T026-T028 |
| SC-007 | T040, T044, T046, T050-T052, T055-T056 |
| SC-008 | T016-T017, T024-T025, T033-T035, T039-T043, T053 |
| SC-009 | T010, T018, T053-T054 |
| SC-010 | T011, T016, T019, T024-T025, T039-T044, T046-T053 |
| SC-011 | T027-T028, T053 |

NFR-007 and SC-011 are a deployed measurement. The local tasks above pin only
the accounting — that `desired_set_churn_total` increments by entered plus
departed channels with bounded labels, and that the bound is documented — so
the 8-changes-per-poll average over 24 hours stays E2/B4 evidence and is never
claimed from this ledger.

SC-006 has the same evidence boundary: T017 and T026-T028 prove capacity
arithmetic and convergence only with deterministic fakes; live 400-channel
convergence remains pending E2/B4. T040/T042/T044/T047/T050-T052 establish the
offline mechanics behind SC-003, but real-burst confirmation remains pending
E4/B6. T029-T032/T046/T048-T052/T057 establish deterministic overlap and
configuration behavior for SC-005, while tuning and adequacy of the window
defaults remain pending E4/B6. No live evidence is closed by this task ledger.

---

## Independent Test Criteria by Story

| Story | Independently complete when |
|---|---|
| **US3** | Fake EventSub state converges both partial directions to complete dual coverage, a refused notification subscription is degraded for a bounded hour and repairable afterwards or on reconnect, every lifecycle path is type-aware, 400 channels occupy exactly 800 subscriptions with no connection above 300, units remain distinct, and the 401st channel is refused before 400/400 is written. |
| **US4** | Pure Python tests prove configurable 120/180 defaults, monotone max behavior, duplicate/order independence, exact-boundary behavior, viewer-count independence, and the full `SuppressionSourceSettings` value set without importing PyFlink. T029 authors the failing static compose assertions during US4, but the US4 configuration criterion and T029 close only after T046 writes both Flink blocks. |
| **US1** | Offline producer and keyed-operator tests prove only valid trigger notices publish with key/payload equality held at the producer, malformed/unknown payloads fail open visibly at the consumer, the sparse second source is configured safely, delivery age is classified healthy/lagging per received record with silence left as idle/unknown, and only qualifying output inside the peak-time window is gated with the required metric/log. |
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
| E2 | Live convergence reaches 400 complete channels and 800 subscriptions, no connection exceeds 300, and at least 100 slots remain free. |
| E2 churn | Across a 24-hour observation at 400/400, desired-set entries plus departures average at most 8 per poll (2% of the 400-channel ceiling). Exceeding it blocks enabling gating and requires a specification change, not a code workaround (NFR-007, SC-011). |
| E3 | A silent suppression topic does not worsen the chat input watermark lag for at least one hour, **and** an isolated notice delivered after that silence holds the operator watermark no longer than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`. Both cases are required. |
| E4 | Real gift/raid capture confirms `suppression_delivery_age_seconds` against the 30-second warning threshold, real-burst suppression for SC-003, window-default tuning/adequacy for SC-005, trigger mapping, and unaffected out-of-window clips. |
| E5 | Disabling `SUPPRESSION_GATING_ENABLED` restores pre-007 emission, and the capacity-safe rollback order is executable: gating off, unwind the transport with thresholds still at 400/400, wait for the notification subscriptions to disappear and subscription count to fall to roughly the desired channel count, and only then raise thresholds. |

---

## Notes

- This ledger is fixed at **58 tasks, T001-T058**. Later corrections fold work
  into existing IDs; do not add, split, or renumber them.
- Tests must be written and observed failing before their paired
  implementation task.
- `[P]` never permits concurrent edits to the same file.
- Do not add a new module, dependency, migration, token scope, application
  identity, or `FLINK_PYFILES` entry.
- Do not add a heartbeat or synthetic-record protocol to the suppression topic;
  a silent window is idle/unknown by design.
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

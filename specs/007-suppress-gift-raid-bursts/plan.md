# Implementation Plan: Suppress Gift and Raid Chat Bursts

**Branch**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/007-suppress-gift-raid-bursts/spec.md`
**Companions**: [research.md](./research.md), [data-model.md](./data-model.md),
[contracts/suppression-events.schema.md](./contracts/suppression-events.schema.md),
[quickstart.md](./quickstart.md), [autonomous-decisions.md](./autonomous-decisions.md)

## Summary

Gifted-subscription thank-you floods and post-raid greeting bursts produce large
chat-rate spikes that are not interesting content, and the detector currently
clips them.

This feature adds a second EventSub subscription — `channel.chat.notification` —
for **every** monitored channel, maps the three triggering notices
(`community_sub_gift`, `sub_gift`, `raid`) onto a new versioned Kafka topic
`suppression-events` keyed by `broadcaster_id`, and gates **clip emission only**
in the Flink detector for a short, operator-configured window after each notice.
Chat messages continue to flow, be counted, and update the rolling baseline
exactly as they do today; nothing is removed from any topic or any state.

Three things carry all the risk, and the plan is ordered around them:

1. **The pool becomes a two-slot-per-channel state model.** `eventsub_pool.py`
   assumes one subscription per channel in routing, reservation, enumeration,
   409 adoption, deletion, revocation, reconnect staleness, and retirement.
   The pair lives inside the pool; `reconciler.py` stays channel-keyed
   (research D1). Its capacity behaviour must be proved by deterministic tests
   **before** the ramp configuration moves.
2. **Two subscriptions per channel halve the ceiling.** 400 channels × 2 = 800
   of the 900 available subscription slots, leaving the required 100 for
   reconnect and adoption. `JOIN_THRESHOLD` / `LEAVE_THRESHOLD` move from
   800/900 to 400/400 (FR-013, locked decision 5).
3. **A second Flink input can freeze event time.** A two-input operator's
   watermark is the minimum of its inputs, and the suppression stream is silent
   for hours at a time. It gets real watermarks, idleness shorter than the chat
   stream's, `latest()` offsets, and partitions matching parallelism
   (research D4, R3). One residual case is accepted rather than engineered
   away: when a long-idle suppression subtask receives a single isolated
   notice, that subtask re-enters the watermark minimum and can hold the
   operator's two-input watermark for up to
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before
   it goes idle again (research R10, data-model I16).

The detector change is deliberately small: suppression state is a per-channel
notice-bounded half-open interval
`[suppress_from_ms, suppress_until_ms)`, and the gate is an **output-only
filter** at the end of `on_timer`. An extending overlapping notice preserves
the earliest start and later deadline, an earlier/equal candidate is a complete
state no-op, and a notice at or after the old deadline starts a new interval.
No detector-state write changes, so a replay with gating and a replay without
it produce identical counts, baselines, holds, and `last_fire_second` (SC-004);
only the clip and the two required operator signals differ.

## Technical Context

**Language/Version**: Python 3.11 (`services/stream-monitoring`); Python 3.10 (PyFlink job image)
**Primary Dependencies**: `twitchAPI==4.5.0` (`EventSubWebsocket.listen_channel_chat_notification`), `confluent-kafka==2.3.0`, `apache-flink==1.18.0` (`KeyedCoProcessFunction`, `KafkaSource`, `WatermarkStrategy`), `prometheus-client==0.19.0`, `redis==5.0.1`, `psycopg2-binary==2.9.9`, `APScheduler==3.10.4` — **all pins unchanged; no new dependency**
**Storage**: Kafka `suppression-events` (new topic, 4 partitions, keyed by `broadcaster_id`); Flink keyed state (one new `ValueState`); Redis desired set and Postgres `streamers` unchanged — **no schema migration**
**Testing**: pytest, co-located. `services/stream-monitoring/test_stream_monitoring.py` (pool + publisher, no broker, no socket); `services/flink-job/test_spike_detector.py` (pure gate arithmetic), `test_replay.py` (event-time replay), `test_clip_detector.py` (mapping/filters)
**Target Platform**: Docker Compose stack; one `stream-monitoring` container; Flink 1.18 standalone cluster at `FLINK_PARALLELISM=4`
**Project Type**: single (backend service plus stream-processing job)
**Authorization**: existing application identity and operator user token; `user:read:chat` already covers **both** chat coverage types — no reseeding, no new scope (FR-016, research §1.3)
**Capacity**: 3 websocket connections × 300 enabled subscriptions = 900 per client-id/user-id; 400 monitored channels × 2 subscriptions = 800 steady state; ≥100 slots reserved (FR-014)
**Suppression windows**: gift 120 s, raid 180 s, operator-configured, applied at the **consumer** (research D7); raid viewer count never affects duration
**New configuration surface**: `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_GATING_ENABLED`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30` on **both** Flink blocks; `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` on `stream-monitoring`. `docker-compose.yml` checks in `SUPPRESSION_GATING_ENABLED=false`; the code-level `SuppressionConfig` default stays `true` (decision 21). `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` is a fixed consumer contract constant, not a configuration value or environment variable (decision 23)
**Offline testability**: the suppression source's settings — topic name, `latest()` offset mode, bounded out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS = 5`, expected 4 partitions and parallelism 4, plus pure fields `delivery_lag_warn_seconds=30` and `checked_in_gating_enabled=False` — and fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` live in `spike_detector.py`, so they are asserted in `test_spike_detector.py` and against `docker-compose.yml` without importing PyFlink. `SuppressionConfig.from_env()` remains the runtime reader and its code default for gating remains `true`
**Constraints**: the suppression input must never become the binding watermark minimum (research R3); the `chat-messages` schema stays frozen (spec 004 FR-008); `evaluate()` stays pure and signature-compatible
**Scale/Scope**: 400 channels, ~800 subscriptions, ~3 suppression events per channel per hour at the high end — a topic that is silent for most seconds on most partitions

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design below.*

| Principle | Status |
|---|---|
| Kafka for all inter-service messaging | **Extended** — one new topic, `suppression-events`, produced by `stream-monitoring` and consumed by the Flink job. No other transport is introduced; the operator performs no lookup of its own (research D3) |
| Postgres exclusively for persistence | Unchanged — suppression state is transient Flink keyed state with a TTL. Nothing new is persisted, and no migration is required |
| PyFlink for stream processing | Unchanged in principle — `AnomalyDetector` becomes a `KeyedCoProcessFunction` on the same job, same parallelism |
| Twitch API integration | Unchanged identity — same app, same user token, same `user:read:chat` scope, one additional subscription type (FR-016) |
| Prometheus metrics | **Extended** — suppressed-emission counter, coverage-state gauge, ignored-notice counter, malformed-notice counter, rejected-record counter, per-record `suppression_delivery_age_seconds` observation, desired-set churn counter |
| Grafana / Loki observability | Unchanged path — the suppression decision log is a structured `jsonlogger` line like every other |
| **No data loss in the event pipeline** | **Preserved** — see the re-check below. Suppression gates a derived emission decision only; no chat record, bucket, or row is dropped |
| Anomaly detection filters bot commands | Unchanged — `CommandFilter` is upstream of the detector and untouched |
| Health check endpoints | Unchanged — `/health` on 8080, same process |
| Python virtual environments | `services/stream-monitoring/.venv`, `services/flink-job` venv; no new package to install |

No principle is violated by design. The two items worth stating explicitly —
fail-open on an absent auxiliary signal, and the deliberate non-emission of a
suppressed clip — are examined after the design in the next section.

### Constitution re-check after design

**Why gating only emission preserves the chat pipeline and "no data loss".**
The gate is a single conditional around one `yield` at the very end of
`AnomalyDetector.on_timer`, after `evaluate()` has returned and after every
state write. Concretely, none of the following changes on a suppressed
decision: the `chat-messages` record is produced by `stream-monitoring` exactly
as before; `message_counts` receives the same bucket increments; the same
`expired_buckets` are removed; the same `hold` is written or cleared; the same
chain timer is registered; and `last_fire_second` is updated exactly as it
would have been (research D6). The pipeline Kafka → Processing → Storage
carries the same data it carried before this feature; what does not happen is a
*Twitch clip creation request*, which is a downstream action taken on a derived
decision, not a pipeline record. SC-004 is the executable form of this claim: a
replay with gating and one without must produce identical counts, baseline,
hold, and cooldown state, differing only in clip emission and the two required
operator signals.

**Why fail-open on an absent auxiliary signal preserves availability.**
Suppression state has exactly one absent value — no active deadline — and the
detector cannot distinguish "no notice occurred" from "notice coverage is
missing, lagging, revoked, or the topic is empty". Treating absence as
"suppress" would mean any auxiliary failure — a revoked notification
subscription, a Kafka partition stall, a job restart with `latest()` offsets,
one dead websocket — silently stops clip creation for the affected channels
while the chat pipeline is perfectly healthy. That converts an auxiliary-signal
outage into a highlight outage, which is precisely the failure the
constitution's "Reliability: ensure no highlight moments are missed due to
system failures" value forbids. Failing open keeps the pre-007 behaviour as the
floor: the worst outcome of a suppression failure is the false-positive clips
the system already produces today. The degradation is not silent — coverage
state, delivery age, and malformed-input counters make it operationally
distinguishable from healthy dual coverage (FR-011, NFR-004, NFR-005).

**The one asymmetry, stated deliberately.** The chat mapper publishes a message
with `sent_at: null` rather than dropping it, because dropping chat *is* data
loss. The suppression producer does the opposite: a notice without a
trustworthy channel identity or occurrence time is **not published**
(FR-017, research D9). Publishing it would require the consumer to invent a
time, which would fabricate a deadline and suppress real clips — a worse
outcome than losing an auxiliary event that the system is already designed to
survive losing.

## Project Structure

### Documentation (this feature)

```text
specs/007-suppress-gift-raid-bursts/
├── spec.md                  # Product behaviour (do not change in planning)
├── plan.md                  # This file
├── research.md              # Phase 0 — sources, decisions D1-D16, risks R1-R12, deferred evidence E1-E5
├── data-model.md            # Phase 1 — entities, state, transitions, invariants
├── contracts/
│   └── suppression-events.schema.md   # Phase 1 — the versioned Kafka contract
├── quickstart.md            # Phase 1 — offline validation here vs deployed validation elsewhere
├── autonomous-decisions.md  # Decisions taken without asking the user
└── tasks.md                 # Phase 2 — /speckit.tasks output, NOT created by planning
```

### Source Code (repository root)

```text
services/stream-monitoring/
├── eventsub_pool.py               # two-slot-per-channel state model; notification listener;
│                                  #   map_suppression_event(); type-aware route/list/create/
│                                  #   adopt/delete/revoke/reconnect; coverage view;
│                                  #   bounded auxiliary-refusal backoff (AUXILIARY_REFUSAL_RETRY_SECONDS)
├── stream_monitoring_service.py   # _on_eventsub_notification handler; _publish_suppression_event();
│                                  #   suppression + coverage metrics; bounded desired_set_churn_total
│                                  #   at the desired-set write/publish site
├── reconciler.py                  # UNCHANGED behaviourally — stays channel-keyed (research D1).
│                                  #   Threshold resolution and churn computation stay out of it
├── test_stream_monitoring.py      # extended: pool two-slot classes, mapping, publisher, capacity
└── Dockerfile                     # unchanged — no new module

services/flink-job/
├── spike_detector.py              # + SuppressionState, apply_notice(), is_suppressed(),
│                                  #   SuppressionConfig, SuppressionSourceSettings,
│                                  #   SUPPRESSION_IDLENESS_SECONDS,
│                                  #   SUPPRESSION_DELIVERY_LAG_WARN_SECONDS.
│                                  #   evaluate() UNCHANGED in signature and behaviour
├── clip_detector_job.py           # AnomalyDetector -> KeyedCoProcessFunction; second KafkaSource;
│                                  #   suppression watermark strategy; output-only gate; metrics
├── tools/replay.py                # replay a merged chat + suppression corpus; gate applied the
│                                  #   same way, so the harness and the job agree
├── test_spike_detector.py         # extended: max-register, window arithmetic, gate predicate,
│                                  #   SuppressionSourceSettings, static docker-compose assertions
├── test_replay.py                 # extended: silent suppression stream, late notice, ordering,
│                                  #   sparse idle -> active watermark-hold bound
└── test_clip_detector.py          # extended: suppression record decode/validation, PyFlink
                                   #   wiring with fakes; conditional on the pinned PyFlink package

docker-compose.yml                 # kafka-init: suppression-events topic (4 partitions);
                                   #   flink env: SUPPRESSION_* vars on jobmanager AND taskmanager,
                                   #   with SUPPRESSION_GATING_ENABLED=false checked in;
                                   #   stream-monitoring: JOIN_THRESHOLD/LEAVE_THRESHOLD 400/400,
                                   #   AUXILIARY_REFUSAL_RETRY_SECONDS=3600

OPERATIONS.md                      # 400-channel ceiling, dual coverage, suppression runbook,
                                   #   capacity-safe rollback order
```

**Structure Decision**: no new module in either service.

- The pure suppression arithmetic goes into `spike_detector.py`, which is
  already the "pure detector math, no pyflink import" module and is already on
  the job's `FLINK_PYFILES` list and both Flink bind-mounts. A new module would
  need a `-pyFiles` entry plus two `docker-compose.yml` volume entries, and the
  repository has been bitten by exactly that omission before (spec 004,
  "Deployment wiring (do not skip)"; OPERATIONS.md "Adding a new Python
  module"). Avoiding new deployment wiring is worth more here than a tidier
  module boundary.
- The **suppression source settings** go into the same module, as a pure
  `SuppressionSourceSettings` construct: topic name `suppression-events`,
  `latest()` starting-offset mode, bounded out-of-orderness equal to
  `WATERMARK_OUT_OF_ORDERNESS_SECONDS`, `SUPPRESSION_IDLENESS_SECONDS = 5`,
  expected partition count 4 and expected parallelism 4,
  pure fields `delivery_lag_warn_seconds=30` and
  `checked_in_gating_enabled=False`. `SuppressionConfig.from_env()` remains
  the runtime environment reader and keeps its `true` gating default.
  `clip_detector_job.py` builds the real `KafkaSource` and
  `WatermarkStrategy` **from** that construct rather than from inline literals.
  The point is testability: every one of those values is then asserted in
  `test_spike_detector.py` and against `docker-compose.yml` with no PyFlink
  import and no cluster, so the offline evidence for the sparse-source design
  is never conditional on an optional package being installed.
- The notification mapping goes into `eventsub_pool.py` next to
  `map_chat_message`, for the same reason that one exists: the mapping must be
  testable without a socket.
- `reconciler.py` is not restructured. The pair is a transport concern; the
  reconciler's channel-keyed diff, rank ordering, refusal cache, retry budget
  and metrics are unchanged (research D1).

### Deployment wiring (do not skip)

| Surface | Required change |
|---|---|
| `docker-compose.yml` `kafka-init` | Create `suppression-events` with **4 partitions** (matching `FLINK_PARALLELISM`) and a short retention, alongside the existing two topics |
| `docker-compose.yml` `flink-jobmanager` **and** `flink-taskmanager` | Add `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and `SUPPRESSION_GATING_ENABLED=false` to **both** blocks — the existing `DETECTION_*` variables are duplicated across both for the same reason. The checked-in gating value is `false`; the code default in `SuppressionConfig` stays `true`, so a deploy is inert until an operator flips the compose value after E1-E3 and the 24-hour E2 churn observation (decision 21, NFR-007) |
| `docker-compose.yml` `stream-monitoring` | `JOIN_THRESHOLD=400`, `LEAVE_THRESHOLD=400`, with the comment explaining the two-subscriptions-per-channel arithmetic; `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` for the bounded notification-refusal backoff (decision 17) |
| `FLINK_PYFILES` | **No change** — `spike_detector.py` is already listed |
| `Dockerfile` (either service) | **No change** — no new module |

## Design at a glance

```text
Twitch EventSub websocket (one session, mixed types)
  │
  ├── channel.chat.message ──────► map_chat_message ──► Kafka chat-messages        (unchanged)
  │
  └── channel.chat.notification ─► map_suppression_event
                                     │  drop unless notice_type in
                                     │  {community_sub_gift, sub_gift, raid}
                                     │  drop (and count) if identity or time is untrustworthy
                                     └──► Kafka suppression-events   key = broadcaster_id
                                                        │
Flink job                                               │
  chat-messages ──► CommandFilter ──► key_by(broadcaster_id) ─┐
                                                              ├─► AnomalyDetector
  suppression-events ──► key_by(broadcaster_id) ──────────────┘   (KeyedCoProcessFunction)
                                                                    │
   process_element1: bucket + arm timer            (unchanged)      │
   process_element2: decode/fields → fixed future-time trust check  │
                     → apply [suppress_from, suppress_until)         │
                     (no timer; rejected future record observes/writes nothing)
   on_timer:  evaluate() -> all state writes       (unchanged)      │
              then, and only then:                                  │
                 emit is None                      -> nothing       │
                 suppress_from <= peak < suppress_until
                                                    -> metric + log, no clip
                 otherwise                         -> yield anomaly ──► ClipCreator
```

Operator signals added (all per broadcaster, all distinguishable — NFR-004,
NFR-006):

| Signal | Answers |
|---|---|
| `clips_suppressed_total{broadcaster_id, notice_type}` + structured log | Was a would-have-clipped spike suppressed, on which channel, by which notice category (FR-012, NFR-006). The `broadcaster_id` label is **required** by NFR-006 and is the same finite monitored-channel label policy the existing `anomalies_detected_total` already uses; only reason/category labels need bounding |
| `eventsub_channel_coverage{state="complete"\|"chat_only"\|"notification_only"\|"degraded_chat_only"}` | Is dual coverage complete, which half is missing, and is a channel inside a bounded auxiliary-refusal hold-off (FR-001, FR-002, FR-015, NFR-003) |
| `suppression_notices_ignored_total{notice_type}` | Was a notice correctly dropped because its category is outside the trigger set, with an `other` bucket for unrecognised values (FR-005, research R7) |
| `suppression_notices_malformed_total{reason}` | Was a notice dropped for untrustworthy identity or time (FR-017) |
| `suppression_records_rejected_total{reason}` | Did the consumer refuse a record for decode, `schema_version`, field-type/category reasons, or an occurrence time beyond the fixed 30-second future trust bound (contract §4, FR-017) |
| `suppression_records_consumed_total{lag_class="healthy"\|"lagging"}` + `suppression_delivery_age_seconds` observed **once per received record** | Is suppression delivery healthy, lagging, or idle/unknown, while detection continues fail-open (NFR-005). Silence produces no sample, which is exactly what makes idle/unknown distinguishable from healthy |
| `desired_set_churn_total` | Is the zero-width 400/400 band actually thrashing the monitored set, and does it stay inside the NFR-007 bound (research D10) |

Delivery health is read over a Prometheus window, not from a level:
`increase(suppression_records_consumed_total[W]) == 0` is **idle/unknown**;
`increase(suppression_records_consumed_total{lag_class="lagging"}[W]) > 0` is
**lagging**; otherwise **healthy**. There is deliberately no continuously
refreshed per-channel delivery gauge driven from `on_timer` — that would have to
invent a value during legitimate silence, and inventing one is what makes
"complete coverage plus silence" look falsely healthy (decision 20).

For each decoded and field-valid record, `process_element2` captures
`consumer_receipt_ms` from its injected/current consumer clock and first
enforces fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`. A record at
`consumer_receipt_ms + 30_000` is accepted; one millisecond beyond is rejected
as malformed fields, counted and logged, before any delivery observation or
state write. Each accepted record then computes
`delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`. That value is
the sole input to the lag threshold and is observed as seconds in
`suppression_delivery_age_seconds`. Accepted negative raw age is clamped to
zero and structured-logged as clock skew. Optional `received_at_ms` can
diagnose Twitch-to-producer versus producer-to-consumer latency, but
classification never reads it and no logic depends on its presence.

`anomalies_detected_total` keeps incrementing for a suppressed decision, so
"detected but not clipped" stays computable and the existing
`AnomalyDetectionStalled` alert keeps its meaning (research R8).

## Requirements coverage

| Requirement | Where it is satisfied |
|---|---|
| FR-001, FR-004 | Phase 1 pool pair placement; one `channel.chat.notification` subscription supplies gift and raid; the bounded auxiliary-refusal hold-off (`AUXILIARY_REFUSAL_RETRY_SECONDS`, reconnect re-eligibility) keeps the degraded exception temporary |
| FR-002, NFR-003 | Per-(channel, type) slots and the joined `list()`; both partial states converge without duplicating the surviving type; `degraded_chat_only` expires back to `chat_only` and resumes ordinary repair |
| FR-003, FR-005 | `map_suppression_event` allow-list; contract identity/notice/time fields; `suppression_notices_ignored_total` |
| FR-006, FR-010, SC-005 | `apply_notice()` notice-bounded interval transition: overlapping extension preserves/minimizes start, earlier/equal candidate is a full no-op, notice at/after deadline starts a new interval (data-model §3) |
| FR-007, SC-003 | Output-only gate in `on_timer` using `suppress_from_ms <= peak_ms < suppress_until_ms`; pre-notice peaks and exact-deadline peaks remain eligible |
| FR-008, FR-009, SC-004, SC-007 | Gate placement after every state write; `evaluate()` untouched |
| FR-011, NFR-005, SC-010 | Absent state = not suppressed; coverage state plus the received-record delivery classification (healthy / lagging / idle-unknown) against `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` |
| FR-012, NFR-006 | `clips_suppressed_total{broadcaster_id, notice_type}` + structured log, distinct from coverage/delivery/ignored/malformed/rejected/capacity signals |
| FR-013, FR-014, NFR-001, SC-006 | T026 capacity proof, then T027/T028 ramp tests/change; 400 × 2 = 800 ≤ 900, independent of later runtime producer/topic work T033-T036 |
| FR-015 | Subscription-counting occupancy vs channel-counting coverage gauge |
| FR-016 | `user:read:chat` already covers both types (research §1.3) |
| FR-017 | Producer drops untrustworthy notices and counts them; after schema/field decode the consumer rejects timestamps beyond fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` as malformed fields before delivery observation or state write |
| FR-018 | State read at decision time only; no retraction path exists |
| NFR-002 | Both inputs keyed on payload `broadcaster_id`; state is per-key |
| NFR-004 | Coverage, ignored, malformed, rejected, capacity-refusal and delivery signals are separate series |
| NFR-007, SC-011 | `desired_set_churn_total` measured over 24 deployed hours at 400/400 against the 8-changes-per-poll bound (E2/B4); exceeding it blocks gating and requires a spec change, never a code workaround |
| SC-001, SC-002, SC-008 | Coverage gauge including `degraded_chat_only` + notice allow-list tests + the signal set above |

## Exact surfaces this feature touches

Anchors are current line numbers, for orientation; they will drift as work
lands.

### Stream monitoring

| Surface | Anchor | Change |
|---|---|---|
| `CHAT_MESSAGE_SUBSCRIPTION_TYPE` | `eventsub_pool.py:105` | Add the notification type; every `sub_type` filter becomes a set |
| `map_chat_message` | `:236` | Untouched; new `map_suppression_event` sits beside it and reuses `to_epoch_ms` (`:177`) |
| `_Slot` / `_slots` / `_by_subscription` | `:316`, `:368` | Keyed by (channel, type); new channel-level coverage view |
| `create()` | `:441` | Creates only missing types; per-type 409 adoption; per-type session stamping |
| `delete()` | `:596` | Deletes both types for the channel; partial failure keeps the surviving slot |
| `list()` | `:640` | Two type-filtered walks joined per channel; a channel is actual only when both are `enabled` |
| `occupancy()` | `:717` | Still subscriptions per connection; coverage is a separate signal (FR-015) |
| `route()` / `_reserve()` | `:730`, `:748` | Pair-aware placement and reservation; prefer the connection already holding the channel |
| `_classify()` | `:997` | Auxiliary-only refusal is not propagated as a channel refusal; it starts a bounded `AUXILIARY_REFUSAL_RETRY_SECONDS` hold-off rather than a permanent one (research D2, decision 17) |
| `_adopt_conflict()` | `:1074` | Matches type + broadcaster |
| `_connection_holds()` / `_live_subscription_ids()` | `:1264`, `:1320` | Add `sub_type` matching — without it a chat-only channel reads as current for the notification type |
| `_forget_revoked()` / `_forget_unrecognised()` | `:944`, `:1288` | Forget one type, keep the other; channel becomes partially covered |
| `_on_eventsub_message` | `stream_monitoring_service.py:545` | New sibling handler for notifications |
| `_publish_lifecycle_event` | `:1193` | The pattern `_publish_suppression_event` copies (key, `produce`, `poll(0)`, `kafka_messages_produced`) |
| desired-set write/publish site | `stream_monitoring_service.py` poll result | Compute bounded `desired_set_churn_total` as entered plus departed channels, with no per-channel labels |
| `resolve_thresholds` / `compute_desired_set` | `:105`, `:136` | Unchanged code; new tests pin 400/400 and the zero-band consequence (research D10) |

### Flink job

| Surface | Anchor | Change |
|---|---|---|
| `WATERMARK_OUT_OF_ORDERNESS_SECONDS` / `WATERMARK_IDLENESS_SECONDS` | `spike_detector.py:99`, `:157` | Unchanged; new `SUPPRESSION_IDLENESS_SECONDS = 5`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS = 30`, and fixed non-environment `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30` documented against them |
| `DetectorConfig` | `:217` | Unchanged; new `SuppressionConfig.from_env()` and pure `SuppressionSourceSettings` beside it |
| `evaluate()` | `:504` | **Unchanged** — the gate is outside it |
| `AnomalyDetector.open` | `clip_detector_job.py:656` | Registers the `suppression` `ValueState` with the existing TTL config |
| `AnomalyDetector.process_element` | `:695` | Becomes `process_element1`, body unchanged |
| new `process_element2` | — | Decode and validate fields; reject over-bound future time before delivery observation/state; otherwise classify delivery and apply the interval transition. No timer, no output |
| `AnomalyDetector.on_timer` | `:717` | Gate at the `yield` only using the half-open notice-bounded interval; every state write above it unchanged |
| `main()` pipeline | `:1045`-`:1090` | Second `KafkaSource` + watermark strategy; `connect().key_by(...).process(...)` |
| `_init_metrics` | `:88` | New counters/gauges registered the same way |

### Tests

| File | Anchor | Coverage added |
|---|---|---|
| `test_stream_monitoring.py` | `FakeWebsocket` `:4619`, `make_pool` `:4724`, `TestPool*` `:4748`-`:5943` | `FakeWebsocket.listen_channel_chat_notification`; 150-channels-per-session; mixed types on one session; both ids tracked; per-type route/list/create/adopt/drop/revoke/reconnect; chat-only and notification-only partial states; 409 for either type; occupancy in subscriptions vs coverage in channels; mid-ramp reconnect and rebalance; 400/400 config; bounded auxiliary-refusal hold-off, its expiry, and reconnect re-eligibility; producer key/payload equality |
| `test_spike_detector.py` | whole file | Interval start/end transition, equal/earlier full-state no-ops, duplicate idempotence, new interval at/after deadline, per-category windows, both gate boundaries, pre-notice eligibility, fixed future-skew constant, absent state fails open; `SuppressionSourceSettings` values; static `docker-compose.yml` assertions. **Guaranteed offline** — no PyFlink import, so this file is the non-skippable evidence |
| `test_replay.py` | whole file | Suppression stream silent while chat fires; suppressed vs unsuppressed replay produce identical state and differ only in emission; a pre-notice peak reported later remains eligible; late notice does not retract; sparse idle → active re-entry holds the simplified two-input watermark by no more than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` |
| `test_clip_detector.py` | whole file | Suppression record decode, malformed and over-future records ignored before observation/state, exact future-bound acceptance, unknown `schema_version` ignored, operator and topology wiring against fakes. **Conditional** — it imports `clip_detector_job`, so it runs only when the pinned `apache-flink==1.18.0` is already installed; it never starts a cluster or MiniCluster |

Note: there is **no** `test_eventsub_pool.py` in this checkout. All pool tests
live in `test_stream_monitoring.py`; extend those classes rather than creating a
new file.

## Phases and dependency ordering

The ordering rule that must not be relaxed: **pool capacity safety is proved by
deterministic tests before the ramp configuration changes, and in deployment
the ramp reduction converges before the two-subscription transport ships
(research D12).**

| Phase | Work | Depends on | Exit gate |
|---|---|---|---|
| **1 — Contract, mapping, and pool primitives** | Producer contract tests and pure `map_suppression_event` (T004/T005); type-aware slots and routing/reservation | — | Payload shape and pure mapping are fixed; the pool can represent and reserve both coverage types |
| **2 — Pool lifecycle and capacity proof** | Create/adopt/delete, revoke/reconnect/retire, coverage, units, and deterministic capacity execution through T026 | 1 | T026 proves with deterministic fakes that 400 channels use 800 subscriptions, no session exceeds 300, and the 401st channel is refused |
| **3 — Capacity-safe ramp** | T027 adds failing 400/400/churn assertions; T028 changes thresholds and bounded churn accounting | **2 (T026)** | T027/T028 complete after the pool proof. This stage has no dependency on runtime producer, publisher, handler, or topic tasks T033-T036 |
| **4 — Runtime producer and topic** | T033-T036 notification callback, publisher, producer observability, and `kafka-init` topic creation | 1, 3 | Runtime publication tests pass against the already-fixed contract; the topic exists with four partitions |
| **5 — Detector** | `SuppressionState` + gate arithmetic and `SuppressionSourceSettings` in `spike_detector.py`; `KeyedCoProcessFunction`; second source, watermarks, idleness, `latest()` offsets built from those settings; gate; metrics and log | 4 | Pure-arithmetic and source-settings tests pass with no PyFlink import; gated vs ungated replay state-identical (SC-004) |
| **6 — Replay harness, docs, observability** | `tools/replay.py` suppression input; OPERATIONS.md runbook; alert-impact notes | 5 | Replay determinism holds; runbook documents every new signal |
| **7 — Integration pass** | Cross-surface consistency across all changed files | 1-6 | Targeted suites green; no unresolved clarification markers anywhere in the artifacts; contract referenced by both producer and consumer tasks |
| **8 — Deployed validation** | E1-E5 from research §9, including the 24-hour churn observation against NFR-007/SC-011 | 7 | Run by the operator on the configured machine; **not** claimable here |

Within the pool stages, the sub-ordering matters as well: slot/index model → routing
and reservation → create/adopt → delete → revocation/reconnect/retire →
coverage and metrics. Each step leaves the pool self-consistent, and each is
independently testable.

## Rollout and rollback

### The deployment-ordering hazard, and the rule that avoids it

The desired set lives in **Redis** (`chat:desired`), not in the container. On
restart the reconciler reads whatever set is already there and starts
converging to it immediately, while the poller only rewrites it on its next
tick (up to `POLL_INTERVAL_SECONDS` = 120 s later). If the feature revision —
threshold change and two-slot transport together — were deployed against a
Redis set still holding ~800 channels, the pool would try to create ~1,600
subscriptions against a 900 ceiling before the first poll landed: mass
refusals, `pool is at its 3-connection limit`, and a half-covered set to
untangle.

**Rule: reduce the ramp first, on the current single-subscription revision,
and let the desired set converge to 400 in Redis *before* deploying the
two-subscription revision.** Lowering thresholds on the old code is always
safe — 400 channels × 1 subscription = 400 of 900 — and it guarantees the new
transport never sees a set larger than its capacity model allows. Because that
preliminary ramp-down is arithmetically safe on its own, it is **not** gated by
E1: E1 gates dual-coverage sign-off and enabling gating, not the
single-subscription ramp-down.

### Forward order

1. **Ramp down on the current revision** — `JOIN_THRESHOLD=400`,
   `LEAVE_THRESHOLD=400`, recreate `stream-monitoring`, wait for
   `eventsub_subscription_count` and `ZCARD chat:desired` to settle at ~400.
   Unconditionally capacity-safe at 400 × 1; no evidence gate blocks it.
2. **Deploy the feature revision with the checked-in
   `SUPPRESSION_GATING_ENABLED=false`** — the pool converges to ~800
   subscriptions across the same 400 channels. Detection behaviour is unchanged
   from pre-007 throughout, and no operator action is needed to keep it that
   way, because `false` is what is in `docker-compose.yml`.
3. **E1** — confirm both types on live sessions and `total_cost` still 0. A
   non-zero cost blocks dual coverage and the feature; roll back the transport
   revision at once, using the rollback order below (research R6).
4. **E2** — coverage complete, 800 subscriptions, no connection over 300, ≥100
   slots free, 401st channel not admitted. Start the 24-hour
   `desired_set_churn_total` observation here and hold it against the NFR-007
   bound of 8 membership changes per poll (SC-011).
5. **E3** — with the suppression topic silent for at least an hour, chat
   detection and chat watermark lag are unchanged; and after that silence, a
   single isolated notice does not hold the operator watermark for longer than
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`.
6. **Enable gating** — only after E1, E2 (including the 24-hour churn
   observation against NFR-007), and E3 have passed, change the compose value to
   `SUPPRESSION_GATING_ENABLED=true`; then **E4** against a captured gift/raid
   slice.
7. **E5** — rollback rehearsal.

### Rollback order, capacity-safe by construction

The governing invariant, which overrides convenience at every step:
**thresholds MUST NEVER be raised above 400 while any
`channel.chat.notification` subscription still exists.** At 2 subscriptions per
channel, any threshold above 400 permits more than 800 subscriptions and can
cross the 900 ceiling; the only safe sequence therefore unwinds capacity
*before* it relaxes the threshold, not after.

1. **Stop gating.** Set `SUPPRESSION_GATING_ENABLED=false`. Clip behaviour
   returns to pre-007 immediately; subscriptions and topic stay in place
   (research D11). This is the whole rollback for a detection-policy problem.
2. **Unwind the transport while thresholds stay at 400/400.** Revert the
   two-subscription revision with `JOIN_THRESHOLD`/`LEAVE_THRESHOLD` still
   `400`/`400`. The single-subscription revision at 400 channels needs 400 of
   900 slots, so this step is safe at every instant, including while the old
   notification subscriptions are still being torn down.
3. **Wait for convergence before touching thresholds.** Continue only once
   `channel.chat.notification` subscriptions are gone from the enumeration,
   `eventsub_subscription_count` has fallen to approximately the desired channel
   count (~400 rather than ~800), and the coverage and desired-set metrics are
   stable.
4. **Only then, raise thresholds.** With no notification subscriptions left,
   thresholds may be raised back toward the single-subscription ramp. Doing this
   before step 3 completes is the unsafe reverse order and is forbidden.
5. The `suppression-events` topic may be left in place; an unread topic with
   short retention costs nothing and removing it is not part of rollback.

There is no schema migration, dependency change, token change, or Redis layout
change to reverse.

## Complexity Tracking

> Filled only where the Constitution Check needs justification.

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| A second Kafka topic | The detector needs a per-channel signal at decision time, and the constitution requires Kafka for inter-service messaging | Reusing `chat-messages` would break its frozen schema contract and force the notice through `CommandFilter` and the chat mapper (research D3) |
| A two-input Flink operator | Suppression state must be keyed by broadcaster and read inside the same keyed context as the detector state | Broadcast state fans per-channel data to every subtask and gives up keyed isolation (NFR-002); an external lookup adds I/O to the per-second hot path |
| Two subscriptions per channel | FR-001 requires both coverage types for every monitored channel, and one `channel.chat.notification` subscription supplies both gift and raid notices | A separate `channel.raid` subscription would be a third slot per channel and is explicitly out of scope |

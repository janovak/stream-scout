# Implementation Plan: Suppress Gift and Raid Chat Bursts

**Branch**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04 (capacity amendment 2026-09-05) | **Spec**: [spec.md](./spec.md)
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
2. **Two subscriptions per channel halve the ceiling, and the amended model
   spends it exactly.** 450 channels × 2 = 900 of the 900 available
   subscription slots, with **no guaranteed free reserve**. `JOIN_THRESHOLD`
   moves from 800 to **400** and `LEAVE_THRESHOLD` from 900 to **450**
   (FR-013, decision 27) — the same entry-gate/retention-band/exact-ceiling
   shape as the pre-007 800/900 ramp, halved. Exact capacity is only safe with
   decision 28's hardening: pair placement that can reserve one slot on each of
   two connections atomically when no single connection holds two, a distinct
   `PoolCapacityError` and capacity metric classification, no transient growth
   backoff for a hard ceiling, and `full_at` cleared and re-evaluated on
   reconnect/retirement with below-cap full state exposed. Its capacity
   behaviour must be proved by deterministic tests **before** the retention
   threshold moves to 450, and the deployed ramp reaches 450 only after E1 and
   E2a.
3. **A second Flink input can freeze event time.** A two-input operator's
   watermark is the minimum of its inputs, and the suppression stream is silent
   for hours at a time. It gets real watermarks, idleness shorter than the chat
   stream's, `latest()` offsets, and partitions matching parallelism
   (research D4, R3). Those watermarks are attached with
   `DataStream.assign_timestamps_and_watermarks()` **after** `from_source`,
   because PyFlink 1.18's `from_source` forwards only the Java strategy and
   silently ignores a Python timestamp assigner; the same correction is applied
   to the chat source so both inputs stay on Twitch's clock (research §4.1.2,
   R13, decisions 25-26). Each real strategy is built in the binding order
   bounded out-of-orderness → idleness → Python timestamp assigner **last**,
   because PyFlink 1.18's `with_idleness()` returns a fresh wrapper and drops
   an assigner stored earlier. Both assigners enforce the fixed 30-second
   future trust bound before watermark generation: over-bound suppression
   occurrence or chat `sent_at` falls back to the Kafka record timestamp
   without changing or dropping the payload, preventing an untrusted value
   from irreversibly poisoning either input watermark (research R12,
   decisions 24 and 26). One residual case is accepted rather than engineered
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
**Capacity**: 3 websocket connections × 300 enabled subscriptions = 900 per client-id/user-id; entry threshold 400, retention-and-maximum threshold 450; 450 monitored channels × 2 subscriptions = 900 steady state; **0 guaranteed free slots** (FR-014, decisions 27-28)
**Suppression windows**: gift 120 s, raid 180 s, operator-configured, applied at the **consumer** (research D7); raid viewer count never affects duration
**New configuration surface**: `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_GATING_ENABLED`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30` on **both** Flink blocks; `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` on `stream-monitoring`. `docker-compose.yml` checks in `SUPPRESSION_GATING_ENABLED=false`; the code-level `SuppressionConfig` default stays `true` (decision 21). `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` is a fixed source-event-time bound for both inputs and a suppression-operator contract constant, not a configuration value or environment variable (decisions 23-24, 26)
**Offline testability**: the suppression source's settings — topic name, `latest()` offset mode, bounded out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS = 5`, expected 4 partitions and parallelism 4, plus pure fields `delivery_lag_warn_seconds=30` and `checked_in_gating_enabled=False` — and fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` live in `spike_detector.py`, so they are asserted in `test_spike_detector.py` and against `docker-compose.yml` without importing PyFlink. Both timestamp assigners use the same injectable/current source wall clock: suppression and plain-integer chat time at +30,000 ms are assigned directly, while +30,001 ms and missing/unreadable values (plus chat string/float/bool values) use Kafka `record_timestamp` without rewriting or dropping the payload. Fakes can assert post-source attachment and the exact bounded → idleness → assigner-last builder order, but no local test proves the real PyFlink chain executes either assigner or measures the two added Python stages — that is E3 (research §4.1.2, §4.7, R13-R14). `SuppressionConfig.from_env()` remains the runtime reader and its code default for gating remains `true`
**Constraints**: the suppression input must never become the binding watermark minimum (research R3); every real `WatermarkStrategy` must be built bounded out-of-orderness → `with_idleness()` → `with_timestamp_assigner()` last, then attached with `DataStream.assign_timestamps_and_watermarks()` rather than passed to `env.from_source`, on both streams, or the Python timestamp assigner never executes (research §4.1.2, R13-R14); the topology must keep topic partitions = source parallelism = assignment parallelism = 4 with no repartition between them; the `chat-messages` schema stays frozen (spec 004 FR-008); `evaluate()` stays pure and signature-compatible
**Scale/Scope**: up to 450 channels, up to 900 subscriptions, ~3 suppression events per channel per hour at the high end — a topic that is silent for most seconds on most partitions

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
                                   #   stream-monitoring: JOIN_THRESHOLD 400 / LEAVE_THRESHOLD 450
                                   #   (checked-in target; deploy at 400/400 until E1/E2a),
                                   #   AUXILIARY_REFUSAL_RETRY_SECONDS=3600

OPERATIONS.md                      # entry 400 / maximum 450, exact 900-slot capacity,
                                   #   dual coverage, suppression runbook,
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
  `WatermarkStrategy` **from** that construct rather than from inline literals,
  and attaches the strategy with
  `DataStream.assign_timestamps_and_watermarks()` on the stream returned by
  `env.from_source(source, WatermarkStrategy.no_watermarks(), ...)`. The same
  post-source attachment is applied to the existing chat source, because
  PyFlink 1.18 silently ignores a Python assigner handed to `from_source`
  (research §4.1.2, decision 25). For both sources the real strategy is built
  in this exact order: bounded out-of-orderness, `with_idleness(...)`, then
  `with_timestamp_assigner(...)` last. PyFlink 1.18's `with_idleness()` returns
  a fresh wrapper without an assigner stored earlier (decision 26).
  Its `SuppressionTimestampAssigner` uses the same injectable/current
  source/receipt wall-clock basis as `process_element2`: it assigns a parsed
  `occurred_at_ms` only through +30,000 ms, otherwise using the Kafka
  `record_timestamp` for event time while preserving the original payload for
  downstream validation and rejection.
  `SentAtTimestampAssigner` accepts only a plain `int` (explicitly not `bool`)
  through the same +30,000 ms source-clock boundary. Missing/null, string,
  float, bool, and +30,001 ms values use Kafka `record_timestamp`; the chat
  payload continues downstream unchanged and is never dropped for this reason.
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
| `docker-compose.yml` `flink-jobmanager` **and** `flink-taskmanager` | Add `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and `SUPPRESSION_GATING_ENABLED=false` to **both** blocks — the existing `DETECTION_*` variables are duplicated across both for the same reason. The checked-in gating value is `false`; the code default in `SuppressionConfig` stays `true`, so a deploy is inert until an operator flips the compose value after E1, E2a, E2b, and E3 (decision 21, decision 27) |
| `docker-compose.yml` `stream-monitoring` | Checked-in target `JOIN_THRESHOLD=400`, `LEAVE_THRESHOLD=450`, with the comment explaining the entry/retention split, the two-subscriptions-per-channel arithmetic, the exact 450 × 2 = 900 ceiling, and that the *initial* dual-transport deployment overrides the retention threshold to `400` until E1/E2a pass (T063, decision 27); `AUXILIARY_REFUSAL_RETRY_SECONDS=3600` for the bounded notification-refusal backoff (decision 17) |
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
  chat-messages ──► assign_timestamps_and_watermarks(SentAtTimestampAssigner)
                    ──► CommandFilter ──► key_by(broadcaster_id) ─┐
                                                              ├─► AnomalyDetector
  suppression-events ──► assign_timestamps_and_watermarks(SuppressionTimestampAssigner)
                         ──► key_by(broadcaster_id) ─┘
                         both sources enter with WatermarkStrategy.no_watermarks();
                         each real strategy is built in binding order:
                         out-of-orderness → idleness → Python assigner LAST,
                         then attached here, because PyFlink 1.18's
                         from_source silently ignores a Python assigner
                         and with_idleness drops an assigner stored before it
                         chat sent_at: plain int and <= source clock +30,000
                         otherwise: Kafka record_timestamp; payload continues
                         parsed occurred_at <= source clock +30,000: use occurred_at
                         missing/unreadable/over-bound: use Kafka record_timestamp
                         (watermark only; original payload is unchanged)
                                                                    (KeyedCoProcessFunction)
                                                                    │
   process_element1: bucket + arm timer            (unchanged)      │
   process_element2: decode original payload/fields                 │
                     → fixed future-time trust check again          │
                     → apply [suppress_from, suppress_until)         │
                     (no timer; rejected future record logs/counts,
                      observes no delivery and accesses/writes no state)
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
| `desired_set_churn_total` | How much the 400/450 entry-retention band moves the monitored set. Advisory bounded-label telemetry with no numeric release gate (decision 27, research D10a) |
| `eventsub_connection_full{connection}` and the capacity classification on `subscription_create_failures_total{reason="capacity"}` | Is the account exactly full, is a specific connection full **below** the 300 cap, and is this a capacity condition rather than a provider refusal or a transient fault (FR-015, NFR-001, NFR-004, decision 28) |

Delivery health is read over a Prometheus window, not from a level:
`increase(suppression_records_consumed_total[W]) == 0` is **idle/unknown**;
`increase(suppression_records_consumed_total{lag_class="lagging"}[W]) > 0` is
**lagging**; otherwise **healthy**. There is deliberately no continuously
refreshed per-channel delivery gauge driven from `on_timer` — that would have to
invent a value during legitimate silence, and inventing one is what makes
"complete coverage plus silence" look falsely healthy (decision 20).

Before that operator path, `SuppressionTimestampAssigner` captures
`source_clock_ms` from the same injectable/current wall-clock basis. A parsed
occurrence at `source_clock_ms + 30_000` remains the assigned event timestamp;
one at +30,001 ms uses the Kafka record timestamp, as missing/unreadable values
already do. This is watermark-only substitution: the record value is unchanged
so downstream observability is retained.

That assigner runs only because the strategy carrying it is attached with
`DataStream.assign_timestamps_and_watermarks()` on the stream returned by
`env.from_source(...)`. PyFlink 1.18 forwards only `_j_watermark_strategy` from
`from_source`, so a Python assigner passed there is discarded without an error
and event time silently falls back to the Kafka record timestamp. The chat
source is corrected the same way in the same change: Feature 007 compares a
chat peak second against a suppression interval, so both must be on Twitch's
clock, and correcting only one side would turn a uniform mismatch into a
cross-clock mismatch inside the comparison (research §4.1.2, decision 25).
For both strategies, `with_timestamp_assigner(...)` is called only after
bounded out-of-orderness and `with_idleness(...)`; calling idleness last would
replace the Python wrapper and discard `_timestamp_assigner` (decision 26).
The now-live `SentAtTimestampAssigner` accepts only a plain `int` (not `bool`)
at or before `source_clock_ms + 30_000`. Missing/null, string, float, bool, and
one-millisecond-over-bound `sent_at` use Kafka `record_timestamp` for event
time while the chat record continues unchanged. Chat is never rejected or
dropped by this fallback.
Because idleness is now emitted by that assignment operator per subtask rather
than by the Kafka source per split, the topology invariant becomes explicit:
topic partitions = source parallelism = assignment parallelism = 4, chained
one-to-one, which makes per-subtask idleness equivalent to per-split idleness.
A later mismatch or rescale invalidates that equivalence and requires
revalidation (research R13).

For each decoded and field-valid original record, `process_element2` captures
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
| FR-013, FR-014, NFR-001, SC-006 | T026 capacity proof, then T027/T028 ramp tests/change at 400/400; amended by T060-T063, which prove the 400 entry gate, the 450 retention maximum, exact 450 × 2 = 900 occupancy, the 451st exclusion, and split-fragment replacement before the retention threshold moves to 450 |
| FR-015 | Subscription-counting occupancy vs channel-counting coverage gauge, plus the entry/maximum boundaries and the below-cap connection-full signal (T063) |
| FR-016 | `user:read:chat` already covers both types (research §1.3) |
| FR-017 | Producer drops untrustworthy notices and counts them; the timestamp assigner — attached post-source so it actually runs — uses Kafka `record_timestamp` instead of over-bound occurrence time before watermark generation without rewriting the payload; after schema/field decode the operator rejects that original timestamp beyond fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` as malformed fields before delivery observation or state access |
| FR-018 | State read at decision time only; no retraction path exists |
| NFR-002 | Both inputs keyed on payload `broadcaster_id`; state is per-key |
| NFR-004 | Coverage, ignored, malformed, rejected, capacity-refusal and delivery signals are separate series |
| NFR-007, SC-011 | `desired_set_churn_total` remains an advisory bounded-label counter; T064 keeps the metric and removes the numeric release gate, so nothing blocks enabling gating on a churn observation |
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
| `route()` / `_reserve()` | `:730`, `:748` | Pair-aware placement and reservation; prefer the connection already holding the channel, then a single connection with room for the whole pair, then — when no connection has two free slots but the pool has at least two — reserve one slot on **each** of two connections atomically in one critical section (decision 28) |
| `_grow()` and create-failure classification | `:730`-`:760`, `:997` | A hard 900-of-900 or three-connection exhaustion raises a distinct `PoolCapacityError`, is counted under a capacity reason rather than a refusal or transient reason, and **does not** arm the transient growth backoff (decision 28) |
| `full_at` on `_Connection` | `:717` | Cleared and re-evaluated on reconnect and retirement; a connection full **below** the 300 cap is exposed as stranded capacity rather than left silent (decision 28) |
| `_classify()` | `:997` | Auxiliary-only refusal is not propagated as a channel refusal; it starts a bounded `AUXILIARY_REFUSAL_RETRY_SECONDS` hold-off rather than a permanent one (research D2, decision 17) |
| `_adopt_conflict()` | `:1074` | Matches type + broadcaster |
| `_connection_holds()` / `_live_subscription_ids()` | `:1264`, `:1320` | Add `sub_type` matching — without it a chat-only channel reads as current for the notification type |
| `_forget_revoked()` / `_forget_unrecognised()` | `:944`, `:1288` | Forget one type, keep the other; channel becomes partially covered |
| `_on_eventsub_message` | `stream_monitoring_service.py:545` | New sibling handler for notifications |
| `_publish_lifecycle_event` | `:1193` | The pattern `_publish_suppression_event` copies (key, `produce`, `poll(0)`, `kafka_messages_produced`) |
| desired-set write/publish site | `stream_monitoring_service.py` poll result | Compute bounded `desired_set_churn_total` as entered plus departed channels, with no per-channel labels; advisory only |
| `resolve_thresholds` / `compute_desired_set` | `:105`, `:136` | Unchanged code; the existing `(previous | top_join) & top_leave` expression already implements entry 400 / retention 450. New tests pin 400/450, the 401-450 fresh-versus-incumbent asymmetry, and the 450 maximum |

### Flink job

| Surface | Anchor | Change |
|---|---|---|
| `WATERMARK_OUT_OF_ORDERNESS_SECONDS` / `WATERMARK_IDLENESS_SECONDS` | `spike_detector.py:99`, `:157` | Unchanged; new `SUPPRESSION_IDLENESS_SECONDS = 5`, `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS = 30`, and fixed non-environment `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30` documented against them |
| `DetectorConfig` | `:217` | Unchanged; new `SuppressionConfig.from_env()` and pure `SuppressionSourceSettings` beside it |
| `evaluate()` | `:504` | **Unchanged** — the gate is outside it |
| `AnomalyDetector.open` | `clip_detector_job.py:656` | Registers the `suppression` `ValueState` with the existing TTL config |
| `AnomalyDetector.process_element` | `:695` | Becomes `process_element1`, body unchanged |
| `SuppressionTimestampAssigner` / new `process_element2` | — | Before watermark generation, assign trusted occurrence time or fall back to Kafka record time for missing/unreadable/over-bound values without rewriting payload; then decode and validate the original payload in the operator and reject over-bound future time before delivery observation/state. No timer, no output |
| `SentAtTimestampAssigner` attachment | `clip_detector_job.py:1496`-`:1506` | Build bounded out-of-orderness → idleness → assigner last; accept only plain-int, at-most-+30 s `sent_at`, otherwise fall back to Kafka record time without dropping chat. `from_source` receives `no_watermarks()` and the real strategy is attached post-source (research §4.1.2, decisions 25-26) |
| `AnomalyDetector.on_timer` | `:717` | Gate at the `yield` only using the half-open notice-bounded interval; every state write above it unchanged |
| `main()` pipeline | `:1045`-`:1090` | Second `KafkaSource`; both sources entered with `WatermarkStrategy.no_watermarks()`; each real strategy is built bounded → idleness → assigner last and attached by `assign_timestamps_and_watermarks()`, creating two Python stages at parallelism 4; `connect().key_by(...).process(...)` |
| `_init_metrics` | `:88` | New counters/gauges registered the same way |

### Tests

| File | Anchor | Coverage added |
|---|---|---|
| `test_stream_monitoring.py` | `FakeWebsocket` `:4619`, `make_pool` `:4724`, `TestPool*` `:4748`-`:5943` | `FakeWebsocket.listen_channel_chat_notification`; 150-channels-per-session; mixed types on one session; both ids tracked; per-type route/list/create/adopt/drop/revoke/reconnect; chat-only and notification-only partial states; 409 for either type; occupancy in subscriptions vs coverage in channels; mid-ramp reconnect and rebalance; 400/450 entry-retention config and the fresh-versus-incumbent asymmetry at rank 401-450; exact 450 × 2 = 900 occupancy and the 451st exclusion; split-pair reservation when two free slots sit on two connections, its atomicity, and surviving-half retention; distinct capacity error/metric classification and no transient growth backoff on a hard ceiling; `full_at` clearing/re-evaluation and below-cap full exposure; bounded auxiliary-refusal hold-off, its expiry, and reconnect re-eligibility; producer key/payload equality |
| `test_spike_detector.py` | whole file | Interval start/end transition, equal/earlier full-state no-ops, duplicate idempotence, new interval at/after deadline, per-category windows, both gate boundaries, pre-notice eligibility, fixed future-skew constant, source timestamp assignment at exact/+1 ms boundaries with Kafka-record fallback and unchanged payload, absent state fails open; `SuppressionSourceSettings` values; static `docker-compose.yml` assertions. **Guaranteed offline** — no PyFlink import, so this file is the non-skippable evidence |
| `test_replay.py` | whole file | Suppression stream silent while chat fires; suppressed vs unsuppressed replay produce identical state and differ only in emission; a pre-notice peak reported later remains eligible; late notice does not retract; sparse idle → active re-entry holds the simplified two-input watermark by no more than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`; over-future input is assigned Kafka record time before downstream rejection and cannot advance the monotonic combined watermark |
| `test_clip_detector.py` | whole file | Suppression source timestamp assignment and operator decode, malformed and over-future original records ignored before observation/state, exact future-bound acceptance, unknown `schema_version` ignored, operator and topology wiring against fakes — including that **both** sources are entered with `WatermarkStrategy.no_watermarks()` and receive their real strategy through `assign_timestamps_and_watermarks()`. That is a wiring assertion only; it never claims the real PyFlink chain executed the assigner (research §4.7, R13). **Conditional** — it imports `clip_detector_job`, so it runs only when the pinned `apache-flink==1.18.0` is already installed; it never starts a cluster or MiniCluster |

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
| **3 — Capacity-safe ramp** | T027 adds failing 400/400 and churn assertions; T028 changes thresholds and bounded churn accounting | **2 (T026)** | T027/T028 complete after the pool proof. This stage has no dependency on runtime producer, publisher, handler, or topic tasks T033-T036 |
| **4 — Runtime producer and topic** | T033-T036 notification callback, publisher, producer observability, and `kafka-init` topic creation | 1, 3 | Runtime publication tests pass against the already-fixed contract; the topic exists with four partitions |
| **5 — Detector** | `SuppressionState` + gate arithmetic and `SuppressionSourceSettings` in `spike_detector.py`; `KeyedCoProcessFunction`; second source, watermarks attached post-source on **both** streams, idleness, `latest()` offsets built from those settings; gate; metrics and log | 4 | Pure-arithmetic and source-settings tests pass with no PyFlink import; gated vs ungated replay state-identical (SC-004) |
| **6 — Replay harness, docs, observability** | `tools/replay.py` suppression input; OPERATIONS.md runbook; alert-impact notes | 5 | Replay determinism holds; runbook documents every new signal |
| **7 — Integration pass** | Cross-surface consistency across all changed files | 1-6 | Targeted suites green; no unresolved clarification markers anywhere in the artifacts; contract referenced by both producer and consumer tasks |
| **8 — Deployed validation** | E1, E2a, the foreign-subscription sweep, the ramp to 450, E2b, and E3-E5 from research §9 | 7, 9 | Run by the operator on the configured machine; **not** claimable here |
| **9 — Capacity amendment (T059-T068)** | Decisions 27-28: entry 400 / retention-and-maximum 450, exact 900-slot occupancy, atomic split-pair reservation, distinct capacity error and metric, `full_at` clear/re-evaluate/expose, churn gate removal, artifact and runbook updates | 1-7 | Deterministic tests prove the 400/450 band, exact 450 × 2 = 900, the 451st exclusion, split placement, and capacity classification **before** the checked-in retention threshold moves to 450; deployed proof stays in phase 8 |

The ordering rule inside phase 9 mirrors the original one: the amended capacity
behaviour is proved by deterministic tests (T060-T062) before the runtime
ceiling and configuration change (T063), and the deployed ramp to 450 happens
only after E1 and E2a.

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

**Corollary for the amended thresholds (decision 27).** The final checked-in
target is `JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=450`, but the *initial*
deployment of the dual transport must run with the retention threshold
overridden to **400**, so the first dual-coverage convergence happens at
400 channels / 800 subscriptions with a whole 100-slot cushion. The ramp to 450
— and therefore to exact 900-of-900 occupancy — is a later configuration step,
taken only once E1 and E2a have passed and the account has been swept for
enabled subscriptions this feature did not create. Reaching exact capacity and
first proving the dual transport are two separate risks and are never taken in
the same step.

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
4. **E2a — dual coverage at 400/800, with a cushion.** Coverage complete,
   800 subscriptions, no connection over 300, roughly 100 slots free, and the
   401st channel not admitted while the retention threshold is still 400. This
   is the dual-transport proof, taken deliberately *before* exact capacity.
   `desired_set_churn_total` is read here as advisory context only; no
   observation window gates progression (NFR-007, SC-011, decision 27).
5. **Sweep the account for foreign enabled subscriptions.** Before spending the
   last 100 slots, enumerate every enabled subscription on the client-id /
   user-id pair and remove anything this feature's pool does not own — an
   earlier revision's leftovers, an orphan from a failed delete, another
   process's subscription. At exact capacity these are indistinguishable from a
   defect and consume slots the model has already allocated (decision 27).
6. **Ramp the retention threshold to the final 400/450.** Move
   `LEAVE_THRESHOLD` to `450` — the checked-in target — and let the monitored
   set converge to 450 channels and 900 subscriptions. Only incumbents ranked
   401-450 are added by this step, so the set grows gradually rather than in
   one jump.
7. **E2b — exact-capacity drills.** With relational equality, not an
   approximate reading: total subscriptions **equal** 900 and **equal** twice
   the complete-coverage channel count; every connection occupancy is **at
   most** 300 and their sum **equals** 900; free slots **equal** 0; the 451st
   qualifying channel is **excluded**. Then exercise the exact-capacity paths:
   a pair placed as one slot on each of two connections when no connection has
   two free; a deliberately deleted half converging back to complete coverage
   without exceeding 900; a capacity refusal reported under its own capacity
   classification rather than as a provider refusal, without arming a transient
   growth backoff; and a connection reporting full below 300 being visible and
   re-evaluated after a reconnect or retirement (decision 28).
8. **E3** — with the suppression topic silent for at least an hour, chat
   detection and chat watermark lag are unchanged; and after that silence, a
   single isolated notice does not hold the operator watermark for longer than
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`.
   A controlled over-future record must also use Kafka record time instead of
   its untrusted occurrence time, leave the combined watermark
   monotonic, and not stall real-time timers after chat idles and resumes.
   The same run must confirm the post-source assignment path is live on both
   streams: watermarks originate from the assignment operator, chat event time
   accepts only plain-int `sent_at` through the +30 s source bound (with
   missing/null/string/float/bool/+30,001 ms falling back without chat loss),
   suppression event time applies its corresponding bound, and each assignment
   subtask still maps to exactly one partition at parallelism 4 (research
   §4.1.2, R13-R14). Confirm the deployed job graph retains the assigner after
   idleness on both streams and record TaskManager Python process count and RSS
   for the two added parallelism-four Python stages; these are deployed
   observations, never local claims.
9. **Enable gating** — only after E1, E2a, E2b, and E3 have passed, change the
   compose value to `SUPPRESSION_GATING_ENABLED=true`; then **E4** against a
   captured gift/raid slice. No churn observation window blocks this step.
10. **E5** — rollback rehearsal.

### Rollback order, capacity-safe by construction

The governing invariant, which overrides convenience at every step:
**the retention threshold is never above 450 while two subscriptions per
channel are live, and it must be back at 400 before the dual transport is
unwound.** At 2 subscriptions per channel, a retention threshold above 450
permits more than 900 subscriptions; and unwinding the transport from 450
channels would leave a single-subscription revision converging to a set larger
than the ramp it is about to be given. Capacity is therefore always reduced
*before* the threshold is relaxed, never after.

1. **Stop gating.** Set `SUPPRESSION_GATING_ENABLED=false`. Clip behaviour
   returns to pre-007 immediately; subscriptions and topic stay in place
   (research D11). This is the whole rollback for a detection-policy problem,
   and it is not the right lever for a capacity problem.
2. **For a capacity incident, lower retention to 400 first and reconverge.**
   Set `LEAVE_THRESHOLD=400` — `JOIN_THRESHOLD` is already 400 — and wait for
   the monitored set to fall to 400 channels and 800 subscriptions. This is the
   fastest way back to a cushion, it requires no code change, and it leaves
   dual coverage intact while the cause is diagnosed. Fragmentation, a stranded
   below-cap `full_at`, a foreign subscription, or a failed delete are all
   diagnosed from this state rather than from the ceiling.
3. **Unwind the transport while thresholds stay at 400/400.** Revert the
   two-subscription revision with `JOIN_THRESHOLD`/`LEAVE_THRESHOLD` at
   `400`/`400`. The single-subscription revision at 400 channels needs 400 of
   900 slots, so this step is safe at every instant, including while the old
   notification subscriptions are still being torn down.
4. **Wait for convergence before touching thresholds.** Continue only once
   `channel.chat.notification` subscriptions are gone from the enumeration,
   `eventsub_subscription_count` has fallen to the desired channel count (400
   rather than 800), and the coverage and desired-set metrics are stable.
5. **Only then, restore the single-subscription ramp.** With no notification
   subscriptions left, thresholds may be raised back toward the pre-007
   single-subscription ramp. Doing this before step 4 completes is the unsafe
   reverse order and is forbidden.
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
| A pair split across two connections, reserved atomically | At exact capacity, parity fragmentation can leave two free slots on two different connections; refusing the pair would make a pool with room report itself full and stall convergence short of 450 (decision 28) | Pair compaction or migration between connections was rejected: it deletes and recreates a live subscription, opening a real coverage gap and needing its own ordering, failure, and idempotence rules, purely to recover locality that splitting already handles |

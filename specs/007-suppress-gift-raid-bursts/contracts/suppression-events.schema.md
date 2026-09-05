# Contract: `suppression-events` Kafka topic

**Feature**: `007-suppress-gift-raid-bursts` | **Version**: 1 | **Date**: 2026-09-04

The producer is `stream-monitoring` (`_publish_suppression_event`, modelled on
the existing `_publish_lifecycle_event`). The consumer is the Flink clip
detector (`AnomalyDetector.process_element2`). This file is the single
reference for both; producer tasks and consumer tasks must both cite it.

The topic carries a **notice**, never a decision. Window durations, the gate,
and every policy question are applied by the consumer (research D7), so
changing a window is a consumer configuration change and never invalidates
records already on the topic.

---

## 1. Topic

| Property | Value | Why |
|---|---|---|
| Name | `suppression-events` | New topic; `chat-messages` stays frozen (spec 004 FR-008) |
| Key | `str(broadcaster_id).encode("utf-8")` | Same keying convention as `chat-messages` and `stream-lifecycle`; keeps a channel's notices in one partition and in order. **Set and verified at the producer**: the Flink consumer deserializes values only and never sees the key (§4.0) |
| Value | UTF-8 JSON, one object per notice | Matches every other topic in this system |
| Partitions | **4** | Must equal `FLINK_PARALLELISM` so every source subtask owns exactly one split and split idleness is well defined (research §4.1) |
| Replication factor | 1 | Single-broker development stack, as with the existing topics |
| Retention | 1 hour (`retention.ms=3600000`) | A notice is actionable for at most 180 s; matching `chat-messages` retention keeps replay debugging possible without storing an operational log indefinitely |
| Produced by | `stream-monitoring` only | |
| Consumed by | Flink clip detector, `KafkaOffsetsInitializer.latest()` | Old notices must never be replayed into event time (research D4) |

---

## 2. Message schema, version 1

```json
{
  "schema_version": 1,
  "broadcaster_id": 123456789,
  "notice_type": "community_sub_gift",
  "occurred_at_ms": 1772668800123,
  "notice_id": "9c2b1f4e-...-a1",
  "received_at_ms": 1772668800298,
  "viewer_count": null
}
```

### 2.1 Required fields

| Field | Type | Source | Contract |
|---|---|---|---|
| `schema_version` | `int` | constant `1` | Present on every record. A consumer that does not recognise the value ignores the record (§4.2) |
| `broadcaster_id` | `int` | `event.event.broadcaster_user_id`, parsed to `int` | Channel identity. Must equal the key. Never guessed, never defaulted |
| `notice_type` | `str` | `event.event.notice_type` | Exactly one of `community_sub_gift`, `sub_gift`, `raid`. No other value is ever produced |
| `occurred_at_ms` | `int` | `to_epoch_ms(event.metadata.message_timestamp)` | Epoch **milliseconds**, Twitch's clock — the same clock and the same converter as `chat-messages.sent_at`. Never `null`, never a string |

### 2.2 Optional fields

Each optional field is justified individually; anything not justified here is
not carried.

| Field | Type | Justification | Consumer rule |
|---|---|---|---|
| `notice_id` | `str \| null` | Twitch's own `metadata.message_id`, which Twitch re-sends unchanged on redelivery. It is the only way an operator can tell a genuine second notice from a redelivery when reading the topic | **Diagnostic only.** The consumer must not use it for deduplication — `apply_notice`'s `max()` is already duplicate-safe (data-model I7), and a de-dup cache would add per-key state for no behavioural gain |
| `received_at_ms` | `int \| null` | Optional ingestion clock at the producer. When present, it can split Twitch-to-producer latency from producer-to-consumer latency | **Diagnostic only.** Never used for delivery-health classification, deadline or gate arithmetic, and no consumer logic may depend on its presence |
| `viewer_count` | `int \| null` | Present only for `raid`; `null` otherwise. Kept because the operator tuning question "should raid windows scale with audience?" was answered *no* (decision 1) and the data to revisit it must be visible on the topic rather than requiring a producer change | **Must not be read by any consumer logic.** Raid audience size never affects window duration (FR-006). A consumer test asserts that changing it changes nothing |

At `process_element2` receipt, the consumer captures
`consumer_receipt_ms` from its injected clock in tests and the current consumer
clock at runtime. After schema and field decode, it first enforces the fixed
contract constant `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30`:

```text
occurred_at_ms <= consumer_receipt_ms + 30_000
```

Equality is accepted. A record one millisecond beyond is rejected under
`reason="fields"`, counted and structured-logged, with no
`suppression_delivery_age_seconds` observation, no consumed classification,
and no state read or write. This is a fixed defence-in-depth bound, not an
environment variable. For every record that passes:

```text
raw_delivery_age_ms = consumer_receipt_ms - occurred_at_ms
delivery_age_ms = max(0, raw_delivery_age_ms)
```

`delivery_age_ms` is the only value compared with
`SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` and the value observed, in seconds, by
`suppression_delivery_age_seconds`. If `raw_delivery_age_ms` is negative within the allowed 30 seconds, the
consumer observes and classifies zero and emits a structured clock-skew
diagnostic log; no new metric is required. When `received_at_ms` is present it
may additionally support diagnostic decomposition into Twitch-to-producer
(`received_at_ms - occurred_at_ms`) and producer-to-consumer
(`consumer_receipt_ms - received_at_ms`) latency. Neither component is a
classification input.

### 2.3 Fields deliberately not carried

| Not carried | Reason |
|---|---|
| A computed `suppress_until_ms` | Bakes window policy into the topic; a tuning change would make retained records wrong (research D7) |
| `system_message`, `message`, fragments | Free user text on an operational topic, with no requirement to satisfy |
| `chatter_user_id`, `chatter_is_anonymous`, gifter identity | Suppression is channel-scoped; the gifter is irrelevant to it |
| `broadcaster_login` | The detector keys on id; a login would be a second identity to keep consistent |
| `sub_tier`, `total`, `cumulative_total` | Gift size does not affect the window (decision 1) |

---

## 3. Producer rules

The producer is the only place a record can be created, and it is the FR-017
enforcement point.

1. **Trigger allow-list.** Map an event only when `notice_type` is exactly
   `community_sub_gift`, `sub_gift`, or `raid`. Every other value — including
   `unraid`, `sub`, `resub`, any of the remaining documented categories, an
   absent attribute, and any future value Twitch adds — produces **no record**
   and increments `suppression_notices_ignored_total{notice_type}` with an
   `other` bucket for unrecognised values (research R7). Read the attribute
   with `getattr(data, "notice_type", None)`: pyTwitchAPI omits absent fields
   entirely rather than setting them to `None`.
2. **Identity validation.** `broadcaster_user_id` must be present and parse to
   an `int`. Otherwise: no record,
   `suppression_notices_malformed_total{reason="identity"}`, and a structured
   log carrying `notice_type` and `notice_id` if available.
3. **Time validation.** `to_epoch_ms(metadata.message_timestamp)` must return a
   non-`None` `int`. Otherwise: no record,
   `suppression_notices_malformed_total{reason="occurred_at"}`, and a
   structured log. **The ingestion clock must never be substituted** — that
   would fabricate a deadline, which FR-017 forbids. This is the one place the
   suppression path deliberately diverges from the chat path, which publishes
   `sent_at: null` rather than dropping a message (research D9, plan
   "Constitution re-check").
4. **Publication.** `producer.produce(topic="suppression-events", key=str(broadcaster_id).encode("utf-8"), value=..., callback=self._delivery_callback)` followed by `poll(0)` and
   `kafka_messages_produced.labels(topic="suppression-events").inc()` — the
   same shape as `_publish_lifecycle_event`.
5. **Key/payload equality is the producer's invariant to keep.** The key must
   be `str(payload["broadcaster_id"])` for every record. This is asserted in the
   producer's own tests, because the producer is the only party that can see
   both the key and the payload (§4.0, research D15).
6. **Failure containment.** A produce exception is logged and swallowed, exactly
   as the chat and lifecycle publishers do. A broker problem degrades
   suppression to absent, which the consumer treats as not-suppressed
   (FR-011). It must never propagate into the EventSub callback and disturb
   chat delivery on the same socket.
7. **No blocking, no retry loop.** The handler runs on the receiving socket's
   own event loop. It must do no I/O beyond the thread-safe `produce`/`poll`
   pair the chat path already uses.

---

## 4. Consumer rules

The consumer runs inside `AnomalyDetector.process_element2`, on the keyed
suppression input.

### 4.0 The consumer cannot see the Kafka key

The job builds its Kafka sources with a **value-only deserialization schema**,
exactly as it does for `chat-messages`. The record key is therefore not
available to `process_element2` at all.

Three rules follow, and they are binding on the task list:

1. **Routing, keying, and state use the payload `broadcaster_id`.** That field
   is the channel identity as far as the consumer is concerned, and it is what
   `key_by` derives the key from.
2. **The consumer performs no key-agreement check.** It cannot. A test asking
   it to compare key against payload would be asserting an unobservable, so no
   consumer task requires one; the equality invariant is verified at the
   producer instead (§3.5, §5.1).
3. **Malformed-payload validation is entirely the consumer's job**, and it is
   real work: a payload whose `broadcaster_id`, `notice_type`, or
   `occurred_at_ms` is missing or of the wrong type is rejected and counted
   (§4.3), and a field-valid payload with an occurrence time beyond the fixed
   future trust bound is then rejected under §4.4. "The consumer cannot check
   the key" is not a licence to skip payload checking — it is the reason
   payload checking is the only defence the consumer has.

### 4.1 Rules

1. **Decode defensively.** A record that is not valid JSON, or is not an
   object, is ignored: increment
   `suppression_records_rejected_total{reason="decode"}` and return. It must
   never raise out of `process_element2` — an exception there would fail the
   operator and stop chat detection for every key on the subtask.
2. **Version check.** `schema_version` must equal a known version. An unknown
   version is ignored with `reason="schema_version"`. A missing
   `schema_version` is treated as unknown. This is what makes a future field
   addition safe against a live topic (research D8).
3. **Field validation.** `broadcaster_id` must be an `int`, `notice_type` must
   be in the consumer's window map, and `occurred_at_ms` must be an `int`.
   Anything else is ignored with `reason="fields"`. In particular, a
   `notice_type` outside the trigger set is ignored rather than defaulted to a
   window (FR-005 defence in depth).
4. **Future-time trust, before observation or state.** Capture
   `consumer_receipt_ms`, then require
   `occurred_at_ms <= consumer_receipt_ms + 30_000` using fixed
   `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`. Reject one millisecond beyond with
   `reason="fields"` and a structured malformed-record log. Do not observe
   delivery age, increment a consumed lag class, read state, or write state for
   that record. Equality passes; accepted future skew is handled by rule 5.
5. **Delivery-health classification, on trusted receipt only.** For each
   record that passed rule 4, compute
   `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`, observe
   `delivery_age_ms / 1000` in `suppression_delivery_age_seconds`, and increment
   `suppression_records_consumed_total{lag_class}` with `lag_class="healthy"`
   when `delivery_age_ms` is at or below
   `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS * 1000` (default 30 s) and
   `lag_class="lagging"` when it is above; a lagging record also emits a
   structured log line. A negative raw age within the allowed 30 seconds is
   clamped to zero for both observation and classification and emits a
   structured clock-skew diagnostic log. Optional `received_at_ms` is
   diagnostic-only and never changes this classification. **Nothing is
   published for a window in which no record arrived** — that window is
   idle/unknown, and it is read as
   `increase(suppression_records_consumed_total[W]) == 0` in Prometheus. No
   per-channel delivery gauge is refreshed from `on_timer`, because during
   legitimate silence any value it published would be invented (NFR-005,
   research D13).
6. **Apply.** After delivery observation under rule 5,
   `state = apply_notice(state, notice_type, occurred_at_ms, config)` and write
   only when the value changed — the same write-only-on-change rule `hold`
   already uses.
7. **No timer, no output, no buffering.** `process_element2` registers nothing
   and emits nothing. Waiting for suppression before deciding is explicitly
   rejected: the spec requires fail-open, not a delay (decision 3).
8. **Lateness is not an error.** A record whose `occurred_at_ms` is behind the
   operator watermark is applied normally. It affects only decisions taken
   after it lands (FR-018), and it is classified through the delivery-age
   signals rather than dropped.

---

## 5. Invariants

1. Key equals `str(broadcaster_id)`. This is a **producer invariant**, asserted
   in producer tests where the key is observable (T033). The consumer
   deserializes values only and never sees the key, so it neither checks nor
   depends on this; it keys, routes, and validates on the payload
   `broadcaster_id`, and rejects a malformed payload under §4.1 rule 3
   (research D15, §4.0).
2. `occurred_at_ms` is epoch **milliseconds** on the same clock as
   `chat-messages.sent_at`, produced by the same `to_epoch_ms` converter. This
   is what makes
   `suppress_from_ms <= peak_second * 1000 < suppress_until_ms` a meaningful
   comparison.
3. `notice_type` on the topic is always one of the three trigger values. The
   consumer's allow-list is defence in depth, not the only filter.
4. The record is immutable and self-contained: no consumer needs any other
   record, any earlier record, or any external lookup to apply it.
5. The active interval is half-open and notice-bounded. An overlapping
   extending notice preserves the earliest retained start and advances the
   deadline; an earlier/equal candidate is a complete-state no-op; a notice at
   or after the old deadline starts a new interval. Arbitrary ordering retains
   the same maximum deadline but is not claimed to produce an identical lower
   bound (data-model I6, I7).
6. No record ever carries a computed deadline or any window duration.
7. A record never carries chat message text or a chatter identity.
8. Version 1 consumers ignore unknown `schema_version` values; a version 2 must
   therefore only ever be introduced alongside a consumer that accepts both.
9. `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS` is fixed at 30. A field-valid record
   at the bound is accepted with age zero and a skew diagnostic; one
   millisecond beyond is rejected as malformed fields before any delivery
   observation or state access.

---

## 6. Compatibility and evolution

- **Adding an optional field**: keep `schema_version: 1`. Version-1 consumers
  ignore unknown keys; the field must not be required for correct behaviour.
- **Changing the meaning or type of an existing field, or adding a required
  field**: bump to `schema_version: 2`, deploy the consumer that accepts 1 and
  2 **first**, then the producer. With 1-hour retention, version 1 records
  disappear an hour after the producer switches.
- **Removing a field**: only after no consumer reads it, and only with a
  version bump if it was required.
- **Changing the partition count**: it must continue to equal
  `FLINK_PARALLELISM`. Kafka can only grow a partition count in place, and the
  topic must be recreated to shrink it — the same constraint `chat-messages`
  documents in `docker-compose.yml`.

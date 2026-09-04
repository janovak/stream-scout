# Phase 1 Data Model: Suppress Gift and Raid Chat Bursts

**Feature**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04
**Companions**: [plan.md](./plan.md), [research.md](./research.md),
[contracts/suppression-events.schema.md](./contracts/suppression-events.schema.md)

Three stores hold this feature's state, and each owns a disjoint part of it:

| Store | Owns | Lifetime |
|---|---|---|
| `EventSubPoolTransport` in-memory indexes | Subscription slots, channel coverage, connection occupancy | Process lifetime; rebuilt from Twitch by re-adoption |
| Kafka `suppression-events` | The immutable notice record | Topic retention (short) |
| Flink keyed state (`AnomalyDetector`) | `suppress_until` per broadcaster | State TTL, or until the key stops being written |

Nothing new is persisted in Postgres or Redis, and there is no migration.

---

## 1. Producer-side entities

### 1.1 `CoverageType`

A closed enumeration of the two subscription types a monitored channel needs.

| Value | Twitch subscription type | Purpose |
|---|---|---|
| `chat` | `channel.chat.message` | Chat-message coverage; feeds `chat-messages` |
| `notification` | `channel.chat.notification` | Chat-notification coverage; feeds `suppression-events` |

The set is fixed by this feature. A third type would change every capacity
number in §4 and is out of scope.

### 1.2 `SubscriptionSlot`

One live Twitch subscription. Today's `_Slot` keyed by broadcaster becomes one
record per **(broadcaster, coverage type)**.

| Field | Type | Notes |
|---|---|---|
| `broadcaster_id` | `int` | Channel identity |
| `coverage_type` | `CoverageType` | Which half of the pair this is |
| `connection_id` | `int` | The pool connection holding it |
| `subscription_id` | `str` | Twitch's id at create time; a reconnect rotates it |
| `session_id` | `str \| None` | The websocket session the create was **issued on**, read before the call — the existing staleness rule, unchanged |

Two slots for the same channel are **independent records**. Either may exist
without the other, and neither may be inferred from the other's presence.

### 1.3 `ChannelCoverage`

A derived, per-channel view over the pair. It is the only thing that answers
"is this channel fully covered".

| Field | Type | Notes |
|---|---|---|
| `broadcaster_id` | `int` | |
| `chat_slot` | `SubscriptionSlot \| None` | |
| `notification_slot` | `SubscriptionSlot \| None` | |
| `auxiliary_refused_until_ms` | `int \| None` | Set when Twitch refuses the notification type while chat is live: `now + AUXILIARY_REFUSAL_RETRY_SECONDS` (3600 s). `None` means no hold-off (research D2, autonomous decision 17) |

`auxiliary_refused` is derived, not stored: it is true only while
`auxiliary_refused_until_ms` is set **and** in the future.

Derived state:

| State | Condition | Consequence |
|---|---|---|
| `complete` | both slots present | Channel is in the pool's actual set; suppression is available |
| `chat_only` | chat present, notification absent, no active hold-off | Channel is **not** in the actual set, so the reconciler re-creates the missing half; chat is unaffected; detection fails open |
| `notification_only` | notification present, chat absent | Same: not actual, chat half re-created; the channel produces no chat and therefore no clip decisions at all |
| `absent` | neither | Not covered; ordinary create path |
| `degraded_chat_only` | `chat_only` **and** the hold-off is still active | Reported as covered **for the duration of the hold-off only**, so the reconciler does not hot-loop on a refusal it cannot fix; visibly degraded; suppression unavailable for this channel |

`degraded_chat_only` is the only state in which the pool reports a channel as
actual without both slots, and it exists solely to stop a per-channel Twitch
refusal on an auxiliary signal from evicting that channel's chat for the
seven-day refusal window (research §3, D2).

It is **temporary by construction**, which is what keeps it consistent with
FR-001 rather than a standing exception to it:

| Event | Effect on the hold-off |
|---|---|
| `auxiliary_refused_until_ms` passes | Cleared. The channel reverts to `chat_only`, leaves the actual set, and ordinary create repair resumes on the next pass |
| The channel's connection reconnects, or is retired | Cleared immediately — the refusal may have been session-specific, and the reconnect is a free opportunity to retest it |
| The notification subscription is created or adopted successfully | Cleared; the channel becomes `complete` |
| Another refusal arrives after expiry | A new hold-off starts; the channel is degraded again for one more bounded period, never permanently |

### 1.4 `Connection` (existing, semantics clarified)

| Field | Counts | Bound |
|---|---|---|
| `subscription_ids` | **subscriptions** | ≤ 300 enabled per connection |
| `reserved` | **subscriptions** in flight | included in `load` |
| `load` | `len(subscription_ids) + reserved` | routing compares this against the cap |
| `full_at` | occupancy Twitch refused at | unchanged semantics |

**Occupancy is a subscription count. Capacity policy is a channel count.**
Those two units must never be mixed:

- `eventsub_connection_occupancy` — subscriptions per connection, ≤ 300.
- `eventsub_subscription_count` — total subscriptions held, ≤ 800 steady state.
- `eventsub_channel_coverage{state}` — **channels** by coverage state; the
  `complete` series is what is compared against `ZCARD chat:desired` and against
  the 400-channel ceiling (FR-015).

---

## 2. Transport entity: `SuppressionEvent`

The full field list, types, required/optional split, and both producer and
consumer validation rules live in
[contracts/suppression-events.schema.md](./contracts/suppression-events.schema.md).
Summarised here only for the state model:

- Immutable, self-contained, versioned; key = `broadcaster_id`.
- Carries the **notice**, never a computed deadline — window policy is applied
  by the consumer (research D7).
- Produced only for `community_sub_gift`, `sub_gift`, `raid`.
- Never produced with a guessed identity or occurrence time (FR-017).
- Key/payload agreement is a **producer** invariant. The job's Kafka sources
  deserialize values only, so the consumer never observes the record key: it
  keys, routes, and validates on the payload `broadcaster_id` (research D15).

---

## 3. Consumer-side entity: `SuppressionState`

One `ValueState` per broadcaster key inside `AnomalyDetector`, JSON-encoded in a
`Types.STRING()` state exactly as `HoldState` already is, and covered by the
same TTL configuration (`retained_seconds × 4`, `NeverReturnExpired`).

| Field | Type | Meaning |
|---|---|---|
| `suppress_until_ms` | `int` | Epoch ms, Twitch clock. Emission is gated for peaks strictly before this instant |
| `notice_type` | `str` | The category that last **moved** the deadline; diagnostic, for the required structured log |
| `notice_at_ms` | `int` | `occurred_at_ms` of that same notice; diagnostic and the Twitch-clock input to the receipt-only delivery-age calculation — never a continuously refreshed gauge (research D13, I19) |

Absent state (never written, expired, or read back as absent under
`NeverReturnExpired`) means **not suppressed**. There is no third value and no
"unknown" — that is the structural form of the fail-open decision (FR-011).

`consumer_receipt_ms` is not stored state. `process_element2` captures it from
an injected clock in tests and the current consumer clock at runtime, then
computes `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`.
That value alone is compared with `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` and
observed as seconds in `suppression_delivery_age_seconds`. A negative raw age
is clamped to zero and structured-logged as clock skew. Optional
`received_at_ms` may decompose the latency diagnostically, but is never a
classification input and no state transition depends on it.

### 3.1 Transition: applying a notice

Pure function in `spike_detector.py`, callable without Flink:

```text
apply_notice(state, notice_type, occurred_at_ms, config) -> SuppressionState | unchanged

  window        = config.window_for(notice_type)          # gift 120 s, raid 180 s
  candidate_ms  = occurred_at_ms + window * 1000
  current_ms    = state.suppress_until_ms if state else 0

  if candidate_ms <= current_ms:  return state            # unchanged: never moves backward
  return SuppressionState(candidate_ms, notice_type, occurred_at_ms)
```

Consequences that fall out of `max()` alone, with no extra bookkeeping:

| Case | Result | Requirement |
|---|---|---|
| Notice extends beyond the deadline | Deadline moves out | FR-006, US4-1 |
| Notice would produce an earlier or equal deadline | No change, no second window | FR-006, FR-010, US4-2 |
| The same notice delivered twice | Second application is a no-op | FR-010, edge case "duplicated" |
| Notices arrive out of order | Same final deadline either way | FR-010 |
| Gift and raid overlap with different windows | The later candidate wins | US4-3, SC-005 |
| Notice arrives exactly at the current deadline | `candidate > current`, so it extends | Edge case "exactly at the deadline" |
| Excluded notice type reaches the consumer anyway | No `window_for` entry → record ignored | FR-005 |

### 3.2 Transition: gating an emission

```text
is_suppressed(state, peak_second) -> bool
  return state is not None and peak_second * 1000 < state.suppress_until_ms
```

Applied **only** at the end of `on_timer`, and only when `decision.emit` is not
`None`:

| `decision.emit` | `is_suppressed(peak)` | Effect |
|---|---|---|
| `None` | — | Nothing; no signal |
| present | `False` | `anomalies_detected_total` + `ANOMALY DETECTED` log + yield → `ClipCreator` (unchanged behaviour) |
| present | `True` | `anomalies_detected_total` + `clips_suppressed_total` + structured suppression log + **no yield** |

The compared instant is the **peak** second (`spike.detected_at_seconds`), not
the report second, so a burst that peaks inside the window cannot escape by
being reported after `hold_cap_seconds` (research D5).

### 3.3 What the gate must not touch

Every one of these happens identically whether the decision is suppressed or
not, because the gate is strictly downstream of them:

| State | Written by | Behaviour under suppression |
|---|---|---|
| `message_counts` buckets | `process_element1` | Every message still counted |
| Expired buckets | `on_timer` loop over `decision.expired_buckets` | Same removals |
| `hold` | `decision.hold != hold` write | Same open/extend/close trajectory |
| Chain timer | `next_chain_timer(...)` | Same registration |
| `last_fire_second` | `on_timer` on any `decision.emit` | **Still updated** (research D6) |

`last_fire_second` is the one that has to be argued rather than assumed:
updating it keeps the gated run byte-identical in state to the ungated run,
which is what SC-004 asserts. The cost is that a suppressed decision starts the
30 s cooldown as if a clip had been created — a period that lies inside a
120–180 s suppression window anyway, where no clip could have been emitted.

---

## 4. Capacity model

| Quantity | Unit | Value | Enforced by |
|---|---|---:|---|
| Connections | connections | ≤ 3 | `_grow()` refusal (`MAX_CONNECTIONS`) |
| Enabled subscriptions per connection | subscriptions | ≤ 300 | `route()` against `cap`, `full_at` |
| Total subscriptions | subscriptions | ≤ 900 | `MAX_SUBSCRIPTIONS` |
| Monitored channels | channels | ≤ 400 | `LEAVE_THRESHOLD` = 400 |
| Steady-state subscriptions at the ceiling | subscriptions | 800 | 400 × 2 |
| Reserved headroom | subscriptions | ≥ 100 | 900 − 800 |
| Channels per connection when a pair is co-located | channels | ≤ 150 | 300 / 2 |

Admission of a 401st channel is refused at the **intent** layer, not the
transport: `compute_desired_set` never returns more than `LEAVE_THRESHOLD`
entries, so the monitored set cannot exceed 400 and the transport is never
asked for a 801st subscription. The transport's own refusals (`route()`
returning `None`, `_grow()` at the connection limit) remain the second line of
defence and stay loud.

Placement rule for a pair:

1. Prefer the connection that already holds either slot for this channel, when
   it has room for the slot being added.
2. Otherwise take rendezvous order, choosing the first connection that can hold
   the number of slots being created (two for a new channel, one for a repair).
3. If no connection can, grow — subject to the 3-connection limit — and
   otherwise refuse, exactly as today.

Splitting a pair across two connections is legal and modelled (research R2): a
socket death then leaves the channel in `chat_only` or `notification_only`,
both of which are already convergent states.

---

## 5. Lifecycle transitions

### 5.1 Channel joins the monitored set

`absent` → (create both slots) → `complete`. If only one create succeeds, the
channel rests in a partial state, is not counted as actual, and the next pass
creates the missing half only. A 409 on either type adopts the existing
subscription of **that type** rather than creating a duplicate (FR-002).

### 5.2 Channel leaves the monitored set

`complete` → (delete both slots) → `absent`. Both subscriptions are deleted;
"already gone" is success. The channel then occupies no subscription slot and
no monitored slot. Its Flink `SuppressionState` is not deleted explicitly —
it stops being written, its bucket state drains, and the TTL removes it. A
still-future deadline for a departed channel is harmless because no chat
arrives to be gated.

### 5.3 Channel re-enters the monitored set

Fresh slots are created. On the detector side, an expired deadline reads back
as absent under `NeverReturnExpired`, so a previous window cannot suppress new
activity (spec edge case "re-enters the monitored set"). A deadline that has
genuinely not expired yet still applies — that is the same window, not a stale
one.

### 5.4 Revocation

Twitch revokes one subscription. The pool forgets **that type's** slot only,
reports one lost subscription, and the channel drops to a partial state. The
reconciler re-enumerates and re-creates the missing half. Chat coverage is
untouched when the revoked subscription was the notification one.

### 5.4.1 Auxiliary refusal and its expiry

Twitch refuses the notification create while chat is live:
`chat_only` → (`auxiliary_refused_until_ms = now + 3600 s`) →
`degraded_chat_only`. No channel refusal is written to Postgres and chat is
never evicted (research D2).

The state leaves on its own:

- `degraded_chat_only` → `chat_only` when the hold-off expires, or as soon as
  the channel's connection reconnects or is retired;
- `chat_only` → `complete` on the next successful create or 409 adoption, which
  also clears any residual hold-off;
- a fresh refusal after expiry starts one new bounded hold-off, so a channel
  that Twitch permanently refuses cycles between the two states roughly hourly
  and stays visible as degraded, rather than disappearing from repair.

### 5.5 Socket death and reconnect

`_retire()` clears every slot on the dead connection — both types for
co-located channels. Affected channels become `absent` (or partial, if the pair
was split) and are re-created on a surviving or new connection. A library
reconnect rotates subscription ids; the session stamp on each slot is what
detects that, per type, independently.

### 5.6 Flink job restart

Checkpointing is off and the suppression source starts at `latest()`. All
`SuppressionState` is therefore empty after a restart, and every channel fails
open until its next notice. This is intended: the alternative — replaying old
notices from `earliest()` — would pin the operator watermark in the past and
stall detection for every channel (research D4).

---

## 6. Invariants

Each is stated so it can be asserted by a test rather than reasoned about.

| ID | Invariant |
|---|---|
| **I1** | A channel is reported as actual only when both coverage types are present and `enabled` on a live session, or when it is `degraded_chat_only` **and** its bounded auxiliary-refusal hold-off has not yet expired |
| **I2** | A repair creates only the missing coverage type; the surviving type is never re-created or duplicated (FR-002) |
| **I3** | The two slots of a channel are tracked independently; deleting, revoking, or losing one never implicitly removes the other from the indexes |
| **I4** | Connection occupancy counts subscriptions and never exceeds 300; the channel-coverage gauge counts channels and never exceeds 400 (FR-015) |
| **I5** | Steady-state subscriptions ≤ 800, leaving ≥ 100 of the 900 slots free (FR-014, NFR-001) |
| **I6** | `suppress_until_ms` is non-decreasing for a given key while the state exists; no input can move it backward (FR-006, FR-010) |
| **I7** | Applying the same notice more than once, or applying notices in any order, yields the same `suppress_until_ms` (FR-010) |
| **I8** | A notice whose category is outside `{community_sub_gift, sub_gift, raid}` never creates or extends state, at either the producer or the consumer (FR-005) |
| **I9** | A notice with an untrustworthy channel identity or occurrence time produces no event, no deadline, and an incremented malformed counter (FR-017) |
| **I10** | Absent suppression state is indistinguishable from "not suppressed" and never blocks emission (FR-011) |
| **I11** | The gate changes only whether the anomaly is yielded; every keyed state write in `on_timer` is identical to the ungated run (FR-008, SC-004) |
| **I12** | An emitted clip is never retracted; a suppression event that arrives after emission affects only later decisions (FR-018) |
| **I13** | Suppression state is per broadcaster key; no notice can alter another channel's state or eligibility (NFR-002) |
| **I14** | Every suppressed would-have-clipped decision emits exactly one metric increment and one structured log, attributable to the channel and distinct from coverage, delivery, malformed, and capacity signals (FR-012, NFR-006) |
| **I15** | The suppression input never becomes the binding watermark minimum in steady state: its idleness timeout is strictly less than the chat stream's, and its partition count equals `FLINK_PARALLELISM` (research R3) |
| **I16** | When a long-idle suppression subtask becomes active again with a single isolated notice, it may hold the operator's two-input watermark for at most `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before going idle and releasing it. Sustained notice traffic advances the watermark normally and never reaches this bound (research §4.1.1, R10) |
| **I17** | `degraded_chat_only` is always time-bounded: a hold-off never exceeds `AUXILIARY_REFUSAL_RETRY_SECONDS`, is cleared early by reconnect, retirement, successful create, or adoption, and never prevents a later repair attempt (FR-001, NFR-003, SC-001) |
| **I18** | Key/payload agreement is asserted at the producer, where the key exists. The consumer deserializes values only, never observes the key, and derives routing, keying, and state from the payload `broadcaster_id`; a malformed payload is rejected and counted (research D15, contract §5.1) |
| **I19** | Suppression delivery health has exactly three classifications. For a received record, `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)` from the injected/current consumer clock is the sole threshold input and is observed in `suppression_delivery_age_seconds`; negative raw age is clamped and structured-logged as skew, while optional `received_at_ms` remains diagnostic-only. A window with no record is idle/unknown and reports no value (NFR-005, research D13) |
| **I20** | `desired_set_churn_total` increments by entered plus departed channels per poll, with no per-channel label growth, so the NFR-007 bound of ≤ 8 changes per poll averaged over 24 deployed hours is directly computable from it (SC-011, research D14) |

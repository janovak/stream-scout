# Phase 0 Research: Suppress Gift and Raid Chat Bursts

**Feature**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04 (capacity amendment 2026-09-05)
**Input**: [spec.md](./spec.md), [autonomous-decisions.md](./autonomous-decisions.md)

This file records what was verified against primary sources, what was read out
of the current checkout, and the decisions (D) and risks (R) the plan depends
on. Nothing here was measured on live infrastructure: this workstation is
code-and-unit-test only. Every quantity that needs a live system is listed in
§9 as deferred evidence, not as a finding.

---

## 1. Twitch EventSub capacity, mixed types, and authorization

### 1.1 What the primary documentation says

Source: <https://dev.twitch.tv/docs/eventsub/handling-websocket-events/>,
section "Subscription limits", checked 2026-09-04. Limits apply **per user
token (client ID and user ID tuple)**:

- "You can create a maximum of 3 WebSockets connections with enabled
  subscriptions. Reconnecting using a reconnection URL … doesn't add to your
  WebSocket count."
- "Each WebSocket connection may create a maximum of **300 enabled
  subscriptions** (disabled subscriptions don't count against the limit)."
- "The `max_total_cost` is 10 across all subscriptions."

Two further statements from the same page matter to this feature:

- A lost connection has **no event replay**: "There is no replay of events that
  are lost during the time it takes to establish a new connection and
  resubscribe." A suppression notice that occurs while a socket is down is
  simply never delivered — this is a fail-open case, not a recoverable one.
- "If you disconnect from a WebSocket session, all subscriptions associated
  with that session are automatically disabled." This is why the pool's
  session-id stamping (`_Slot.session_id`, `_slot_is_current`) exists, and it
  applies identically to the second subscription type.

**Mixed subscription types on one session**: the 300 limit is stated per
connection over *enabled subscriptions*, with no per-type partition and no
statement that a session is restricted to one subscription type. `channel.chat.message`
and `channel.chat.notification` are ordinary subscription types with the same
websocket transport, so both may live on one session and both consume the same
300-subscription budget. This is the documented basis for the two-slot model;
it is confirmed operationally only by the deployed check in §8 (E1).

### 1.2 Cost budget

`max_total_cost` is 10. Production already runs ~800 `channel.chat.message`
subscriptions on this token, which is only possible because those subscriptions
cost 0 — Twitch charges 0 for a subscription whose condition user has
authorized the client for the required scope. `channel.chat.notification` uses
the same authorization model and the same `user:read:chat` scope on the same
already-authorized user, so it is expected to cost 0 as well. **This is the one
capacity assumption that cannot be proven from documentation alone**; E1 in §8
checks `total_cost` on the deployed system before the ramp change.

### 1.3 Authorization (FR-016 — no scope change)

Source: pyTwitchAPI 4.5.0 API reference for
`EventSubWebsocket.listen_channel_chat_notification`
(<https://pytwitchapi.dev/en/stable/modules/twitchAPI.eventsub.websocket.html>,
checked 2026-09-04):

> "A notification for when an event that appears in chat has occurred. Requires
> `USER_READ_CHAT` scope from chatting user. If app access token used, then
> additionally requires `USER_BOT` … and either `CHANNEL_BOT` … or moderator
> status."

The service uses a **user** access token (spec 004; `docker-compose.yml`
`stream-monitoring` comments; `seed_twitch_tokens.py` `REQUIRED_SCOPES`), so the
app-token clause does not apply. `user:read:chat` is already seeded and is the
same scope `listen_channel_chat_message` requires. **FR-016 holds: no scope
change, no token reseeding.**

### 1.4 Capacity arithmetic for this feature

| Quantity | Value | Source |
|---|---:|---|
| Connections allowed | 3 | Twitch docs, §1.1 |
| Enabled subscriptions per connection | 300 | Twitch docs, §1.1 |
| Total subscription ceiling | 900 | `MAX_SUBSCRIPTIONS`, `eventsub_pool.py:101` |
| Subscriptions per monitored channel after 007 | 2 | FR-001 |
| Channels per connection when a pair is co-located | 150 | 300 / 2 |
| Monitored-channel **entry** threshold | 400 | FR-013 (decision 27) |
| Monitored-channel **retention and maximum** threshold | 450 | FR-013 (decision 27) |
| Steady-state subscriptions at the maximum | 900 | 450 × 2 |
| Guaranteed free slots | 0 | 900 − 900 (FR-014) |

The amended model spends the account exactly, which is the **same shape as the
pre-007 800/900 ramp**: a deep entry gate, a retention band above it, and a
ceiling equal to the account limit. At one subscription per channel that ramp
authorised up to 900 channels against 900 slots; at two subscriptions per
channel the halved 400/450 pair authorises up to 450 channels against the same
900 slots.

Two paths that a reserve would nominally protect do not normally consume new
slots at all:

- **Reconnect.** Ending a session automatically disables its subscriptions, and
  disabled subscriptions do not count against the 300-per-connection limit;
  reconnect URLs do not add to the websocket count either (§1.1). Re-creating
  on the new session therefore reuses budget the old session released.
- **Adoption.** A 409 conflict means the subscription already exists and is
  already counted; adoption records its id and creates nothing.

What zero slack does change is placement and failure legibility. Three
connections at 300 hold 450 co-located pairs only under perfect packing, so a
connection left at an odd occupancy strands a single slot that no whole pair can
use. Pair placement must therefore be able to take one slot on each of two
connections, atomically (D14, R5). The remaining exposures — enabled
subscriptions this feature did not create, failed deletes, and a connection
that reports itself full below the cap — are risks R15-R17 rather than
arithmetic, and are why exact capacity is reached only after E2a and the
account sweep.

The former 100-subscription headroom is **not** global spare capacity in the
amended model; there is none. Rendezvous routing (`_score`,
`eventsub_pool.py:164`) still does not balance perfectly, so `route()`'s
fall-through to the next connection in rendezvous order — and now its
split-pair fallback — is what keeps imbalance from becoming a failure (see
D1/R2/D14).

---

## 2. pyTwitchAPI 4.5.0 notification listener and payload

Verified against the pinned version's own source
(<https://raw.githubusercontent.com/Teekeks/pyTwitchAPI/v4.5.0/twitchAPI/object/eventsub.py>)
and the 4.5.0 API reference, both checked 2026-09-04.

### 2.1 Listener

```text
async listen_channel_chat_notification(broadcaster_user_id, user_id, callback) -> str
```

Identical shape to `listen_channel_chat_message(broadcaster_user_id, user_id, callback)`,
which `eventsub_pool.create()` already calls (`eventsub_pool.py:497`). Same
returned subscription id, same raised exceptions —
`EventSubSubscriptionConflict` (409), `EventSubSubscriptionError`,
`EventSubSubscriptionTimeout`, `TwitchBackendException` — so the existing
`except` ladder and `_classify()` apply unchanged to the second type.

### 2.2 Callback payload

```text
ChannelChatNotificationEvent
├── subscription : Subscription
├── metadata     : MessageMetadata      # message_id, message_type,
│                                       # message_timestamp (datetime),
│                                       # subscription_type, subscription_version
└── event        : ChannelChatNotificationData
```

`ChannelChatNotificationData` fields this feature uses:

| Field | Type | Use |
|---|---|---|
| `broadcaster_user_id` | `str` | channel identity → Kafka key and `broadcaster_id` |
| `notice_type` | `str` | trigger selection (FR-005) |
| `message_id` | `str` | Twitch's own UUID → duplicate detection |
| `community_sub_gift` | `Optional[CommunitySubGiftNoticeMetadata]` | present only when `notice_type == community_sub_gift` |
| `sub_gift` | `Optional[SubGiftNoticeMetadata]` | present only when `notice_type == sub_gift` |
| `raid` | `Optional[RaidNoticeMetadata]` | present only when `notice_type == raid`; carries `viewer_count` |

The documented `notice_type` values in 4.5.0 are exactly: `sub`, `resub`,
`sub_gift`, `community_sub_gift`, `gift_paid_upgrade`, `prime_paid_upgrade`,
`raid`, `unraid`, `pay_it_forward`, `announcement`, `bits_badge_tier`,
`charity_donation`. FR-005's trigger set (`community_sub_gift`, `sub_gift`,
`raid`) and its explicit exclusions (`unraid`, `sub`, `resub`) are all real
members of that enumeration, and the remaining six are covered by FR-005's
"every other notification category".

Two consequences the mapper must respect:

1. **`TwitchObject.__init__` omits absent fields entirely** — this is already
   documented in `map_chat_message` (`eventsub_pool.py:236`, the `getattr`
   comment). An unknown or newly added `notice_type` must therefore be read
   with `getattr(data, "notice_type", None)`, and the notice-specific
   sub-object must never be assumed present.
2. **`metadata.message_timestamp` is already a tz-aware `datetime`** by the time
   the callback runs, and `to_epoch_ms()` (`eventsub_pool.py:177`) already
   converts exactly that, with the same truncation the chat path uses. The
   suppression event reuses `to_epoch_ms` unchanged, so `occurred_at_ms` and
   chat `sent_at` are on the same Twitch clock — which is what makes comparing
   them in the detector meaningful at all (see D5/R4).

### 2.3 What is *not* used, and why

- `viewer_count` on `raid`: decision 1 fixed the raid window at a constant, so
  audience size cannot affect duration. It is carried in the contract as an
  **optional diagnostic field only** and no consumer logic may read it.
- `chatter_user_id` / `chatter_is_anonymous`: the gifter's identity has no
  bearing on channel-scoped suppression.
- `system_message` / `message`: free text; carrying it would put user content
  into an operational topic for no requirement.

---

## 3. The EventSub pool as a two-slot-per-channel state model

Read in full: `services/stream-monitoring/eventsub_pool.py` (1,500 lines).
Every path below currently assumes **one** `channel.chat.message` subscription
per channel. This is the highest-risk surface in the feature and the reason
pool work precedes the ramp change.

| Path | Today | What two slots require |
|---|---|---|
| Type constant (`:105`) | single `CHAT_MESSAGE_SUBSCRIPTION_TYPE` | a type set; every filter becomes type-aware |
| `_Slot` (`:316`) | `broadcaster_id → connection, subscription_id, session_id` | one slot **per (channel, type)**, plus a channel-level coverage view over the pair |
| `_slots` index (`:368`) | `Dict[int, _Slot]` | `Dict[(int, str), _Slot]` plus `Dict[int, _Coverage]` |
| `route()` (`:730`) | first connection with `load < cap` | must be able to place **two** subscriptions, must prefer the connection already holding the channel's other type, and — at exact capacity — must be able to place one slot on each of two connections when no single connection has two free but the pool has at least two (D14) |
| `_reserve()` (`:748`) | `reserved += 1` | reserve the number of subscriptions actually about to be created, in one critical section; a split pair reserves **both** halves atomically or neither |
| `create()` (`:441`) | one listen call, one slot | create only the **missing** types (FR-002: never duplicate the type that exists) |
| 409 `_adopt_conflict()` (`:1074`) | lists one type, matches by broadcaster | must match **type + broadcaster**, and be reachable for either type independently |
| `list()` (`:640`) | one Helix walk filtered to chat | two walks (one per type), joined per channel; a channel is "actual" only when **both** types are enabled on a live session |
| `delete()` (`:596`) | resolve one live id, delete it | resolve and delete **both** types for the channel; partial failure leaves the surviving slot for the next pass |
| `_connection_holds()` (`:1264`) | matches `condition.broadcaster_user_id` only | must also match `sub_type`, or a chat-only channel reads as "current" for the notification type |
| `_live_subscription_ids()` (`:1320`) | matches broadcaster only | same type filter, or a delete removes the wrong type's id |
| `_forget_revoked()` (`:944`) | drops the channel's single slot | drops only the revoked **type**; the surviving type stays, and the channel becomes partially covered |
| `_forget_unrecognised()` (`:1288`) | resolves channel from the library registry | must resolve **type** as well |
| `_retire()` (`:1443`) | clears every slot on the connection | unchanged in shape, but now clears both types for co-located channels |
| `occupancy()` (`:717`) | subscriptions per connection | unchanged (subscriptions), but a **separate** channel-coverage signal is required by FR-015 |

Findings that shape the design:

- **The reconciler is not on the roadmap's implementation-surface list, and it
  does not need to be.** `reconciler.py` diffs a channel-keyed desired set
  against a channel-keyed `_actual` (`reconciler.py:718`), applies per-channel
  refusal caching (`_drop_refused`, `:1097`), and publishes a channel-level
  count (`_publish_subscription_count`, `:1152`). Making the reconciler
  subscription-keyed would touch all of that. Keeping the pair inside the pool
  keeps the diff, the ordering, the refusal cache, the batching and the retry
  budget exactly as they are (D1).
- **`_adopt` only adopts `status == "enabled"`** (`ADOPTABLE_STATUSES`,
  `reconciler.py:69`). Joining the two type walks in `list()` therefore yields a
  channel only when both types are enabled, so partial coverage is repaired
  through the ordinary `to_create` path with no new reconciler branch.
- **A partial view must never delete.** `_adopt` already holds drops back until
  one clean enumeration succeeds (`reconciler.py:723`). With two Helix walks,
  "clean" must mean **both** walks completed; a failure in either must mark the
  enumeration incomplete (R1).
- **Refusal is per channel, not per type** (`streamers.eventsub_refused_at`).
  A 403 on the auxiliary type must therefore not be allowed to reach the
  reconciler as `SubscriptionRefusedError` when chat is live, or a
  notification-only refusal would suppress the channel's chat for
  `REFUSAL_RECHECK_DAYS` = 7 days — data loss caused by an auxiliary signal,
  which FR-011 and the constitution both forbid (D2). The pool-local
  alternative must itself be **time-bounded**: a permanent pool-local refusal
  would trade one coverage hole for another and would quietly contradict
  FR-001, so the hold-off lasts `AUXILIARY_REFUSAL_RETRY_SECONDS` = 3600 s, is
  cleared early by a websocket reconnect or connection retirement, and is
  cleared outright by a successful create or adoption.

---

## 4. Flink: two-input keyed processing, watermarks, idleness

Runtime is Flink **1.18** (`services/flink-job/Dockerfile:1`,
`requirements.txt:1` `apache-flink==1.18.0`), `FLINK_PARALLELISM=4`,
checkpointing off.

### 4.1 Two-input watermark semantics (primary source)

<https://nightlies.apache.org/flink/flink-docs-release-1.18/docs/dev/datastream/event-time/generating_watermarks/>,
"How Operators Process Watermarks", checked 2026-09-04:

> "The same rule applies to `TwoInputStreamOperator`. However, in this case the
> current watermark of the operator is defined as **the minimum of both of its
> inputs**."

This is the central hazard of the whole feature. The chat detector fires its
per-second timers off the operator watermark (`AnomalyDetector.on_timer`,
`clip_detector_job.py:717`). Connecting a second, near-silent stream to it puts
that stream's watermark into the minimum. If the suppression stream's watermark
does not advance, **every broadcaster's detection stops**, which is a far worse
outcome than the false-positive clips this feature removes.

Three ways that could happen, and how the design closes each:

| Failure mode | Why it happens | Closure |
|---|---|---|
| No watermarks at all on the suppression source | `WatermarkStrategy.no_watermarks()` emits none, so the min never advances | Forbidden as an end state. The suppression stream gets a real bounded-out-of-orderness strategy (D4). Under §4.1.2 that strategy is attached one operator downstream via `assign_timestamps_and_watermarks`, so the `no_watermarks()` placeholder handed to `from_source` is never the stream's effective strategy |
| A quiet partition holds the min | Idle split, documented in "Dealing With Idle Sources": "the watermark will be held back, because it is computed as the minimum over all the different parallel watermarks" | `with_idleness(SUPPRESSION_IDLENESS_SECONDS)`, set **below** the chat stream's `WATERMARK_IDLENESS_SECONDS = 10` so the suppression input is never the last to be released |
| Old offsets replayed at start-up | `earliest()` would feed hours-old `occurred_at_ms`, pinning the operator watermark in the past until the backlog drains | `KafkaOffsetsInitializer.latest()`, matching the chat source (`clip_detector_job.py:1049`) (D4) |

A fourth, subtler case: a source **subtask with no split at all**. The existing
`chat-messages` topic was deliberately re-provisioned to 4 partitions to match
`FLINK_PARALLELISM` (`docker-compose.yml:90-109`). The suppression topic is
created with the same 4 partitions for the same reason, so every subtask owns
exactly one split and the "no splits assigned" idleness behaviour is never
relied upon. This is a design constraint, not an observation; E3 in §8 verifies
it on the deployed system.

Note that the *locus* of idleness generation moves under §4.1.2: it is produced
by the post-source assignment operator per parallel subtask rather than inside
the Kafka source per split. §4.1.2 re-derives why the constraint above still
carries the same guarantee, and why it must be revalidated if partitions and
parallelism ever stop matching.

### 4.1.1 The residual case: idle → active re-entry on a sparse source

Idleness removes a quiet subtask from the watermark minimum; it does not stop
that subtask from re-entering the minimum the moment it produces a record
again. On a source as sparse as this one, that re-entry is the normal case
rather than an exotic one: hours of silence, then a single isolated notice.

When the isolated notice arrives, its subtask becomes active with a watermark of
`occurred_at_ms − WATERMARK_OUT_OF_ORDERNESS_SECONDS`, and it stays active — and
therefore stays in the minimum — until the idleness timeout elapses again with
no further record. The operator's two-input watermark can therefore be held
back, briefly, by the very signal that is supposed to be free. The accepted,
conservative upper bound on that hold is

```text
SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS
```

after which the subtask is marked idle again and released. Sustained notice
traffic never hits this bound at all: a stream that keeps producing advances its
own watermark normally, exactly like the chat input. The bound matters only for
the one-record-then-silence shape.

This is accepted rather than engineered away. Removing it would need either
watermarks on the suppression side that are not derived from event time, or a
heartbeat/synthetic-record protocol on the topic — both explicitly out of scope,
and the first is the failure mode D4 exists to prevent. It is bounded, it is
short, it delays per-second evaluation rather than stopping it, and the delay is
smaller than the shortest suppression window by more than an order of magnitude.
It is recorded as R10, asserted offline in the replay harness's simplified model
(T050), and measured on the deployed system as part of E3.

### 4.1.2 Where the Python timestamp assigner actually runs (PyFlink 1.18)

Checked against the PyFlink 1.18 source, 2026-09-05:

`StreamExecutionEnvironment.from_source(source, watermark_strategy, name)`
(`pyflink/datastream/stream_execution_environment.py`) forwards only
`watermark_strategy._j_watermark_strategy` into the Java `fromSource` call. A
Python `TimestampAssigner` supplied through `.with_timestamp_assigner(...)` is
retained on the Python-side `WatermarkStrategy` wrapper and is never installed
into that Java strategy. The assigner becomes an executable operator **only**
through `DataStream.assign_timestamps_and_watermarks(strategy)`
(`pyflink/datastream/data_stream.py`), which wraps the stream in a Python
timestamp-assigner and watermark-generator operator.

Passing a Python assigner straight to `from_source` is therefore **silently
ignored** — no exception, no log line. The Java strategy still emits
bounded-out-of-orderness watermarks and still honours idleness, so the job
looks healthy; it simply derives event time from the **Kafka record
timestamp** instead of from the payload.

Two consequences for this checkout, both real today:

1. `SentAtTimestampAssigner` on the chat source (`clip_detector_job.py:1496`,
   `:1502`) does not run. `process_element1`'s `ctx.timestamp()` is broker
   ingestion time, not `sent_at`, contradicting its own comment and the
   Feature 004 contract.
2. `SuppressionTimestampAssigner` (`clip_detector_job.py:782`, `:1517`) does
   not run either, so decision 24's +30 s source-side trust fallback is
   unreachable dead code and `occurred_at_ms` never reaches event time.

**The correction (decision 25).** Each Kafka source is built into the
environment with `WatermarkStrategy.no_watermarks()`, and the real strategy is
attached immediately to the resulting `DataStream`:

```text
stream = env.from_source(kafka_source, WatermarkStrategy.no_watermarks(), name)
stream = stream.assign_timestamps_and_watermarks(real_strategy)
```

`real_strategy` contains `for_bounded_out_of_orderness(...)`,
`with_idleness(...)`, and the Python assigner — `SentAtTimestampAssigner` for
chat, `SuppressionTimestampAssigner` for suppression — in a binding order that
is itself an invariant:

```text
WatermarkStrategy.for_bounded_out_of_orderness(...)
  .with_idleness(...)
  .with_timestamp_assigner(...)  # MUST be last
```

In PyFlink 1.18, `with_idleness()` returns a fresh `WatermarkStrategy` wrapper
and does not copy a Python `_timestamp_assigner` stored on the previous wrapper.
Calling it after `with_timestamp_assigner()` silently drops the assigner, even
when `assign_timestamps_and_watermarks()` is correctly used post-source. The
`no_watermarks()`
handed to `from_source` is **not** the rejected end state in the D4 table: no
stream is left without watermarks, because the real strategy is attached
unconditionally on the next line. What changes is only *which operator* emits
them.

This is applied to **both** sources. Feature 007's gate compares a chat peak
second with a suppression interval, and §4.3's skew argument holds only because
both sides carry Twitch's clock. Correcting suppression alone would replace a
uniform ingestion-time mismatch with a genuine cross-clock mismatch inside the
comparison itself, which is strictly worse.

**Idleness safety, re-derived on subtasks.** With assignment moved downstream,
idleness is generated by the assignment operator per parallel subtask instead
of by the Kafka source per split. §4.1's argument was stated on splits, so it
is restated here:

| Element | Checked-in value |
|---|---|
| `suppression-events` / `chat-messages` partitions | 4 (`docker-compose.yml` `kafka-init`) |
| Source parallelism | 4 (`FLINK_PARALLELISM`) |
| Assignment/operator parallelism | 4 (same default parallelism, no override) |
| Source → assigner edge | one-to-one forward chain, no repartition |

Because those four hold together, each assignment subtask sees exactly the
records of exactly one split, so per-subtask idleness is equivalent to
per-split idleness and the I15/I16 bounds are unchanged. **This equivalence is
conditional, not structural.** Any future partition/parallelism mismatch, a
rescale, or a repartitioning step inserted between source and assigner breaks
it and requires revalidation of §4.1, §4.1.1, I15 and I16. It is recorded as
R13, and E3 remains the deployed proof for all of it; the offline suites use
stubs and cannot demonstrate the real PyFlink chain.

The post-source attachment also adds two Python operator stages, one for chat
and one for suppression, each at parallelism four. Static topology tests can
prove that the stages are requested, but only E3 may establish the deployed
job graph, TaskManager Python process count, and RSS impact.

### 4.2 Why suppression needs no timers, no Flink windows, and no join

Suppression is one notice-bounded half-open interval per broadcaster:
`[suppress_from_ms, suppress_until_ms)`. The deadline remains monotone within
an overlapping chain, while the lower bound prevents a spike that peaked
before the notice from being suppressed merely because its hold reports later
(FR-006, FR-007, autonomous decision 22):

- **Idempotent** — re-applying the same notice cannot move the deadline
  (FR-010, duplicate delivery; Twitch explicitly re-sends on uncertainty, see
  `MessageMetadata.message_id` in §2.2).
- **No backward deadline movement** — an earlier/equal candidate is a complete
  state no-op. An overlapping extension preserves the earliest retained start;
  a notice at or after the old deadline starts a new interval at its own
  occurrence.
- **Deadline-order-insensitive, not full-state-order-insensitive** — arbitrary
  notice ordering still yields the maximum candidate deadline, but the lower
  bound follows the explicit extension/no-op transition. The former blanket
  order-independence claim is therefore retired rather than applied to the new
  interval state.
- **No retroactive decision change** — a late notice affects only decisions
  made after receipt, and even then only peaks at or after its retained lower
  bound (FR-018).

The suppression input therefore never registers a timer and never buffers.
It reads state, writes state, emits nothing. All timers stay on the chat side,
exactly as today.

### 4.3 Late notices, skew, and lag

Because the gate is evaluated **at decision time against whatever state exists
then**, a notice that arrives after the decision has no effect on it. That is
precisely the behaviour FR-018 and User Story 1 scenario 6 require, and it
means the design has no "wait for the suppression stream" path to get wrong —
there is no waiting (D3 rejects the buffering alternative explicitly).

Clock skew between the two streams is bounded by construction: `sent_at` on
chat and `occurred_at_ms` on suppression both come from
`metadata.message_timestamp` through the same `to_epoch_ms()` helper, so both
are Twitch's clock, not the ingest host's (§2.2). The chat path's own measured
delivery lag (spec 004 research, p99 257 ms, p99.99 1,255 ms) is the best
available estimate for the notification path too, but it is **not** a
measurement of it — E4 in §8.

That shared-clock expectation is not sufficient as a trust boundary. The fixed
`SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30` is therefore enforced on source
event time for **both** inputs. `SentAtTimestampAssigner` accepts `sent_at`
only when it is a plain Python `int` (not `bool`) and is at or before
`source_clock_ms + 30_000`. Missing/null, string, float, bool, and later values
use Kafka `record_timestamp`. This changes only assigned event time: the chat
record is neither rewritten nor dropped, preserving the existing no-data-loss
behavior. Equality is accepted; +30,001 ms falls back. This prevents the
normally binding chat watermark from being poisoned once the assigner is live.

For suppression, the same fixed bound is enforced twice against
the same injectable/current receipt/source wall-clock basis. Before watermark
generation, `SuppressionTimestampAssigner` uses parsed `occurred_at_ms` through
`source_clock_ms + 30_000`; missing, unreadable, or later values use Kafka
`record_timestamp`. This protects event time only and preserves the original
payload. After schema/field decode, `process_element2` validates that original
value against `consumer_receipt_ms + 30_000` before delivery observation or
state access. Equality uses occurrence time upstream and is accepted
downstream with age zero and the clock-skew diagnostic. One millisecond beyond
uses Kafka record time upstream, then is rejected downstream as malformed
fields, counted and logged, with no delivery sample or state. This is defence
in depth against unit mistakes, bad clocks, and irreversible source-watermark
poisoning while retaining fail-open behavior (autonomous decisions 23-24).

### 4.4 Detector-state contamination (baseline, peak hold, cooldown, last fire)

`evaluate()` (`spike_detector.py:504`) is pure and owns four state effects:
bucket expiry, `hold` open/extend/close, the `emit` decision, and — via the
operator — `last_fire_second` (`clip_detector_job.py:812`). "Keep counting" on
its own is not sufficient, exactly as the roadmap warns:

- **Buckets / baseline** — untouched by design: the gate runs after
  `evaluate()` returns, so every message counted during suppression is in the
  baseline afterwards. This is what SC-004 measures.
- **Peak hold** — must not be cleared or shortened. Clearing a hold because its
  emit was suppressed would change `hold` and `expired_buckets` trajectories
  and break SC-004; it would also let a second, real peak in the same elevated
  period re-open a hold that should have been one episode.
- **Cooldown / `last_fire_second`** — the only genuinely ambiguous one.
  SC-004 requires *all* message-derived state after suppression to match a run
  without gating, and permits only "clip emission and the required
  would-have-clipped metric and structured log" to differ. Updating
  `last_fire_second` on a suppressed decision keeps the two runs
  state-identical; skipping the update makes the gated run diverge for
  `cooldown_seconds` (30 s) after every suppressed decision. Decision D6 takes
  the state-identical option, with the recall cost stated: for up to 30 s after
  a suppressed decision a genuine new episode cannot open — but that window
  sits inside a 120–180 s suppression window anyway, where a clip could not
  have been emitted regardless.
- **Gate bounds** — also downstream of every state write. The output filter
  uses the peak in `[suppress_from_ms, suppress_until_ms)`; a pre-notice peak
  remains eligible even if the hold closes after the notice. Thus the accepted
  overlap false negative is genuine hype whose peak is inside the interval,
  not any decision reported while an interval happens to exist (D5/D6,
  autonomous decision 22).
- **`hold_regressed`** — unaffected. It is an unmeasurable-second path that
  never produces an `emit`, so the gate never sees it.

### 4.5 PyFlink shape

`AnomalyDetector` becomes a `KeyedCoProcessFunction`:

- `process_element1` — today's `process_element` body, unchanged.
- `process_element2` — decode the suppression record, apply the max-register,
  write state. No timer, no output.
- `on_timer` — today's body, with the emission gated at the yield.

Both inputs are keyed on `broadcaster_id` with the same key type
(`connect(...).key_by(chat_key, suppression_key)`), and the suppression record
is mapped to the same `(broadcaster_id, json)` tuple shape the chat side
already uses (`clip_detector_job.py:1084`). Keying both inputs on the same
value is what makes the state channel-isolated, satisfying NFR-002 structurally
rather than by convention.

The effective per-stream order is therefore fixed, and it is the order §4.1.2
requires:

```text
real_strategy =
  bounded_out_of_orderness
  -> with_idleness(...)
  -> with_timestamp_assigner(...) LAST
env.from_source(kafka_source, WatermarkStrategy.no_watermarks(), name)
  -> assign_timestamps_and_watermarks(real_strategy)   # Python assigner runs here
  -> (chat only) CommandFilter / mapping
  -> key_by(broadcaster_id)
  -> connect(...).process(AnomalyDetector)
```

Event time is established by the assignment operator, not by the source, so
`ctx.timestamp()` in `process_element1` is trusted plain-integer, at-most-+30 s
`sent_at` (or the Kafka record-time fallback), and in `process_element2` is
trusted `occurred_at_ms` (or its Kafka record-time fallback). Chat fallback
does not reject or drop the record. Before decisions 25-26, neither assignment
path was safe and effective.

**The `broadcaster_id` that does the keying is the one in the payload, because
the payload is all the consumer can see.** The job's Kafka sources are built
with a value-only deserialization schema, exactly as the chat source is, so the
record key never reaches `process_element2`. That makes key/payload agreement a
**producer** invariant — asserted in the producer's own tests (T033) where the
key is actually visible — and makes payload validation the consumer's job:
`broadcaster_id`, `notice_type` and `occurred_at_ms` are checked on the decoded
value and rejected there when malformed. Writing a consumer test that "asserts
the key matches the payload" would be testing something the consumer cannot
observe, so no consumer task does.

### 4.6 Delivery lag has three states, not two

NFR-005 asks for lagging delivery to be distinguishable from healthy delivery.
On a topic that is legitimately silent for hours, a two-state answer is not
merely hard, it is unobtainable: silence is exactly what a healthy system and a
stalled broker path both look like from the consumer, and no amount of
instrumentation on the consumer side can separate them without traffic to
measure. A heartbeat would create that traffic, and it is out of scope — it
would be a second protocol on the topic, with its own failure modes, purely to
service a monitoring question.

So the classification is defined on **trusted received records**, and silence
is named rather than guessed:

| Observation in a window | Classification |
|---|---|
| A record whose clamped `delivery_age_ms` at consumer receipt ≤ `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS * 1000` (default 30 s) | healthy |
| A record whose clamped `delivery_age_ms` at consumer receipt > that threshold | lagging |
| No record at all | **idle/unknown** — neither healthy nor lagging |

At `process_element2` entry, the consumer captures `consumer_receipt_ms` from
an injected clock in tests and the current consumer clock at runtime. The
record is first decoded and field-validated. It is then rejected as malformed
fields, counted and logged, if
`occurred_at_ms > consumer_receipt_ms + 30_000`; this fixed trust check occurs
before all delivery observation and state access. For an accepted record, the
classification input is exactly:

```text
raw_delivery_age_ms = consumer_receipt_ms - occurred_at_ms
delivery_age_ms = max(0, raw_delivery_age_ms)
```

That clamped value is compared with the warning threshold and observed as
seconds in `suppression_delivery_age_seconds`. A negative raw age within the
30-second allowance indicates tolerable clock skew; it is observed and
classified as zero and produces a structured diagnostic log rather than
expanding scope with another metric. A raw age below -30 seconds is not a
delivery observation at all; it fails the timestamp trust check.
`received_at_ms` remains optional and diagnostic-only. When present it can
split Twitch-to-producer (`received_at_ms - occurred_at_ms`) from
producer-to-consumer (`consumer_receipt_ms - received_at_ms`) latency, but
neither component is used for classification and no logic depends on the field
being present.

Two consequences are stated so nothing overclaims:

- **Coverage and delivery answer different questions.** Coverage state says
  whether the `channel.chat.notification` subscriptions exist. Complete coverage
  plus a silent topic does **not** prove the broker path works; it proves only
  that nothing was expected to flow. The two signals are read together, never
  substituted for one another.
- **The feature does not claim to detect a stalled delivery path during a period
  with no real notices.** That is a deliberate limitation of a fail-open
  auxiliary signal, not an oversight; the cost of the undetected case is the
  pre-007 behaviour, which is the floor this whole design already accepts.

The metric shape follows from that. The
`suppression_delivery_age_seconds` per-record observation plus a
`suppression_records_consumed_total{lag_class}` counter is what makes silence
visible as *no samples*. A continuously refreshed per-channel gauge driven
from `on_timer` would have to publish some number every second for every key,
including during legitimate silence, and whatever it published would be a
fabricated statement about a path nothing had traversed. It is rejected for the
same reason a fabricated `occurred_at_ms` is rejected in D9.

### 4.7 Keeping the sparse-source design offline-testable

Everything in §4.1, §4.1.1 and §4.6 is a claim about fixed/testable values —
topic, offset mode, out-of-orderness, idleness, partition count against
parallelism, and the pure fields `delivery_lag_warn_seconds=30` and
`checked_in_gating_enabled=False`, plus the fixed contract constant
`SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`. The future-skew bound is deliberately
not an environment setting. If those values live as literals inside
`clip_detector_job.py`, the only way to assert them is a test that imports
PyFlink, and that test is skipped whenever `apache-flink` is not installed.
The evidence for the feature's highest risk would then be conditional on an
optional package.

They are therefore lifted into a pure `SuppressionSourceSettings` construct in
`spike_detector.py` — the module that already holds the detector's constants and
imports no PyFlink — and `clip_detector_job.py` builds its `KafkaSource` and
`WatermarkStrategy` from that construct. The values are then asserted twice, in
two places that always run: `test_spike_detector.py` for the construct itself,
and static assertions over the checked-in `docker-compose.yml` for the wiring.
The PyFlink-importing tests in `test_clip_detector.py` remain, use fakes and
start no cluster, and are conditional **only** on the pinned
`apache-flink==1.18.0` already being present.

One boundary is stated so nothing overclaims after §4.1.2. A fake or stub
`WatermarkStrategy`/`DataStream` can assert that the job *calls*
`assign_timestamps_and_watermarks(real_strategy)` on each source stream, and
that `from_source` receives only a `no_watermarks()` placeholder — that is a
wiring assertion and it is worth having. It cannot demonstrate that the real
PyFlink chain then executes the Python assigner, emits watermarks from the
assignment operator, or applies idleness per subtask. That remains E3.

---

## 5. Constitution: fail-open against "no data loss"

The constitution's non-negotiable is **"No data loss in the event pipeline
(Kafka → Processing → Storage)"**, alongside "Reliability: ensure no highlight
moments are missed due to system failures".

The design does not weaken either, and the distinction is worth stating
precisely because decision 3 (fail open) could look like a trade against it:

1. **The chat pipeline is untouched.** Every `channel.chat.message` event is
   mapped, published to `chat-messages`, counted into `message_counts`, and
   folded into the baseline exactly as before. Suppression removes no record
   from any topic, no bucket from any state, and no row from Postgres. The
   gated quantity is a *derived emission decision*, not pipeline data.
2. **Suppression is an auxiliary signal with an explicit absent state.** When
   the notification subscription is missing, revoked, lagging, or the Kafka
   topic is empty, the detector reads "no active deadline" and behaves exactly
   as the pre-007 system. Failing closed would convert an auxiliary-signal
   outage into a clipping outage — a *self-inflicted* missed-highlight failure,
   which is the risk the "Reliability" value names.
3. **The one deliberate loss is a policy choice, not a fault.** A suppressed
   would-have-clipped spike does not become a clip. That is the feature. It is
   made observable rather than silent (FR-012: metric **and** structured log,
   per channel, distinguishable from coverage/delivery/malformed/capacity
   signals — NFR-006).
4. **Malformed suppression input is dropped at the producer, not guessed.**
   This is the one place the design deliberately differs from the chat path,
   which publishes a message with `sent_at: null` rather than dropping it
   (`to_epoch_ms` docstring, `eventsub_pool.py:177`). For chat, dropping would
   be data loss; for suppression, publishing an event with no trustworthy time
   would create a *fabricated* deadline, which FR-017 forbids outright. The
   asymmetry is intentional and is recorded as D9.

Post-design re-check is in `plan.md` §"Constitution re-check after design".

---

## 6. Capacity and ramp: what the 400/450 change actually does

> Replaces the former "what the 400/400 change actually does" section. The
> zero-width-hysteresis analysis it contained no longer describes the system:
> autonomous decision 27 splits entry from retention, so the band exists again.

`docker-compose.yml` currently carries the Feature 007 interim value
`JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=400`, itself reduced from the pre-007
`800` / `900`. FR-013 sets the amended target: entry `400`, retention and
maximum `450`.

`resolve_thresholds()` (`stream_monitoring_service.py:105`) rejects only
`leave < join`, so 400/450 is accepted, and `compute_desired_set()` (`:136`)
already implements exactly the required semantics without a code change: it
computes `(previous | top_join) & top_leave`, so a channel enters only via
`top_join` (rank ≤ 400) but is retained through `top_leave` (rank ≤ 450). That
is the entry/retention asymmetry FR-013 describes — a fresh channel ranked
401-450 does not enter, an incumbent at the same rank stays — and it restores
the 50-channel hysteresis band that 400/400 had removed. The module's own
warning about "thrashing the desired set once per poll and destroying Flink's
baseline" applies to `join == leave`, and no longer applies here.

`desired_set_churn_total` is therefore kept as **advisory** telemetry: entries
plus departures per poll, bounded labels, no per-channel growth. It carries no
numeric release gate and no observation window; nothing about it can block
enabling suppression gating (NFR-007, SC-011, D10a). Its purpose is to let an
operator see monitored-set movement, not to license the threshold choice.

**What the amendment actually costs is slack, not churn.** At 450 channels the
pool holds 900 of 900 subscriptions. Three consequences follow, and each is
engineered rather than absorbed (D14):

1. *Perfect packing is not guaranteed.* 3 × 300 holds 450 co-located pairs only
   if every connection ends at an even occupancy. A connection at 299 strands
   one slot that no whole pair can use, so placement must be able to split a
   pair across two connections, reserving both halves atomically.
2. *Exhaustion becomes an ordinary operating state.* "No slot anywhere" at the
   ceiling is expected, not anomalous, and must be reported as a distinct
   capacity condition rather than as a provider refusal (which writes the
   seven-day per-channel refusal cache) or a transient fault (which arms a
   growth backoff for a condition that waiting cannot fix).
3. *Any stranded slot is now load-bearing.* A connection marked `full_at` below
   300, an enabled subscription this feature did not create, or a delete that
   silently failed each removes capacity the model has already allocated
   (R15-R17).

Ordering constraint, from the roadmap risk register and repeated here because
it is the single most important sequencing rule in the feature: **the pool's
two-slot capacity behaviour must be proven by deterministic tests before the
ramp configuration changes.** Lowering the ramp first would hide a pool defect
behind a smaller set; raising per-channel subscriptions first without the ramp
change would ask for far more subscriptions than the 900 ceiling allows. The
amendment extends the same rule: the amended placement, classification, and
`full_at` behaviour are proved deterministically before the checked-in
retention threshold moves to 450, and deployed convergence is proved at
400 channels / 800 subscriptions (E2a) before the deployed ramp reaches exact
capacity (E2b).

---

## 7. Decisions

| ID | Decision | Alternatives rejected |
|---|---|---|
| **D1** | The two-slot model lives **inside `EventSubPoolTransport`**: one `_Slot` per (channel, type), a channel-level coverage view, and a `list()` that yields a channel only when both types are enabled. `reconciler.py` stays channel-keyed and is not restructured. | Subscription-keyed reconciler diff (touches refusal cache, ordering, batching, metrics, and every reconciler test for no behavioural gain). A second transport instance (doubles sockets; breaks the 3-connection limit). |
| **D2** | A 403-class refusal on the **auxiliary** type while chat is live is recorded in the pool as auxiliary-refused, does not propagate `SubscriptionRefusedError` to the reconciler, and leaves the channel chat-covered and visibly degraded. The hold-off is **bounded**: repeated notification creates are suppressed for `AUXILIARY_REFUSAL_RETRY_SECONDS` = 3600 s, after which the channel is repairable again; a websocket reconnect or connection retirement forces re-eligibility immediately; successful creation or adoption clears the state (superseded the original permanent form — see autonomous decision 17). | Propagating the refusal (marks the whole channel refused for 7 days — kills chat for that channel). Retrying every pass forever (unbounded noise and create budget at 400 channels). Never retrying until restart (a transient refusal becomes a permanent coverage hole and quietly violates FR-001). |
| **D3** | Suppression reaches Flink as a **dedicated `suppression-events` Kafka topic**, keyed by `broadcaster_id`, consumed by a second `KafkaSource` connected to the keyed chat stream through a `KeyedCoProcessFunction`. | Broadcast state (fan-out to all subtasks and a non-keyed state model for per-channel data). Sentinel records inside `chat-messages` (breaks the frozen chat schema contract and the `CommandFilter`/mapping path). Postgres or Redis lookup from the operator (per-decision I/O on the hot path; violates "Kafka for all inter-service messaging"). |
| **D4** | The suppression stream uses `for_bounded_out_of_orderness(WATERMARK_OUT_OF_ORDERNESS_SECONDS)`, `with_idleness(SUPPRESSION_IDLENESS_SECONDS = 5)`, `KafkaOffsetsInitializer.latest()`, and 4 partitions matching `FLINK_PARALLELISM`. **Clarified by autonomous decisions 25-26:** both streams build the real strategy in the exact order bounded out-of-orderness → idleness → Python assigner last, then attach it with `DataStream.assign_timestamps_and_watermarks()` after `env.from_source(source, WatermarkStrategy.no_watermarks(), ...)`. PyFlink 1.18 otherwise drops the assigner either at `from_source` or when a later `with_idleness()` returns a fresh wrapper (§4.1.2). Both source assigners apply fixed 30-second trust using the injectable/current source wall clock and fall back to Kafka `record_timestamp` without rewriting or dropping payloads. | `no_watermarks()` as the stream's effective strategy. Passing a Python assigner to `from_source`. Calling `with_idleness()` after `with_timestamp_assigner()`. A new Java supplier. Longer suppression idleness than chat. `earliest()` offsets. Blindly assigning untrusted payload time. |
| **D5** | **Clarified by autonomous decision 22:** the gate compares the decision's peak second (`spike.detected_at_seconds`) with the notice-bounded half-open interval `suppress_from_ms <= peak_ms < suppress_until_ms`, not merely with the deadline and never with the report second. | Deadline-only gating (incorrectly suppresses a pre-notice peak whose hold reports later). Report-time gating (a burst that peaks inside the interval but reports after `hold_cap_seconds` escapes). Either-of-the-two (again suppresses a pre-notice peak). |
| **D6** | Gating is an **output-only filter** at the end of `on_timer`. Every state write — buckets, expiry, hold, chain timer, **and `last_fire_second`** — happens exactly as it does today; interval membership changes only the final yield and required suppression signals. | Gating inside `evaluate()` (changes hold/`emit` trajectories; breaks SC-004 and the pure module's test suite). Skipping the `last_fire_second` update (diverges from the ungated run for 30 s after every suppressed decision; makes SC-004's "identical state" unprovable). |
| **D7** | Suppression **windows are applied at the consumer** from `SUPPRESSION_GIFT_WINDOW_SECONDS` / `SUPPRESSION_RAID_WINDOW_SECONDS`; the producer publishes only the raw notice (`notice_type`, `occurred_at_ms`). | Producer-computed `suppress_until` (bakes policy into the topic, makes retention replay wrong after a tuning change, and splits the window constants across two services). |
| **D8** | The contract is **versioned** (`schema_version`) and carries exactly the identity/notice/time fields the consumer needs; `viewer_count` is optional and diagnostic-only. | An unversioned payload (no safe way to add a field later against a live topic). Carrying the full notice payload (user content on an operational topic, no requirement). |
| **D9** | A notice with no trustworthy channel identity **or** no parseable `occurred_at_ms` is **not published**; it increments a malformed counter and logs, per FR-017. | Publishing with `occurred_at_ms: null` (the consumer would have to invent a time — a fabricated deadline). Substituting the ingest clock (a guessed deadline wearing a trustworthy field name). |
| **D10** | ~~Ship 400/400 exactly as locked…~~ **Superseded by autonomous decision 27 and replaced by D10a.** The zero-width band it accepted no longer exists. | (historical) |
| **D10a** | Ship the approved **entry 400 / retention-and-maximum 450** thresholds exactly as configured. `compute_desired_set()`'s existing `(previous \| top_join) & top_leave` already produces the required asymmetry, so no hysteresis logic is added. `desired_set_churn_total` is retained as advisory bounded-label telemetry with no numeric gate (§6, NFR-007, SC-011). | Silently deviating from the configured thresholds, or special-casing hysteresis in code (hides policy from configuration — the reason decision 14 rejected it stands). Keeping a numeric churn release gate (it existed only to justify a zero-width band). Equal 450/450 thresholds (removes the retention band the amendment exists to restore). |
| **D11** | Emission gating has an operator kill switch, `SUPPRESSION_GATING_ENABLED`. The **code default in `SuppressionConfig` is `true`**, while `docker-compose.yml` **checks in `false`**, so a deploy is inert until an operator changes the compose value after E1, E2a, E2b and E3 pass. With it false the detector behaves exactly as pre-007 while both subscriptions and the topic stay in place. | Revert-only rollback (a code deploy to undo a detection-policy problem, at the moment clips are being lost). Checking in `true` (a deploy would start gating before any deployed evidence existed). |
| **D12** | Deployment is staged, and no stage takes two risks at once (autonomous decisions 16 and 27). (1) The ramp is reduced to 400/400 on the **current** single-subscription revision and allowed to converge — 400 × 1 = 400 of 900, unconditionally capacity-safe and **not** gated by E1. (2) The feature revision is deployed with the retention threshold still **400**, so first dual convergence is 400 channels / 800 subscriptions with a cushion; E1 and E2a are taken there. (3) The account is swept for enabled subscriptions the pool does not own. (4) Only then does `LEAVE_THRESHOLD` move to the checked-in target **450**, reaching exact 900-of-900 capacity, followed by E2b. Rollback runs the same logic in reverse: gating off; on a capacity incident lower retention to 400 and reconverge; unwind the transport **while thresholds are 400/400**; wait for the notification subscriptions to disappear; only then restore the single-subscription ramp. | One-step deploy of thresholds and transport together (the Redis-resident desired set makes the reconciler chase far more subscriptions than the ceiling allows before the first poll rewrites it — R9). Deploying the dual transport straight to 450 (first dual convergence and first exact-capacity operation in one step, with no cushion to diagnose from). Raising the retention threshold before unwinding the auxiliary subscriptions during rollback (R11a). |
| **D13** | **Clarified by autonomous decisions 23-24:** delivery health is classified per trusted received record from `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`. The same fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` first protects source timestamp assignment and then, after decode/field validation, rejects the unchanged original payload as malformed fields before observation/state; accepted negative raw age is clamped to zero and structured-logged as clock skew. Compare only the clamped value with `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` = 30 s — healthy at or below, lagging above — and observe it in `suppression_delivery_age_seconds`. Optional `received_at_ms` remains diagnostic-only, and no record means **idle/unknown** (§4.6). | A continuously refreshed per-channel delivery gauge from `on_timer` (must fabricate a value during legitimate silence). A heartbeat or synthetic record (a second protocol, out of scope). No future bound (lets unit mistakes create far-future suppression and poison watermarks). Rejecting every future timestamp (turns harmless skew into avoidable false positives). |
| **D14** | ~~The zero-width 400/400 band's cost gets a numeric release disposition…~~ **Superseded by autonomous decisions 27 and 19's banner, and replaced by D14a.** The 8-changes-per-poll, 24-hour gate is removed together with the zero-width band that motivated it. | (historical) |
| **D14a** | Exact capacity is engineered, not assumed (autonomous decision 28). Four behaviours ship together: co-location-first placement with an **atomic split-pair fallback** — one slot reserved on each of two connections, in one critical section, when no connection has two free but the pool has at least two, with a post-reservation partial failure keeping the successful half; a distinct `PoolCapacityError` and a distinct capacity classification on the create-failure metric, separate from provider refusal and transient faults; **no transient growth backoff** for a hard ceiling, because waiting cannot fix it and arming it delays the next legitimate placement; and `full_at` cleared and re-evaluated on reconnect/retirement with below-cap full state exposed. | Running exact capacity on today's placement and classification (turns parity fragmentation into a false full pool and a capacity ceiling into an unexplained refusal). Pair compaction/migration between connections (deletes and recreates live coverage, needs its own ordering/failure/idempotence rules, only to recover locality that splitting already handles). Placement changes without the error and `full_at` work (the pool reaches 900 but cannot explain itself there). |
| **D15** | Key/payload agreement is a **producer** invariant, asserted where the key is visible (T033). The job's sources use value-only deserialization, so `process_element2` never sees the record key; consumer routing and state use the payload `broadcaster_id`, and the consumer's duty is malformed-**payload** rejection. | Consumer-side key/payload comparison (asserts something the consumer structurally cannot observe). Switching to a key-and-value deserialization schema purely to enable that check (a change to the source shape for no behavioural gain, on the hot path). |
| **D16** | The suppression source's configuration — topic, `latest()` offsets, out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS`, expected partitions/parallelism, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` — plus fixed contract constant `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` live in pure `spike_detector.py`. `test_spike_detector.py` asserts all pure values and static compose checks assert both required wiring and the absence of a future-skew environment variable; `SuppressionConfig.from_env()` remains the runtime reader with gating defaulting to `true`, and `clip_detector_job.py` builds the real source and the real watermark strategy from the pure settings, attaching that strategy with `assign_timestamps_and_watermarks` per §4.1.2. Stub-level tests may assert that call and the `no_watermarks()` placeholder, but never that the assigner executed (§4.7). | Literals inline in `clip_detector_job.py` (the only assertions would import PyFlink and skip when it is absent, making the highest-risk evidence conditional). A configurable future-skew allowance (unnecessary environment surface for a trust boundary). A new configuration module (new `FLINK_PYFILES` entry and compose mounts). |

---

## 8. Risks

| ID | Risk | Mitigation | Residual |
|---|---|---|---|
| **R1** | A two-type `list()` where one walk fails could look "complete" and let the reconciler drop live subscriptions. | The enumeration is complete only if **both** walks finish; either failure marks it incomplete, which already holds drops back (`reconciler.py:723`). Covered by a deterministic pool test. | A channel repaired one pass later than today at worst. |
| **R2** | Rendezvous imbalance puts one connection at 300 while the pair for a channel needs two slots. | `route()` places a pair where two slots fit; if the channel's home connection has one slot, the pair splits across connections and both slots are tracked independently. At exact capacity that split is reserved atomically across two connections (D14a). | Slightly less locality; a socket death then touches two channels' partial coverage rather than one channel's whole coverage. Both states are already modelled. |
| **R3** | The suppression stream becomes the binding watermark minimum and stalls all detection. | D4 (real watermarks attached with `assign_timestamps_and_watermarks` per §4.1.2, shorter idleness than chat, `latest()` offsets, partitions == parallelism, and source-level future-time trust before watermark generation). Deterministic replay covers silence and monotonic combined-watermark behavior when an over-future record falls back to Kafka record time before downstream rejection. | PyFlink-level idleness and timestamp-assigner behavior cannot be fully reproduced offline; E3 is the deployed gate, and D11 is the immediate lever if either failure appears. |
| **R4** | A notification arrives after a decision, or after a spike peaked but before its hold reports. | No buffering or retraction (FR-018). A late notice affects later decisions only, and the lower bound ensures a pre-notice peak remains eligible even if reported afterward. Delivery lag is observable from trusted records via `suppression_delivery_age_seconds`. | False-positive clips caused by notices arriving after the relevant peak/decision remain possible and are measured by E4; genuine hype peaking before a notice is intentionally not counted as an accepted suppression false negative. |
| **R5** | **Slot fragmentation at exact capacity.** With 900 of 900 slots spent, parity across three connections can leave the last two free slots on two *different* connections. A placement that only takes whole pairs on one connection reports a pool with room as full and stalls convergence at 449 channels. | D14a's atomic split-pair fallback: when no connection has two free slots and pool-wide free slots ≥ 2, reserve one on each of two connections in a single critical section. Deterministic tests construct the fragmented state directly; E2b exercises it deployed. | Reduced locality for the split channels, and a socket death leaves them partially covered — a state the reconciler already repairs. Repeated split/repair cycles can fragment further; that is visible through coverage state and connection occupancy. |
| **R6** | `channel.chat.notification` costs more than 0 against `max_total_cost = 10`. | E1 checks `total_cost` on the deployed system as early in dual-coverage convergence as possible; a non-zero cost blocks dual-coverage sign-off and the feature. The preliminary 400/400 ramp-down on the single-subscription revision is 400 × 1 and is **not** blocked by E1. | Feature-level: if non-zero, the two-subscription design is not viable on this token and the plan must stop rather than degrade. The preliminary ramp-down is independently safe and need not be reversed to run the check. |
| **R7** | An unknown or renamed `notice_type` silently stops triggering suppression. | Trigger set is an explicit allow-list; everything else is counted under an `other` bucket so a vanished category is visible as a distribution change rather than as silence. | Twitch renaming a trigger type is detected operationally, not automatically. |
| **R8** | The suppressed-emission metric/log is mistaken for a detection outage. | `anomalies_detected_total` still increments for a suppressed decision; the suppression is a separate counter, so "detected but not clipped" is computable and `AnomalyDetectionStalled` keeps its meaning. | Dashboards need the new series added; documented in OPERATIONS.md. |
| **R9** | Deploying the two-subscription transport while Redis still holds the old ~800-channel desired set asks for ~1,600 subscriptions against a 900 ceiling. `chat:desired` survives a restart, and the reconciler converges to it immediately while the poller only rewrites it up to 120 s later. | Two-step deployment: lower the ramp to 400/400 on the **current** single-subscription revision and let the desired set converge, **then** deploy the feature revision (D12; plan "Rollout and rollback"). | If the two steps are collapsed by mistake, the failure is loud (mass refusals, `pool is at its 3-connection limit`) and recovers by lowering the threshold; no data is lost. |
| **R10** | A long-idle suppression subtask re-enters the two-input watermark minimum when one isolated notice arrives, briefly holding the operator watermark and delaying per-second evaluation for the keys on that subtask. | Accepted with a conservative bound of `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` (§4.1.1, data-model I16), asserted offline in the replay harness's simplified model (T050) and measured deployed as part of E3, which must exercise **both** prolonged silence and an isolated notice after silence. | A short, bounded evaluation delay on a sparse-notice channel set — far smaller than the 120 s minimum suppression window. Sustained notice traffic never reaches the bound. |
| **R11** | ~~Raising thresholds back toward the single-subscription ramp while notification subscriptions still exist would permit more than 800 subscriptions…~~ **Replaced by R11a**, which restates the same hazard for the amended thresholds. | | |
| **R11a** | During rollback, relaxing the retention threshold before capacity is unwound over-commits the account. With two subscriptions per channel a retention threshold above 450 permits more than 900 subscriptions, and unwinding the dual transport from a 450-channel set leaves the single-subscription revision converging to a set larger than the ramp it is about to receive. | The rollback order is fixed and invariant-driven: gating off → on a capacity incident lower `LEAVE_THRESHOLD` to **400** and reconverge to 400 channels / 800 subscriptions → unwind the transport **with thresholds at 400/400** → wait until notification subscriptions are gone and total subscriptions equal the desired channel count (400) with stable coverage metrics → only then restore the single-subscription ramp. The retention threshold is never above 450 while dual coverage is live, and must be back at 400 before the transport is unwound (plan "Rollback order", autonomous decisions 21 and 27). | If the order is inverted anyway, the failure is the same loud refusal mode as R9 and is recovered by lowering the threshold again. |
| **R12** | A corrupt unit, wrong type, or bad clock places source payload time far in the future. On suppression, downstream rejection is too late to undo watermark damage; on chat, there is intentionally no downstream rejection and a poisoned binding watermark can stall real-time timers. | Apply fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` in both source assigners. Suppression retains its second downstream validation. Chat accepts only plain `int` (not `bool`) `sent_at`; missing/null/string/float/bool/over-bound values use Kafka `record_timestamp` without rewriting, rejecting, or dropping chat. Equality is accepted and +30,001 ms falls back. Replay covers monotonic assignment semantics; E3 proves deployment. | A bad but plain-integer timestamp within 30 seconds can shift event time slightly. Suppression remains visible through downstream rejection; chat fallback requires deployed clock/watermark observation because it deliberately emits no malformed-record rejection. |
| **R13** | PyFlink 1.18's `from_source` silently drops a Python `TimestampAssigner`, so a strategy that looks correct in review can leave event time as Kafka record time on either stream. Fixing it moves idleness generation from per-split inside the source to per-subtask in the assignment operator and adds two Python stages at parallelism four. | Attach every real strategy post-source on both streams (§4.1.2, decision 25), preserve partitions = source parallelism = assignment/operator parallelism = 4 with a one-to-one edge, and measure the deployed job graph, TaskManager Python process count, and RSS in E3. | Topology equivalence and process/RSS cost are deployed properties. A mismatch, rescale, repartition, or unexpected Python-worker footprint must be revalidated rather than inferred from local tests. |
| **R14** | PyFlink 1.18's `with_idleness()` returns a fresh wrapper without a previously stored Python `_timestamp_assigner`; the visually plausible order assigner → idleness silently disables payload-time assignment and its source trust bound on either stream. | Build every real strategy in the binding order bounded out-of-orderness → `with_idleness(...)` → `with_timestamp_assigner(...)` last. Stub tests inspect the order/result; E3 proves the deployed assigners survive and event time follows trusted payload timestamps. | This is an implementation-detail invariant with no runtime warning. A future PyFlink upgrade must re-check wrapper behavior, and local fakes remain insufficient deployed evidence. |
| **R15** | **Foreign enabled subscriptions.** Anything enabled on the client-id/user-id pair that this pool does not own — an earlier revision's leftovers, another process, a manual experiment — consumes the same 900 slots. With no reserve, one such subscription makes the 450th channel unplaceable, and the symptom is indistinguishable from a defect in the pool. | An account-wide enumeration and sweep is a required rollout step **before** the retention threshold moves to 450 (plan forward order, D12), and the capacity classification on the create-failure metric plus per-connection occupancy makes the resulting shortfall legible rather than mysterious. | Nothing prevents a new foreign subscription appearing later; it is detected as a gap between complete-coverage channels × 2 and total subscriptions, and by capacity refusals at a channel count below 450. |
| **R16** | **Below-cap `full_at`.** A connection that recorded a refusal at an occupancy under 300 permanently offers fewer slots than the capacity model counts on, silently converting exact capacity into an unreachable target. | D14a clears and re-evaluates `full_at` on reconnect and retirement, and exposes below-cap full state so stranded capacity is visible rather than inferred from an unexplained refusal (E2b). | Between a below-cap refusal and the next session transition the connection still offers fewer slots; the condition is visible, and lowering the retention threshold to 400 restores the cushion while it is diagnosed. |
| **R17** | **Failed deletes leak slots.** A delete the pool reports as failed, or one that races a reconnect, can leave an enabled subscription the pool no longer tracks. Previously the 100-slot reserve absorbed this; at exact capacity it is a permanent leak. | Deletion already treats "already gone" as success and retains retryable state after a one-sided failure; the leak is detected as the same total-versus-coverage discrepancy as R15 and is cleared by the same sweep. Capacity refusals are classified distinctly so the leak does not present as a provider refusal. | A leak between sweeps reduces the reachable channel count by one channel per two leaked slots; it is bounded, visible, and recoverable without a code change. |
| **R18** | **Exact convergence is slower and less forgiving.** At 900 of 900 there is no slack to absorb in-flight create/delete overlap, so a churn event at the boundary can transiently need a slot that a pending delete still holds. | Convergence is proved deterministically before the threshold moves (T060-T062) and deployed at 400/800 first (E2a) before exact capacity (E2b); a hard capacity condition does **not** arm a transient growth backoff, so the next pass retries immediately once the delete lands. | A boundary channel may take an extra reconcile pass to become complete at exact capacity. Visible as a short-lived partial-coverage series, not as data loss. |

---

## 9. Evidence deferred to the deployed machine

None of the following can be produced on this workstation, and none may be
claimed from unit tests, fixtures, or reasoning.

| ID | Evidence | Gate |
|---|---|---|
| **E1** | `Get EventSub Subscriptions` shows both types on live sessions with `total_cost` unchanged at 0 against `max_total_cost` 10. | Blocks dual-coverage sign-off and enabling gating, and blocks the feature if cost is non-zero. Does **not** block the preliminary 400/400 ramp-down on the single-subscription revision, which is 400 × 1 and safe on its own |
| **E2a** | With the retention threshold still 400: 400 channels converge to 800 subscriptions, no connection over 300, `eventsub_channel_coverage{state="complete"}` == desired count, and the pool refuses the 401st channel. Roughly 100 slots remain free at this stage by construction, which is what makes it the safe place to prove dual coverage. `desired_set_churn_total` is recorded as advisory context only. | Blocks the ramp to 450 and therefore blocks exact capacity (SC-006). No churn observation window gates it (NFR-007, SC-011) |
| **E2b** | After the account sweep and the ramp to entry 400 / retention 450, prove exact capacity with relational equality: total subscriptions **== 900**; total subscriptions **== 2 ×** complete-coverage channels; every connection occupancy **≤ 300** and their sum **== 900**; free slots **== 0**; the 451st qualifying channel is **excluded**. Then exercise the exact-capacity paths: a pair placed one slot on each of two connections when no connection has two free; a deliberately removed half converging back to complete without exceeding 900; a capacity refusal reported under its own capacity classification, not as a provider refusal, and without arming a transient growth backoff; and a below-cap `full_at` being visible and re-evaluated after reconnect or retirement. | Blocks the amended SC-006 sign-off and enabling gating (FR-013, FR-014, NFR-001, decisions 27-28). "Approximately 900" is not an acceptable reading |
| **E3** | With suppression silent for one hour, verify unchanged chat watermark lag; then the isolated-notice hold bound. Exercise both assigners at exact +30,000/+30,001 ms and chat missing/null/string/float/bool cases: trusted plain-int chat `sent_at` and trusted suppression occurrence drive event time, while every fallback uses Kafka record time without chat loss or combined-watermark poisoning. Confirm the real strategies retain assigners after idleness, watermarks originate from both post-source Python assignment stages, each stage has parallelism four and one partition per subtask, and record TaskManager Python process count and RSS attributable to the two added stages. All are deployed measurements. | Blocks enabling gating in production (R3, R10, R12-R14) |
| **E4** | Twitch-occurrence-to-consumer age distribution from trusted records in `suppression_delivery_age_seconds` against `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` = 30 s, downstream `reason="fields"` rejection/warning visibility for the unchanged original over-future payload after source timestamp fallback, and a captured real gift-bomb/raid slice showing notice-bounded suppression firing on the intended bursts without suppressing pre-notice peaks. | Real-burst confirmation for SC-003, tuning/adequacy evidence for the window defaults under SC-005, and deployed evidence for delivery age, timestamp assignment, and rejection behavior (NFR-005, SC-010); deterministic boundary behavior remains local |
| **E5** | Rollback rehearsal: `SUPPRESSION_GATING_ENABLED=false` restores pre-007 emission behaviour with no other change, and the capacity-safe rollback order is executable — retention lowered to 400 and reconverged on a capacity incident, the transport unwound with thresholds at 400/400, and the single-subscription ramp restored only after the notification subscriptions are gone. | Blocks production enablement (D11, R11a) |

# Phase 0 Research: Suppress Gift and Raid Chat Bursts

**Feature**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04
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
| Monitored-channel ceiling | 400 | FR-013 (locked, decision 5) |
| Steady-state subscriptions at the ceiling | 800 | 400 × 2 |
| Reserved headroom | 100 | 900 − 800 (FR-014) |

The headroom is **global, not per connection**. Rendezvous routing
(`_score`, `eventsub_pool.py:164`) does not balance perfectly, so one connection
can reach 300 while another has room; `route()` already falls through to the
next connection in rendezvous order, which is what keeps that from becoming a
failure (see D1/R2).

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
| `route()` (`:730`) | first connection with `load < cap` | must be able to place **two** subscriptions, and must prefer the connection already holding the channel's other type |
| `_reserve()` (`:748`) | `reserved += 1` | reserve the number of subscriptions actually about to be created, in one critical section |
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
| No watermarks at all on the suppression source | `WatermarkStrategy.no_watermarks()` emits none, so the min never advances | Forbidden. The suppression source gets a real bounded-out-of-orderness strategy (D4) |
| A quiet partition holds the min | Idle split, documented in "Dealing With Idle Sources": "the watermark will be held back, because it is computed as the minimum over all the different parallel watermarks" | `with_idleness(SUPPRESSION_IDLENESS_SECONDS)`, set **below** the chat stream's `WATERMARK_IDLENESS_SECONDS = 10` so the suppression input is never the last to be released |
| Old offsets replayed at start-up | `earliest()` would feed hours-old `occurred_at_ms`, pinning the operator watermark in the past until the backlog drains | `KafkaOffsetsInitializer.latest()`, matching the chat source (`clip_detector_job.py:1049`) (D4) |

A fourth, subtler case: a source **subtask with no split at all**. The existing
`chat-messages` topic was deliberately re-provisioned to 4 partitions to match
`FLINK_PARALLELISM` (`docker-compose.yml:90-109`). The suppression topic is
created with the same 4 partitions for the same reason, so every subtask owns
exactly one split and the "no splits assigned" idleness behaviour is never
relied upon. This is a design constraint, not an observation; E3 in §8 verifies
it on the deployed system.

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

That shared-clock expectation is not sufficient as a trust boundary. After
schema/field decode and before any delivery observation or state write, the
consumer enforces the fixed
`SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30`. A timestamp at or below
`consumer_receipt_ms + 30_000` is accepted; negative raw age is clamped to zero
and logged with the existing clock-skew diagnostic. A timestamp one
millisecond beyond is rejected as malformed fields, counted and logged, and
does not contribute a delivery-health sample or suppression state. This is
defence in depth against unit mistakes and bad clocks while retaining
fail-open behaviour (autonomous decision 23).

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

## 6. Capacity and ramp: what the 400/400 change actually does

`docker-compose.yml:414-415` currently sets `JOIN_THRESHOLD=800` /
`LEAVE_THRESHOLD=900`. FR-013 locks both to 400.

`resolve_thresholds()` (`stream_monitoring_service.py:105`) rejects only
`leave < join`, so 400/400 is accepted. But `compute_desired_set()`
(`:136`) then computes `(previous | top_join) & top_leave` with
`join == leave`, which is a **zero-width hysteresis band**: the retained band
that keeps a boundary channel from leaving and rejoining once per poll no
longer exists. The module's own comment states the cost — "thrashing the
desired set once per poll and destroying Flink's baseline".

This is a real, accepted consequence of the locked decision, not a defect
introduced by this plan (D10). It is bounded: churn affects only channels
oscillating across rank 400, each churn costs two subscription creates/deletes
and one broadcaster's warm-up, and `DETECTION_MIN_BASELINE_FRACTION=0.8` means a
churned channel simply produces no detections until it has watched 240 s again.
The plan adds an observable churn signal so the cost is measurable rather than
assumed, and records a follow-up option (a narrower join threshold inside the
same 400 ceiling) that would need a spec change.

**"Accepted" now has a number attached to it.** NFR-007 and SC-011 turn the
qualitative acceptance into a release gate: desired-set entries plus departures
attributable to the zero-width band, averaged per poll across a **24-hour
deployed observation**, must not exceed **2% of the 400-channel ceiling — 8
membership changes per poll**. That bound is a disposition, not a hope:

- Under the bound, the zero-width band ships as locked and gating may be
  enabled.
- Over the bound, enabling gating is **blocked**, and the resolution is a
  specification change to a narrower join threshold inside the firm 400 ceiling.
  It is explicitly *not* resolved by a hidden code workaround — that is the
  option D10 already rejected, and measuring the cost was the entire reason for
  accepting the locked value in the first place.

The measurement is `desired_set_churn_total` over 24 hours at 400/400 on the
deployed system (E2/B4). It cannot be produced offline: offline tests can pin
the accounting — that the counter increments by entered plus departed channels —
but the rate itself is a property of real ranking movement.

Ordering constraint, from the roadmap risk register and repeated here because
it is the single most important sequencing rule in the feature: **the pool's
two-slot capacity behaviour must be proven by deterministic tests before the
ramp configuration changes.** Lowering the ramp first would hide a pool defect
behind a smaller set; raising per-channel subscriptions first without the ramp
change would ask for 1,440–1,600 subscriptions against a 900 ceiling.

---

## 7. Decisions

| ID | Decision | Alternatives rejected |
|---|---|---|
| **D1** | The two-slot model lives **inside `EventSubPoolTransport`**: one `_Slot` per (channel, type), a channel-level coverage view, and a `list()` that yields a channel only when both types are enabled. `reconciler.py` stays channel-keyed and is not restructured. | Subscription-keyed reconciler diff (touches refusal cache, ordering, batching, metrics, and every reconciler test for no behavioural gain). A second transport instance (doubles sockets; breaks the 3-connection limit). |
| **D2** | A 403-class refusal on the **auxiliary** type while chat is live is recorded in the pool as auxiliary-refused, does not propagate `SubscriptionRefusedError` to the reconciler, and leaves the channel chat-covered and visibly degraded. The hold-off is **bounded**: repeated notification creates are suppressed for `AUXILIARY_REFUSAL_RETRY_SECONDS` = 3600 s, after which the channel is repairable again; a websocket reconnect or connection retirement forces re-eligibility immediately; successful creation or adoption clears the state (superseded the original permanent form — see autonomous decision 17). | Propagating the refusal (marks the whole channel refused for 7 days — kills chat for that channel). Retrying every pass forever (unbounded noise and create budget at 400 channels). Never retrying until restart (a transient refusal becomes a permanent coverage hole and quietly violates FR-001). |
| **D3** | Suppression reaches Flink as a **dedicated `suppression-events` Kafka topic**, keyed by `broadcaster_id`, consumed by a second `KafkaSource` connected to the keyed chat stream through a `KeyedCoProcessFunction`. | Broadcast state (fan-out to all subtasks and a non-keyed state model for per-channel data). Sentinel records inside `chat-messages` (breaks the frozen chat schema contract and the `CommandFilter`/mapping path). Postgres or Redis lookup from the operator (per-decision I/O on the hot path; violates "Kafka for all inter-service messaging"). |
| **D4** | The suppression source uses `for_bounded_out_of_orderness(WATERMARK_OUT_OF_ORDERNESS_SECONDS)` on `occurred_at_ms`, `with_idleness(SUPPRESSION_IDLENESS_SECONDS = 5)`, `KafkaOffsetsInitializer.latest()`, and 4 partitions matching `FLINK_PARALLELISM`. | `no_watermarks()` (stalls the two-input minimum and freezes all detection). Longer idleness than the chat stream's 10 s (makes suppression the binding minimum). `earliest()` offsets (replays old timestamps and pins the operator watermark in the past). |
| **D5** | **Clarified by autonomous decision 22:** the gate compares the decision's peak second (`spike.detected_at_seconds`) with the notice-bounded half-open interval `suppress_from_ms <= peak_ms < suppress_until_ms`, not merely with the deadline and never with the report second. | Deadline-only gating (incorrectly suppresses a pre-notice peak whose hold reports later). Report-time gating (a burst that peaks inside the interval but reports after `hold_cap_seconds` escapes). Either-of-the-two (again suppresses a pre-notice peak). |
| **D6** | Gating is an **output-only filter** at the end of `on_timer`. Every state write — buckets, expiry, hold, chain timer, **and `last_fire_second`** — happens exactly as it does today; interval membership changes only the final yield and required suppression signals. | Gating inside `evaluate()` (changes hold/`emit` trajectories; breaks SC-004 and the pure module's test suite). Skipping the `last_fire_second` update (diverges from the ungated run for 30 s after every suppressed decision; makes SC-004's "identical state" unprovable). |
| **D7** | Suppression **windows are applied at the consumer** from `SUPPRESSION_GIFT_WINDOW_SECONDS` / `SUPPRESSION_RAID_WINDOW_SECONDS`; the producer publishes only the raw notice (`notice_type`, `occurred_at_ms`). | Producer-computed `suppress_until` (bakes policy into the topic, makes retention replay wrong after a tuning change, and splits the window constants across two services). |
| **D8** | The contract is **versioned** (`schema_version`) and carries exactly the identity/notice/time fields the consumer needs; `viewer_count` is optional and diagnostic-only. | An unversioned payload (no safe way to add a field later against a live topic). Carrying the full notice payload (user content on an operational topic, no requirement). |
| **D9** | A notice with no trustworthy channel identity **or** no parseable `occurred_at_ms` is **not published**; it increments a malformed counter and logs, per FR-017. | Publishing with `occurred_at_ms: null` (the consumer would have to invent a time — a fabricated deadline). Substituting the ingest clock (a guessed deadline wearing a trustworthy field name). |
| **D10** | Ship 400/400 exactly as locked, add a desired-set churn signal so the zero-width band's cost is measured, and record the narrower-join option as a follow-up requiring a spec change. | Silently deviating to 380/400 (contradicts FR-013 and SC-006). Special-casing hysteresis in code (hides policy from configuration). |
| **D11** | Emission gating has an operator kill switch, `SUPPRESSION_GATING_ENABLED`. The **code default in `SuppressionConfig` is `true`**, while `docker-compose.yml` **checks in `false`**, so a deploy is inert until an operator changes the compose value after E1-E3 and the 24-hour E2 churn observation pass. With it false the detector behaves exactly as pre-007 while both subscriptions and the topic stay in place. | Revert-only rollback (a code deploy to undo a detection-policy problem, at the moment clips are being lost). Checking in `true` (a deploy would start gating before any deployed evidence existed). |
| **D12** | Deployment is two-step: the ramp is reduced to 400/400 on the **current** single-subscription revision and allowed to converge, **then** the feature revision is deployed. That preliminary ramp-down is 400 × 1 = 400 of 900 and is therefore unconditionally capacity-safe; it is **not** gated by E1, which gates dual-coverage sign-off and enabling gating. Rollback runs the same logic in reverse: gating off, then unwind the transport **while thresholds stay at 400/400**, then wait for the notification subscriptions to disappear, and only then raise thresholds (autonomous decision 21). | One-step deploy of thresholds and transport together (the Redis-resident desired set makes the reconciler chase ~1,600 subscriptions before the first poll rewrites it — R9). Raising thresholds before unwinding the auxiliary subscriptions (at 2 subscriptions per channel, any threshold above 400 can cross the 900 ceiling — R11). |
| **D13** | **Clarified by autonomous decision 23:** delivery health is classified per trusted received record from `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`. After decode/field validation and before observation/state, the fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` rejects larger future timestamps as malformed fields; accepted negative raw age is clamped to zero and structured-logged as clock skew. Compare only the clamped value with `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` = 30 s — healthy at or below, lagging above — and observe it in `suppression_delivery_age_seconds`. Optional `received_at_ms` remains diagnostic-only, and no record means **idle/unknown** (§4.6). | A continuously refreshed per-channel delivery gauge from `on_timer` (must fabricate a value during legitimate silence). A heartbeat or synthetic record (a second protocol, out of scope). No future bound (lets unit mistakes create far-future suppression). Rejecting every future timestamp (turns harmless skew into avoidable false positives). |
| **D14** | The zero-width 400/400 band's cost gets a **numeric release disposition**: entries plus departures averaged per poll over 24 deployed hours must not exceed 8, i.e. 2% of the 400-channel ceiling (NFR-007, SC-011). Over the bound blocks enabling gating and is resolved by a spec change to a narrower join threshold inside the firm ceiling. | Leaving the churn signal without a threshold (unfalsifiable acceptance). Blocking on a tighter bound (boundary-rank movement is normal and a tighter bound would fail for reasons unrelated to this feature). Auto-adjusting the threshold in code when churn is high (the hidden-policy option D10 already rejected). |
| **D15** | Key/payload agreement is a **producer** invariant, asserted where the key is visible (T033). The job's sources use value-only deserialization, so `process_element2` never sees the record key; consumer routing and state use the payload `broadcaster_id`, and the consumer's duty is malformed-**payload** rejection. | Consumer-side key/payload comparison (asserts something the consumer structurally cannot observe). Switching to a key-and-value deserialization schema purely to enable that check (a change to the source shape for no behavioural gain, on the hot path). |
| **D16** | The suppression source's configuration — topic, `latest()` offsets, out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS`, expected partitions/parallelism, `delivery_lag_warn_seconds=30`, and `checked_in_gating_enabled=False` — plus fixed contract constant `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` live in pure `spike_detector.py`. `test_spike_detector.py` asserts all pure values and static compose checks assert both required wiring and the absence of a future-skew environment variable; `SuppressionConfig.from_env()` remains the runtime reader with gating defaulting to `true`, and `clip_detector_job.py` builds the real source from the pure settings (§4.7). | Literals inline in `clip_detector_job.py` (the only assertions would import PyFlink and skip when it is absent, making the highest-risk evidence conditional). A configurable future-skew allowance (unnecessary environment surface for a trust boundary). A new configuration module (new `FLINK_PYFILES` entry and compose mounts). |

---

## 8. Risks

| ID | Risk | Mitigation | Residual |
|---|---|---|---|
| **R1** | A two-type `list()` where one walk fails could look "complete" and let the reconciler drop live subscriptions. | The enumeration is complete only if **both** walks finish; either failure marks it incomplete, which already holds drops back (`reconciler.py:723`). Covered by a deterministic pool test. | A channel repaired one pass later than today at worst. |
| **R2** | Rendezvous imbalance puts one connection at 300 while the pair for a channel needs two slots. | `route()` places a pair only where two slots fit; if the channel's home connection has one slot, the pair splits across connections and both slots are tracked independently. | Slightly less locality; a socket death then touches two channels' partial coverage rather than one channel's whole coverage. Both states are already modelled. |
| **R3** | The suppression stream becomes the binding watermark minimum and stalls all detection. | D4 (real watermarks, shorter idleness than chat, `latest()` offsets, partitions == parallelism). Deterministic replay covers "suppression silent for hours, chat keeps firing". | A PyFlink-level idleness behaviour that cannot be reproduced offline; E3 is the deployed gate, and D11 is the immediate lever if it appears. |
| **R4** | A notification arrives after a decision, or after a spike peaked but before its hold reports. | No buffering or retraction (FR-018). A late notice affects later decisions only, and the lower bound ensures a pre-notice peak remains eligible even if reported afterward. Delivery lag is observable from trusted records via `suppression_delivery_age_seconds`. | False-positive clips caused by notices arriving after the relevant peak/decision remain possible and are measured by E4; genuine hype peaking before a notice is intentionally not counted as an accepted suppression false negative. |
| **R5** | The auxiliary subscription doubles subscription churn on every desired-set change, and at 400/400 the desired set has no hysteresis band (§6). | D10's churn signal plus D14's numeric bound: ≤ 8 entries-plus-departures per poll averaged over 24 deployed hours (NFR-007, SC-011). The ramp change lands only after pool tests pass; the 100-subscription headroom absorbs in-flight create/delete overlap. | Boundary-rank channels can re-warm their baseline repeatedly; visible as detection gaps for those channels only. If the measured rate exceeds the bound, gating stays off until a spec change narrows the join threshold. |
| **R6** | `channel.chat.notification` costs more than 0 against `max_total_cost = 10`. | E1 checks `total_cost` on the deployed system as early in dual-coverage convergence as possible; a non-zero cost blocks dual-coverage sign-off and the feature. The preliminary 400/400 ramp-down on the single-subscription revision is 400 × 1 and is **not** blocked by E1. | Feature-level: if non-zero, the two-subscription design is not viable on this token and the plan must stop rather than degrade. The preliminary ramp-down is independently safe and need not be reversed to run the check. |
| **R7** | An unknown or renamed `notice_type` silently stops triggering suppression. | Trigger set is an explicit allow-list; everything else is counted under an `other` bucket so a vanished category is visible as a distribution change rather than as silence. | Twitch renaming a trigger type is detected operationally, not automatically. |
| **R8** | The suppressed-emission metric/log is mistaken for a detection outage. | `anomalies_detected_total` still increments for a suppressed decision; the suppression is a separate counter, so "detected but not clipped" is computable and `AnomalyDetectionStalled` keeps its meaning. | Dashboards need the new series added; documented in OPERATIONS.md. |
| **R9** | Deploying the two-subscription transport while Redis still holds the old ~800-channel desired set asks for ~1,600 subscriptions against a 900 ceiling. `chat:desired` survives a restart, and the reconciler converges to it immediately while the poller only rewrites it up to 120 s later. | Two-step deployment: lower the ramp to 400/400 on the **current** single-subscription revision and let the desired set converge, **then** deploy the feature revision (D12; plan "Rollout and rollback"). | If the two steps are collapsed by mistake, the failure is loud (mass refusals, `pool is at its 3-connection limit`) and recovers by lowering the threshold; no data is lost. |
| **R10** | A long-idle suppression subtask re-enters the two-input watermark minimum when one isolated notice arrives, briefly holding the operator watermark and delaying per-second evaluation for the keys on that subtask. | Accepted with a conservative bound of `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` (§4.1.1, data-model I16), asserted offline in the replay harness's simplified model (T050) and measured deployed as part of E3, which must exercise **both** prolonged silence and an isolated notice after silence. | A short, bounded evaluation delay on a sparse-notice channel set — far smaller than the 120 s minimum suppression window. Sustained notice traffic never reaches the bound. |
| **R11** | During rollback, raising thresholds back toward the single-subscription ramp while `channel.chat.notification` subscriptions still exist would permit more than 800 subscriptions and can cross the 900 ceiling. | The rollback order is fixed and invariant-driven: gating off → unwind the transport **with thresholds still at 400/400** → wait until notification subscriptions are gone and total subscriptions ≈ desired channel count (~400) with stable coverage metrics → only then raise thresholds. Thresholds are never above 400 while any notification subscription remains (plan "Rollback order", autonomous decision 21). | If the order is inverted anyway, the failure is the same loud refusal mode as R9 and is recovered by lowering the threshold again. |
| **R12** | A corrupt unit or bad producer clock places `occurred_at_ms` far in the future, creating a long false suppression interval and a misleading healthy age-zero sample. | Enforce fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` after decode/field validation and before delivery observation/state; reject one millisecond beyond as malformed fields with a counter and structured log. Accept bounded skew with age zero and the existing skew diagnostic. | A bad timestamp within 30 seconds can shift the interval slightly; the allowance is intentionally bounded and E4 remains the deployed evidence for real timestamp/age behavior. |

---

## 9. Evidence deferred to the deployed machine

None of the following can be produced on this workstation, and none may be
claimed from unit tests, fixtures, or reasoning.

| ID | Evidence | Gate |
|---|---|---|
| **E1** | `Get EventSub Subscriptions` shows both types on live sessions with `total_cost` unchanged at 0 against `max_total_cost` 10. | Blocks dual-coverage sign-off and enabling gating, and blocks the feature if cost is non-zero. Does **not** block the preliminary 400/400 ramp-down on the single-subscription revision, which is 400 × 1 and safe on its own |
| **E2** | 400 channels converge to 800 subscriptions, no connection over 300, `eventsub_channel_coverage{state="complete"}` == desired count, the pool refuses the 401st channel without exceeding the ceiling, and `desired_set_churn_total` over a **24-hour** observation at 400/400 averages ≤ 8 entries-plus-departures per poll. | Blocks the ramp sign-off (SC-006) and, through the churn bound, blocks enabling gating (NFR-007, SC-011) |
| **E3** | With the suppression topic silent for at least one hour, chat detection continues and Flink source watermark lag on the chat input is unchanged from the pre-007 baseline; **and** a single isolated notice delivered after that silence holds the operator watermark for no longer than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before the source goes idle again. Both cases must be measured, not just the silence. | Blocks enabling gating in production (R3, R10) |
| **E4** | Twitch-occurrence-to-consumer age distribution from trusted records in `suppression_delivery_age_seconds` against `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` = 30 s, malformed-future rejection visibility, and a captured real gift-bomb/raid slice showing notice-bounded suppression firing on the intended bursts without suppressing pre-notice peaks. | Real-burst confirmation for SC-003, tuning/adequacy evidence for the window defaults under SC-005, and deployed evidence for delivery age and timestamp behavior (NFR-005, SC-010); deterministic boundary behavior remains local |
| **E5** | Rollback rehearsal: `SUPPRESSION_GATING_ENABLED=false` restores pre-007 emission behaviour with no other change, and the capacity-safe rollback order is executable — transport unwound with thresholds still at 400/400, thresholds raised only after the notification subscriptions are gone. | Blocks production enablement (D11, R11) |

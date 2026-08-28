# Research: EventSub Ingestion with a Parallel Reconciler

**Date**: 2026-08-27. Phase 0 measured 2026-08-28. Numbers are measured unless
the text says "projected". The measured spike data lives in
`specs/003-detector-scale-fanout/research.md` §1. This document does not repeat
it; it records the decisions taken from it.

## Phase 0 — measurement gate (COMPLETE, 2026-08-28)

Two decisions (D3, D4) and one risk (R1) rested on comparisons nobody had made.
Phase 0 made them with throwaway scripts running both transports live against
the top ~500 channels (auth user `48754970`, a separate `user:read:chat` token
that did not touch the running IRC production token). **All four passed. The
T006 gate is met — Phase 2 is unblocked.**

| Task | Result | Feeds |
|---|---|---|
| **T001** | `metadata.message_timestamp − tmi_sent_ts` over 24,473 joined messages (15 channels, 12 min): min 0, **median +1 ms**, p95 1 ms, max 1 ms, mean +0.5 ms, **0 negative**. The envelope timestamp *is* the IRC send time — same Twitch-assigned instant, not "dispatch vs send". | D3, FR-009, **T006** |
| **T002** | EventSub delivery lag `local_recv − envelope_ts` over 59,405 messages (414 channels, 8 min): p50 163, p95 217, p99 257, p99.9 415, p99.99 1,255 ms. **Over 2,000 ms: 1 message = 0.0017%.** Over 1,000 ms: 0.039%. | D4, SC-005 |
| **T003** | 250 creates at concurrency 1 / 5 / 10 / 20 → **zero 429s at every level** (2.6 / 11.5 / 6.5 / 44 subs/s; POST p50 ~330–385 ms). The rate limit is not a concurrency ceiling. | D2 |
| **T003b** | 550 creates, concurrency 15, retry + 10 s backoff on 429: **first 429 after 364 successful creates** (t ≈ 14 s), burst budget ≈ 360–420, **time to 500 enabled = 40.6 s**, time to 550 = 53.2 s, 180 total 429s all retried through. A second cold ramp on an already-drained budget still converged (500 in 125 s, 796 429s). | D2, SC-001 |
| **T004** | 500-channel cold ramp, 12,555 events in the ramp window: **0 × `received event for unknown subscription`, loss 0.00%.** Per-channel first-60 s vs steady (5–6 min) rate ratio 1.08 — opening baseline not depressed. | R1, **FR-014** |

**T005**: this section + D2/D3/D4/R1 below are the Phase 0 commit.
**T006 GATE**: median dispatch-vs-receive offset = **1 ms**, far inside the 2 s
watermark → **PASS. Phase 2 unblocked.**
**FR-014**: ramp loss is 0.00% and the opening baseline is not depressed →
**the warm-up gate is NOT built. Skip T040.**

### Caveats carried forward

- T002 ran at **414 channels, not 500** — 86 creates hit 429 at concurrency 10
  before the harness added backoff. 414 ≈ the 394-channel spike; the lag
  distribution is the relevant output and it is clean. Re-confirm the tail at a
  full, stable 500 during Phase 5 (T044 / T038).
- T002's lag tail includes scheduling jitter from a single-process measurement
  consumer (one asyncio loop doing receive + bookkeeping). The real Kafka
  producer path is lighter, so 0.0017% over 2 s is an **upper bound**.
- T001's dedicated offset number (±1 ms) is not subject to that jitter — it is
  a difference of two Twitch-supplied timestamps, independent of receive time.
- twitchAPI 4.5.0 registers the notification callback synchronously with the
  create POST returning (`websocket.py` `_subscribe`), so the ramp race the 003
  spike saw does not reproduce here. If the library is upgraded, re-check T004.

## Phase 2 — T028a go/no-go gate for IRC removal (2026-08-28)

Phase 3 deletes IRC outright. R5 accepted that there is no intermediate
fallback — `git revert` of the branch is the fallback — so this gate is the
evidence taken before the thing we would fall back to is deleted.

EventSub replaced IRC in production at **16:43:40Z**. The soak ran to
**18:43:40Z** on the post-code-review build, sampling every minute.

| Condition | Status |
|---|---|
| **T006** event-time gate | **PASS** (Phase 0): median dispatch-vs-send offset 1 ms over 24,473 joined messages |
| **T002** delivery-lag tail under 2 s | **PASS at the operating point, NOT re-confirmed at 500.** See below |
| **≥ 2 h live traffic, `eventsub_subscription_count` stable** | **PASS**: 92 min at 19–21 channels, 91 samples, `subs == desired` on every one |
| **T021** schema test | **PASS**: green in `test_stream_monitoring.py`, and confirmed live (below) |

### What the soak measured

- **Subscription stability**: 91 one-minute samples, `eventsub_subscription_count`
  equal to `ZCARD chat:desired` in **all 91**. Deviation 0%, against the SC-004
  budget of ±1%. The count moved 19 → 20 → 21 with the desired set and never
  lagged it by more than one sample interval.
- **Failures**: `subscription_create_failures_total` stayed empty for the whole
  soak — no 429, no refusal, no error. `eventsub_refused_at` is still NULL for
  every row, so nothing was wrongly marked.
- **Reconcile cost**: 1,383 passes; 1,017 of the first 1,020 under 0.5 s, the
  rest under 1 s apart from the 3.4 s cold start. Mean 113 ms.
- **Throughput**: 231,095 messages into `chat-messages` over the soak,
  2,465/min average (2,468–2,756 across the three 30-minute marks).
- **Resources**: 50.8 MiB RSS, 20 file descriptors, 17 threads, flat. No leak
  signature from the retire path.

### The reconnect path was exercised for real

At **16:52:42Z** the socket missed its keepalive and the library reconnected,
re-subscribing all 18 channels with **new subscription ids**. This is the path
the post-review fixes address (`_live_subscription_ids`, `_forget_unrecognised`),
and it is the one no unit test can fully stand in for.

Checked directly against Twitch afterwards by walking the subscription pages:
**20 enabled, all on one session — exactly matching `eventsub_subscription_count`
20 and `eventsub_connection_occupancy{connection="0"} 20`.** Nothing leaked,
nothing duplicated, and the reconciler kept tracking the desired set across it
(18 → 19 → 20).

Incidental: `get_eventsub_subscriptions().total` reported 20 correctly here.
That does not soften D6 — the spike caught it reporting 300 while the pages held
396, and 20 subscriptions is far too few to reproduce that. Page counting stays.

### Detection is unchanged on EventSub data (FR-008, D3 in production)

The strongest available evidence that the schema and the event-time semantics
survived the cutover is that the Flink job never noticed it:

| | Clips | Rate | Mean intensity |
|---|---|---|---|
| 3 h of IRC before cutover | 137 | ~45.7/h | 5.63 |
| First hour on EventSub | 40 | ~40/h | 5.84 |

Anomalies fire with sane event time (`peaked at … 5s ago`) across 17
broadcasters. This is a live version of the comparison T039 plans to make by
replay, and it is a stronger one: the same job, the same tuning, only the
transport changed.

### R1 needs one qualification

One `received event for unknown subscription` was logged at 16:52:48Z, inside
the resubscribe window after the reconnect. Phase 0 T004 measured **0** of these
across a 500-channel cold ramp (12,555 events) and recorded ramp loss as 0.00%.

That conclusion is not contradicted, but "0.00%" should be read as "below the
resolution of that measurement", not "impossible". The race is real and narrow:
Twitch activates a subscription server-side before the create POST's response
reaches us and the callback is registered, so an event arriving in that window
has nowhere to go. The cost is one chat message, which is immaterial against a
baseline built over five minutes. **R1 stays closed; no warm-up gate.**

### What is NOT met: the 500-channel tail

`T002` measured the delivery-lag tail at **414** channels, not 500 (Phase 0
caveat, carried above). Production runs at `JOIN_THRESHOLD` 15 /
`LEAVE_THRESHOLD` 30 — 19 to 21 channels — so this soak re-confirmed the tail at
**~20 channels** (p50 151 / p95 205 / max 288 ms, sampled over 249 messages
against a live pool), not at 500.

Raising production to 500 is a capacity change, and `research.md` Out of Scope
already records why that bites: a 500-channel set detects far more anomalies than
the clip budget can act on. **The operator's decision (2026-08-28) is to leave
production at 15/30 and keep the 414→500 re-check where `tasks.md` already puts
it — Phase 5, T038 and T044.**

### Verdict

**Three of the four conditions are met outright. The fourth is met at the
operating point and deferred at 500 by decision, not by oversight.**

For Phase 3 — deleting IRC — the risk R5 names is "EventSub misbehaves in
production". At the scale production actually runs, that risk is now evidenced
against: two hours of exact subscription tracking, a real reconnect survived
with Twitch and the service in agreement, zero failures, and unchanged detection
output. **Phase 3 is unblocked at the current operating point.** It does not
carry a claim about 500 channels; that claim is Phase 5's to make, and the
414→500 gap is the one thing standing between this gate and an unconditional
pass.

## Decisions

### D1 — Transport: websocket, not webhook

- **Decision**: EventSub over websockets (`EventSubWebsocket`).
- **Rationale**: no public ingress, no TLS certificate, no challenge
  handshake. The library handles reconnect. The spike ran 394 subscriptions
  across 2 sockets with no broadcaster consent and `total_cost` 0.
- **Alternatives considered**: webhook transport has a higher ceiling and
  server-side persistence across restarts, but needs a public HTTPS endpoint
  and a challenge handshake. Revisit only if the connection pool becomes
  unwieldy — that is far past 500 channels (about 7 sockets at 2000). Kept in
  spec Out of Scope as the fallback.

### D2 — Reconciler concurrency: 10, with mandatory 429 backoff-and-retry

- **Decision**: bounded concurrency, default **10** (`RECONCILE_CONCURRENCY`,
  env-configurable). On a 429, back off (fixed ~10 s, or honour the
  `Ratelimit-Reset` header if the library exposes it) and **retry the failed
  channels** — never drop them. The backoff/retry loop is load-bearing, not a
  safety net.
- **Rationale (measured, T003 / T003b, 2026-08-28)**: the limit is a
  **per-token request budget**, not a concurrency ceiling. 250 creates drew
  **zero 429s at concurrency 1, 5, 10 and 20**. A larger burst hits the budget:
  the first 429 landed after **364 successful creates** (~14 s at concurrency
  15). Burst budget ≈ 360–420 creates. So concurrency can be anywhere in
  10–20 with no throttling risk *within* a burst; what matters past ~400
  channels is the retry loop. With concurrency 15 + 10 s backoff, a cold start
  reached **500 subscriptions in 40.6 s** and 550 in 53.2 s — inside the SC-001
  60 s target and well under the 120 s ceiling. Even a ramp starting on an
  already-drained budget converged (500 in 125 s, 796 429s, all retried).
- **Why keep 10 rather than raise it**: 10 already clears SC-001 with margin;
  15–20 shave ~10 s off cold start but buy nothing operationally and give the
  reconciler more in-flight work to unwind on a mid-ramp restart. 10 is the
  conservative default; the env var is there if a future channel count needs it.
- **Alternatives considered**: unbounded fan-out (mirrors the `ClipCreator`
  thread bug this project is moving away from; and a burst >420 just 429s
  anyway); staying sequential (2.1/s — no better than IRC, 500 in ~240 s);
  a fixed requests-per-second limiter instead of concurrency + backoff (more
  code, and the measured burst-then-throttle shape is handled fine by
  concurrency + reactive backoff).

### D3 — `sent_at` source: envelope `metadata.message_timestamp` — GATE PASSED

- **Decision**: `sent_at` comes from the EventSub envelope's
  `metadata.message_timestamp`, converted to epoch milliseconds.
  `ChannelChatMessageData` has no timestamp field, so there is no other
  per-message option. **T001 gate met** (2026-08-28): median offset **+1 ms**,
  max 1 ms, 0 negative, over 24,473 messages joined by message UUID. Cutover
  proceeds.
- **What T001 actually showed**: `metadata.message_timestamp` and IRC
  `tmi-sent-ts` are **the same value** to the millisecond — not "dispatch time
  vs send time" as feared, but the one Twitch-assigned instant carried on both
  transports. There is no offset to correct and no calibration constant to
  maintain. `SentAtTimestampAssigner` sees the identical event-time input it
  sees today; the corpus-derived tuning stays valid.
- **Rationale**: `SentAtTimestampAssigner` drives Flink's event time from
  `sent_at`. A silent shift in that quantity would move every bucket boundary
  and invalidate the corpus-derived tuning without failing loudly.
- **Alternatives considered**: subtract a measured static offset (adds a
  calibration constant that drifts if Twitch changes dispatch behaviour);
  use the ingestion `timestamp` as event time (abandons Twitch's clock, which
  Plan 06 Phase 2 deliberately adopted); block the feature and pursue webhook
  (only if T001 shows a material, uncorrectable offset).
- **Clarified 2026-08-27**: this is the chosen rule (spec Clarifications).
- **Offset direction (pre-measurement reasoning, now moot)**: dispatch time was
  expected to be at or after send time, giving a one-signed offset. T001 showed
  the two timestamps are identical, so there is no offset in either direction.
  The "wide or unstable spread" failure signal did not appear: the spread is
  0–1 ms.

### D4 — Watermark tolerance: 2 s

- **Decision**: `WATERMARK_OUT_OF_ORDERNESS_SECONDS` moves from 1 to 2, in
  `services/flink-job/spike_detector.py`. `clip_detector_job.py` and
  `tools/replay.py` read the same constant, so all three move together.
- **Rationale (T002, 2026-08-28)**: EventSub delivery lag over 59,405 messages
  at 414 channels held p50 163 / p95 217 / p99 257 ms — flat against the
  394-channel spike (154 / 220). The tail did **not** keep growing: p99.99 was
  1,255 ms and exactly **one message (0.0017%) exceeded 2,000 ms** (a lone
  4.4 s outlier, most likely a GC pause in the measurement consumer). A 2 s
  tolerance therefore drops ~0.0017% of records — ~60× under the SC-005 0.1%
  budget. The cost is ~1 s added to the peak-to-clip-request delay floor.
- **Alternatives considered**: keep 1 s and accept a measured drop rate
  (the operator chose the watermark move instead — spec Clarifications); at 1 s
  the T002 data shows ~0.039% would drop — still under budget but with no
  margin for the untested 414→500 gap. Raise to 5 s (the pre-2026-08-27 value,
  a round number with no measurement behind it — over-corrects and slows every
  detection).
- **Acceptance**: the residual late-drop rate at a stable 500 channels must be
  below 0.1% (SC-005), measured in Phase 5 from Flink's `numLateRecordsDropped`
  (T038). T002 confirms the tail has not grown past 2 s at 414; the 414→500
  re-check is a Phase 5 item.

### D5 — Refusal cache and `allows_clipping`: shared 7-day re-check

- **Decision**: add `eventsub_refused_at TIMESTAMPTZ` and
  `clipping_disabled_at TIMESTAMPTZ` to the `streamers` table. A channel with a
  non-null mark is skipped. A mark older than 7 days is retried once; success
  clears it, a fresh refusal resets the timestamp.
- **Rationale**: ~1.5% of channels refuse with `subscription missing proper
  authorization` (6 of 400). Retrying every cycle wastes POSTs; skipping
  forever leaves a channel dark after it fixes its settings. The existing
  `allows_clipping` skip has the same "forever" problem, so both get the same
  self-heal.
- **Alternatives considered**: in-memory set rebuilt each restart (not durable;
  a long-lived process never revisits); permanent skip like `allows_clipping`
  today (rejected by the operator — spec Clarifications); a shorter re-check
  interval such as daily (more self-healing, more wasted calls against
  permanently unauthorized channels).
- **Clarified 2026-08-27**: both flags, 7-day interval (spec Clarifications,
  FR-007 and FR-013).

### D6 — Connection pool routing: consistent hash

- **Decision**: route a channel to a connection by consistent hash of its
  broadcaster id, so it lands on the same connection across reconciles. Track
  per-connection occupancy against the 300 cap. Grow the pool when the desired
  set needs more than `connections * 300` slots.
- **Rationale**: on socket death only that connection's ~300 subscriptions need
  recreating, not a full reshuffle. `get_eventsub_subscriptions().total` is
  unreliable (reported 300 while pages yielded 396) — count pages, do not trust
  `total`.
- **Alternatives considered**: fill-first packing (a socket death forces a
  rebalance across the whole pool); fixed 2-socket pool (breaks silently above
  600 channels).

## Risks

| ID | Risk | Mitigation | Phase 0 task |
|---|---|---|---|
| R1 | Events dropped during the subscribe ramp — `received event for unknown subscription` | **CLOSED (T004, 2026-08-28)**: 0 dropped events across a 500-channel cold ramp (12,555 events in the window), opening baseline not depressed (first-60 s / steady ratio 1.08). twitchAPI 4.5.0 registers the callback synchronously with the create POST. No warm-up gate — T040 skipped. Re-check only if the library is upgraded | T004 ✓ |
| R2 | D3 shifts event time silently and corrupts detection | **CLOSED (T001, 2026-08-28)**: the two timestamps are identical (median offset +1 ms, 0 negative, over 24,473 messages). No event-time shift. T006 gate passed | T001 ✓ |
| R3 | Concurrency triggers 429s not seen sequentially | **Measured (T003/T003b)**: 429s are budget-driven, not concurrency-driven — none at concurrency ≤20 for 250 creates; first 429 after ~364 creates in a larger burst. Mitigation is the D2 backoff-and-retry loop, which converged a 500-channel cold start in 40.6 s and recovered even from a drained budget. Concurrency 10 default, configurable | T003 ✓ |
| R4 | A socket death drops up to 300 channels at once | Consistent-hash routing plus fast reconcile. Alert on a subscription-count drop (FR-012) | — |
| R5 | Removing IRC leaves no fallback if EventSub misbehaves in production | Deliberate. The operator accepted no intermediate compatibility. `git revert` of the branch is the fallback | — |

## Deployment and token notes

- **Token scope**: the production token carries `chat:read` and `clips:edit`
  only — checked on disk 2026-08-28, no `user:read:chat`. Phase 0 seeded a
  **separate** `secrets/phase0_tokens.json` (superset `chat:read` +
  `clips:edit` + `user:read:chat`) via `seed_twitch_tokens.py` device flow and
  never touched the production file; prod kept running on IRC throughout, token
  unaffected. Phase 2 T017 still needs to add `AuthScope.USER_READ_CHAT` to
  `REQUIRED_SCOPES` and re-seed the production token file.
- **New modules**: `reconciler.py` and `eventsub_pool.py` need a `COPY` line in
  `services/stream-monitoring/Dockerfile` and a bind-mount entry in
  `docker-compose.yml`. Neither is picked up automatically.
- **Lingering subscriptions**: after a socket closes, its subscriptions linger
  as `websocket_disconnected` and `DELETE` on them returns "not found". Twitch
  garbage-collects them. Restart reconciliation must tolerate stale entries and
  not treat a failed `DELETE` as an error.

## Out of scope (carried from spec, recorded so it is not re-litigated)

- `ClipCreator`'s unbounded thread spawn and the missing clip budget. Clip
  creation is capped per account, so a larger monitored set detects far more
  anomalies than can be acted on. That needs anomaly ranking against a scarce
  budget — a design change. Candidate for spec 005.
- The detector state redesign from spec 003, which measurement deflated.
- Kafka partition and Flink parallelism re-provisioning.

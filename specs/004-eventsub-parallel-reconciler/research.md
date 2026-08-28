# Research: EventSub Ingestion with a Parallel Reconciler

**Date**: 2026-08-27. Numbers are measured unless the text says "projected".
The measured spike data lives in `specs/003-detector-scale-fanout/research.md`
§1. This document does not repeat it; it records the decisions taken from it.

## Phase 0 — measurements that must exist before cutover

Two decisions (D3, D4) and one risk (R1) rest on comparisons nobody has made
yet. Getting them wrong corrupts detection with no error. Phase 0 makes them.

| Task | Question | Measured input so far |
|---|---|---|
| T001 | Does `sent_at` change meaning when its source moves from IRC `tmi-sent-ts` to the EventSub envelope's `metadata.message_timestamp`? Run both transports on the same channels; record the per-message difference | Envelope value is Twitch *dispatch* time; `tmi-sent-ts` is *send* time. Different quantities; expected to track closely, but "expected" is not data |
| T002 | Delivery-lag percentiles at 500 channels. Does the tail keep growing past 394? | 394-channel spike: p50 154 ms, p95 220 ms, max 1243 ms. Tail grew with fan-out (max 366 ms at 2 channels) |
| T003 | Safe subscription-creation concurrency. Where do 429s begin? | Sequential 2.1 subs/s, p50 421 ms per POST, zero 429s across 394 creations. Concurrency is unmeasured |
| T004 | Ramp-window loss: `received event for unknown subscription` count against total during a 500-channel ramp | The library logs this and drops the events. Rate unmeasured |

**Checkpoint**: D2, D3, D4 decided from data; the T004 result answers FR-014.

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

### D2 — Reconciler concurrency: start at 10, tune from T003

- **Decision**: bounded concurrency, initial value 10, configurable, back off
  on 429.
- **Rationale**: creation is latency-bound, not rate-limited — p50 421 ms per
  POST, zero 429s sequentially. So it parallelizes. 10 in flight projects to
  ~20 subs/s and 500 channels in ~25 s.
- **Alternatives considered**: unbounded fan-out (risks 429s never seen
  sequentially, and mirrors the `ClipCreator` thread bug this project is trying
  to move away from); staying sequential (2.1/s — no better than IRC). The
  projection is arithmetic, not data; T003 sets the real number.

### D3 — `sent_at` source: envelope `metadata.message_timestamp`, gated on T001

- **Decision**: `sent_at` comes from the EventSub envelope's
  `metadata.message_timestamp`, converted to epoch milliseconds.
  `ChannelChatMessageData` has no timestamp field, so there is no other
  per-message option. Cutover proceeds **only if** T001 shows the median
  dispatch-versus-receive offset is within the 2 s watermark tolerance (D4).
- **Rationale**: `SentAtTimestampAssigner` drives Flink's event time from
  `sent_at`. A silent shift in that quantity would move every bucket boundary
  and invalidate the corpus-derived tuning without failing loudly.
- **Alternatives considered**: subtract a measured static offset (adds a
  calibration constant that drifts if Twitch changes dispatch behaviour);
  use the ingestion `timestamp` as event time (abandons Twitch's clock, which
  Plan 06 Phase 2 deliberately adopted); block the feature and pursue webhook
  (only if T001 shows a material, uncorrectable offset).
- **Clarified 2026-08-27**: this is the chosen rule (spec Clarifications).
- **Offset direction**: dispatch time is always at or after send time, so the
  offset is one-signed. A small, steady positive offset shifts every event time
  later by the same amount — it delays detection slightly but does not distort
  the relative bucket structure the detector reads. T001 checks the median
  magnitude against the 2 s tolerance; a wide or unstable spread is the real
  failure signal, not a constant shift.

### D4 — Watermark tolerance: 2 s

- **Decision**: `WATERMARK_OUT_OF_ORDERNESS_SECONDS` moves from 1 to 2, in
  `services/flink-job/spike_detector.py`. `clip_detector_job.py` and
  `tools/replay.py` read the same constant, so all three move together.
- **Rationale**: the measured max delivery lag at 394 channels is 1243 ms, and
  the tail grows with fan-out. A 1 s tolerance already drops that tail. 2 s
  clears the measured max with margin. The cost is ~1 s added to the
  peak-to-clip-request delay floor.
- **Alternatives considered**: keep 1 s and accept a measured drop rate
  (the operator chose the watermark move instead — spec Clarifications);
  raise to 5 s (the pre-2026-08-27 value, a round number with no measurement
  behind it — over-corrects and slows every detection).
- **Acceptance**: after the move, the residual late-drop rate at 500 channels
  must be below 0.1% (SC-005). T002 confirms the tail has not grown past 2 s.

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
| R1 | Events dropped during the subscribe ramp — `received event for unknown subscription` | Measure the loss. Add a warm-up gate that withholds a channel's baseline until its subscription is settled, only if the loss distorts detection (FR-014) | T004 |
| R2 | D3 shifts event time silently and corrupts detection | Measure both timestamps side by side before cutover. Cutover is gated on the result (D3) | T001 |
| R3 | Concurrency triggers 429s not seen sequentially | Start at 10, back off on 429, keep it configurable (D2) | T003 |
| R4 | A socket death drops up to 300 channels at once | Consistent-hash routing plus fast reconcile. Alert on a subscription-count drop (FR-012) | — |
| R5 | Removing IRC leaves no fallback if EventSub misbehaves in production | Deliberate. The operator accepted no intermediate compatibility. `git revert` of the branch is the fallback | — |

## Deployment and token notes

- **Token scope**: the current token carries `chat:read` and `clips:edit`.
  EventSub needs `user:read:chat`. A superset token already exists from the
  spike. Re-seeding does **not** invalidate the running production token — that
  was verified during the spike with production running throughout.
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

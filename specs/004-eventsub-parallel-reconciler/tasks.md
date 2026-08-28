# Tasks: EventSub Ingestion with a Parallel Reconciler

**Input**: `spec.md`, `plan.md`, plus `specs/003-detector-scale-fanout/research.md` §1 for the measured spike data
**Prerequisites**: PR #41 (configurable thresholds) merged

**Path**: `services/stream-monitoring/`, venv at `.venv` (constitution).

---

## Phase 0: Measure before cutting over

**Purpose**: Two design decisions (D3, D4) rest on comparisons nobody has made
yet. Making them wrong corrupts detection silently.

- [ ] T001 **[D3, FR-009]** Run IRC and EventSub side by side on the same channels. For each message, record `tmi-sent-ts` and `metadata.message_timestamp`. Report the distribution of the difference. This decides whether `sent_at` can change source without shifting event time
- [ ] T002 **[D4]** From T001's EventSub stream, measure delivery-lag percentiles at the target channel count. The 394-channel spike gave p50 154ms / p95 220ms / max 1243ms; confirm at 500 and check whether the tail keeps growing
- [ ] T003 **[D2]** Measure subscription creation at concurrency 1, 5, 10, 20. Find where 429s begin. The 2.1/s sequential figure is measured; anything above it is projection
- [ ] T004 **[R1]** Quantify ramp loss: count `received event for unknown subscription` against total during a 500-channel ramp. Decides whether a warm-up gate is needed
- [ ] T005 Record all four in `research.md` in this feature directory

**Checkpoint**: D2, D3, D4 decided from data.

---

## Phase 1: The seam (transport-independent)

**Purpose**: Split poller from reconciler. This is the change that fixes the
cold-start problem, and it would help on IRC too.

- [ ] T006 Define the Redis desired-set schema — sorted set keyed by rank, so the reconciler can work highest-rank-first
- [ ] T007 [US2] **[FR-002]** Strip network fan-out from `poll_top_streams`: it computes the desired set, writes it, refreshes online keys, emits lifecycle events, and returns. No joins, no subscribes
- [ ] T008 [US2] Assert FR-003 with a test: poll duration must not scale with desired-set change size
- [ ] T009 **[FR-004]** Add `reconciler.py` — diff desired vs actual, bounded concurrency (T003's value), highest rank first
- [ ] T010 [US4] **[FR-005]** Make the reconciler converge from any starting state: adopt existing subscriptions, never duplicate
- [ ] T011 [US4] Handle drift — revoked subscriptions recreated, stale ones dropped
- [ ] T011a **[FR-011]** Assert hysteresis survives the rewrite: a channel entering top JOIN_THRESHOLD is subscribed, and is dropped only after it exits top LEAVE_THRESHOLD. This logic moves from `_manage_chat_connections` into the desired-set computation, so it is easy to lose in the move
- [ ] T012 Add metrics per FR-012: subscription count, reconcile duration, creation failures, per-connection occupancy

**Checkpoint**: The poller is fast and the reconciler owns all network work.

---

## Phase 2: EventSub transport

- [ ] T013 Add `user:read:chat` to the token scopes and re-seed. Verified during the spike: re-auth does NOT invalidate the existing token
- [ ] T014 **[FR-001, FR-006]** Add `eventsub_pool.py` — `EventSubWebsocket` pool, consistent-hash routing, per-connection occupancy against the measured **300** cap (D6)
- [ ] T015 [US3] **[FR-009]** Map `ChannelChatMessageEvent` to the existing Kafka schema. `sent_at` per T001's finding. Fragments, badges, and cheer need remapping — the shapes differ from IRC
- [ ] T016 [US3] Assert the published schema is unchanged from what the Flink job consumes (FR-008)
- [ ] T017 [US1] Wire the reconciler to the pool
- [ ] T018 [R4] Handle socket death: recreate only that connection's subscriptions
- [ ] T019 [US4] Tolerate lingering `websocket_disconnected` subscriptions — `DELETE` returns "not found" and Twitch garbage-collects them itself
- [ ] T020 [D5] **[FR-007]** Persist authorization refusals next to `allows_clipping`; skip them, with a re-check interval

## Phase 3: Remove IRC

**No dual-transport period — the operator does not need intermediate
compatibility.** Doing this properly is what keeps the service simple.

- [ ] T021 **[FR-001]** Delete `_manage_chat_connections`, `joined_channels`, `_on_chat_ready`, `_on_chat_message`, and the `Chat` client
- [ ] T022 Delete `DEAD_CHAT_CONFIRMATION_POLLS` and the dead-chat recovery path — it exists only for IRC's connection-oriented membership
- [ ] T023 Delete `patches/twitchapi_leave_room_timeout.py` and its Dockerfile wiring. It works around a PART-confirmation hang that EventSub cannot have
- [ ] T024 Remove `chat:read` from required scopes if nothing else needs it
- [ ] T025 Update `KNOWN_ISSUES.md` — several entries are IRC-specific and become moot

## Phase 4: Watermark and detection

- [ ] T026 **[D4, FR-010]** Set `WATERMARK_OUT_OF_ORDERNESS_SECONDS` from T002. Do not inherit the IRC value by default. Record the accepted late-drop rate (SC-005)
- [ ] T027 [US3] Confirm the detector behaves unchanged: replay a post-cutover capture and compare anomaly rates against the pre-cutover corpus. Not byte-identical — the input differs — but the rate and character must be consistent
- [ ] T028 [R1] Add a warm-up gate if T004 shows meaningful ramp loss

## Phase 5: Verify

- [ ] T029 [SC-001] Cold start to 500 channels under 60 s
- [ ] T030 [SC-002] Poll duration flat against change size
- [ ] T031 [SC-003] No `Bucket channel_join got rate limited` anywhere — the bucket is gone
- [ ] T032 [SC-004] 500 channels sustained, subscription count stable
- [ ] T033 [SC-006] Restart mid-ramp converges with no duplicates
- [ ] T034 Full test suite passes

## Phase 6: Review

- [ ] T035 `/speckit.analyze` — cross-artifact consistency
- [ ] T036 `/code-review high` — this is a large change touching event-time semantics and a live ingestion path
- [ ] T037 Address findings

---

## Dependencies

- **Phase 0 blocks Phase 2.** T001 (timestamp comparison) especially: cutting
  over before knowing whether event time shifts risks silent detection damage.
- Phase 1 is transport-independent and can start immediately.
- Phase 3 depends on Phase 2 working — delete IRC only once EventSub carries
  traffic.
- T026 depends on T002.

## Parallel opportunities

- Phase 0 measurement tasks are independent of each other.
- Phase 1 (the seam) and Phase 0 (measurement) can proceed together.

## Scope discipline

`spec.md` Out of Scope lists the `ClipCreator` thread-and-budget problem. It is
real, it will bite at 500 channels, and it is **not** part of this feature. A
larger monitored set produces more anomalies than Twitch will let you clip, so
the fix is ranking against a scarce budget — a design change worth its own
spec. Resist pulling it in here.

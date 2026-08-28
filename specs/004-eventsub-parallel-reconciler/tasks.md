---
description: "Task list for EventSub ingestion with a parallel reconciler"
---

# Tasks: EventSub Ingestion with a Parallel Reconciler

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`,
`contracts/chat-messages.schema.md`. The measured spike data is in
`specs/003-detector-scale-fanout/research.md` §1.

**Prerequisites**: PR #41 (configurable channel thresholds) is merged and on
this branch as of the 2026-08-27 merge of `origin/main`.

## Path Conventions

Service code: `services/stream-monitoring/`, venv at `.venv` (constitution).
Flink code: `services/flink-job/`. Schema: `infrastructure/postgres/init.sql`.

## Format

`- [ ] [TaskID] [P?] [Story?] Description with file path`

- **[P]**: can run in parallel — different files, no dependency on an open task
- **[US1..US4]**: the user story from `spec.md` the task serves

---

## Phase 0: Measurement gate ⚠️ BLOCKING

**Purpose**: decisions D3 and D4 and the FR-014 warm-up question rest on
comparisons nobody has made. Getting them wrong corrupts detection silently.
Phase 0 blocks Phase 2, not Phase 1.

- [x] T001 [US3] **[D3, FR-009]** DONE 2026-08-28 — envelope `metadata.message_timestamp` == IRC `tmi-sent-ts` to the ms (median +1 ms, 0 negative, 24,473 joined messages). No event-time shift. See `research.md` Phase 0 / D3
- [x] T002 [US3] **[D4]** DONE 2026-08-28 — delivery lag at 414 channels: p50 163 / p95 217 / p99 257 ms, one message (0.0017%) over 2 s. Tail did not grow vs the spike. 414→500 re-check carried to Phase 5 (T038/T044)
- [x] T003 **[D2]** DONE 2026-08-28 — zero 429s at concurrency 1/5/10/20 for 250 creates; 429s are a per-token burst budget (~360–420), not a concurrency ceiling. Concurrency-15 + backoff cold start = 500 subs in 40.6 s. See `research.md` D2
- [x] T004 [US3] **[R1, FR-014]** DONE 2026-08-28 — 0 `received event for unknown subscription`, 0.00% ramp loss, opening baseline not depressed. twitchAPI 4.5.0 registers the callback synchronously. **Warm-up gate NOT built — T040 skipped**
- [x] T005 DONE — `research.md` Phase 0 section + D2/D3/D4/R1/R2/R3 updated with measured numbers (commit `56de514`)
- [x] T006 **[GATE]** PASS 2026-08-28 — median offset 1 ms, far inside the 2 s watermark. **Phase 2 unblocked**

**Checkpoint**: DONE. D2, D3, D4 decided from data. FR-014 answered (no gate).
Open item for Phase 5: re-confirm the T002 lag tail at a stable 500 channels.

---

## Phase 1: The seam — transport-independent

**Purpose**: split the poller from the reconciler. This is the change that
fixes cold start, and it would help on IRC too. Can start immediately, in
parallel with Phase 0.

- [ ] T007 [US2] Define the Redis desired-set layout from `data-model.md`: sorted set `chat:desired` (member = login, score = rank), plus `chat:desired:generation`. Document it in a module docstring in `services/stream-monitoring/reconciler.py`
- [ ] T008 [US2] **[FR-002, FR-003]** In `services/stream-monitoring/stream_monitoring_service.py`, strip network fan-out from `poll_top_streams`: it ranks, writes `chat:desired` in one `ZADD` + rank trim, refreshes `streamer:online:*` keys, emits lifecycle events, and returns. No joins, no subscribes
- [ ] T009 [US2] **[FR-011]** Move the hysteresis band (`resolve_thresholds`, JOIN/LEAVE) into the desired-set computation: a login enters `chat:desired` when it reaches top `JOIN_THRESHOLD` and stays until it exits top `LEAVE_THRESHOLD`. Keep `resolve_thresholds` and its validation as-is
- [ ] T010 [P] [US2] Add a test in `services/stream-monitoring/test_stream_monitoring.py` asserting FR-003: `poll_top_streams` duration does not scale with desired-set change size (0-change vs 500-change poll within noise)
- [ ] T011 [P] [US2] Add a test asserting FR-011 survives the move: a login entering top `JOIN_THRESHOLD` appears in `chat:desired`; it is removed only after it exits top `LEAVE_THRESHOLD`; the 16–30 band is retained
- [ ] T012 [US1] **[FR-004, D2]** Add `services/stream-monitoring/reconciler.py`: read `chat:desired`, diff against the in-memory actual set, create/drop highest-rank-first, concurrency `RECONCILE_CONCURRENCY` (default 10). **On a 429: back off (~10 s, or honour `Ratelimit-Reset` if the library exposes it) and retry the failed channels — never drop them.** T003 showed 429s are budget-driven, not concurrency-driven, so the retry loop is load-bearing past ~400 channels, not a safety net. Runs as an `asyncio` task in the existing `stream_monitoring_service.py` process, launched from `start()`, logging through the shared `jsonlogger` — not a separate container
- [ ] T013 [US4] **[FR-005]** Make the reconciler converge from any starting state: on start-up rebuild the actual set from `get_eventsub_subscriptions()` by **counting pages, not reading `total`** (research trap), and adopt existing subscriptions rather than duplicating
- [ ] T014 [US4] Handle drift: a subscription that is `revoked` or absent but still wanted is recreated on the next reconcile; one no longer wanted is dropped. A desired-set change mid-reconcile is picked up via `chat:desired:generation`
- [ ] T014a [P] [US4] **[FR-004, FR-005]** Reconciler unit tests in `services/stream-monitoring/test_stream_monitoring.py`: diff produces the right create/drop set; an already-subscribed channel is adopted, never re-created; a revoked subscription is re-created; a channel that leaves the desired set mid-reconcile is not created
- [ ] T014b [US4] **[NFR-003]** Start-up adoption tolerates a partial enumeration: if `get_eventsub_subscriptions` pagination raises mid-list, the reconciler keeps what it saw and does not treat unseen subscriptions as absent. Add a test
- [ ] T014c [US4] **[NFR-002]** A Redis or Postgres error inside a reconcile pass is logged, the affected work is skipped, and the loop retries next pass — no crash, no dropped live subscription. Add a test
- [ ] T014d [P] [US1] **[NFR-001]** Assert the reconciler holds no per-channel task or thread: at 500 desired channels the task/thread count is bounded by the concurrency limit plus a small constant
- [ ] T015 [US1] **[FR-012]** Add Prometheus metrics: `eventsub_subscription_count`, `reconcile_duration_seconds`, `subscription_create_failures_total{reason}`, `eventsub_connection_occupancy{connection}`
- [ ] T016 [US2] **[FR-012]** Expose reconciler liveness so a stalled or dead reconciler is visible while polls keep succeeding (US2 acceptance scenario 2) — a `reconcile_last_success_timestamp` gauge

**Checkpoint**: the poller is fast; the reconciler owns all network work. Still
runs against a stub transport until Phase 2.

---

## Phase 2: EventSub transport

**Depends on Phase 0 (T006 gate) and Phase 1.**

- [ ] T017 **[FR-001]** Add `user:read:chat` to `services/stream-monitoring/token_manager.py` scopes and re-seed via `seed_twitch_tokens.py`. Re-auth does not invalidate the running token (verified in the spike)
- [ ] T018 [US1] **[FR-006, D6]** Add `services/stream-monitoring/eventsub_pool.py`: an `EventSubWebsocket` pool with consistent-hash routing by `broadcaster_id`, per-connection occupancy against the measured **300** cap, and pool growth when `desired_count > connections * 300`
- [ ] T019 [US1] Handle the per-connection `enabled` count reported wrong by the library: track occupancy locally from create/drop, do not re-read it each cycle
- [ ] T019a [P] [US1] **[FR-006]** `eventsub_pool.py` unit tests: consistent-hash routing puts a `broadcaster_id` on the same connection across reconciles; occupancy never exceeds 300; the pool grows a connection when `desired_count > connections * 300`
- [ ] T020 [US3] **[FR-009]** Map `ChannelChatMessageEvent` to the `chat-messages` schema per `contracts/chat-messages.schema.md`. Convert the RFC 3339 `metadata.message_timestamp` to epoch ms for `sent_at`. Remap `badges`, `is_subscriber`, `is_mod` from `event.badges`; keep `emotes` `{}`
- [ ] T021 [P] [US3] **[FR-008]** Add a test asserting a mapped EventSub message and a mapped IRC message produce the same JSON keys and the same types for `sent_at` (int or null, never str), `broadcaster_id`, and `text`
- [ ] T022 [US1] Wire the reconciler (T012) to the pool (T018): create = subscribe on the routed connection; drop = unsubscribe; publish `on_message` to Kafka `chat-messages` via the existing producer path
- [ ] T023 [US4] **[R4]** Handle socket death: recreate only that connection's subscriptions, driven by the next reconcile; alert path is the `eventsub_subscription_count` drop (T015)
- [ ] T024 [US4] Tolerate lingering `websocket_disconnected` subscriptions: a `DELETE` returning "not found" is logged at debug and treated as success, not an error
- [ ] T025 [US3] **[D5, FR-007]** Add `eventsub_refused_at TIMESTAMPTZ` handling in `reconciler.py`: on `subscription missing proper authorization`, set it to `NOW()` and skip the channel. Skip any channel with a non-null `eventsub_refused_at` unless it is more than 7 days old, then retry once (success clears it to `NULL`)
- [ ] T025a [US3] **[FR-013]** In `services/flink-job/clip_detector_job.py`, when the job sets `allows_clipping = FALSE`, also set `clipping_disabled_at = NOW()` in the same `UPDATE`. On a later successful clip for a broadcaster whose flag is `FALSE`, set `allows_clipping = TRUE, clipping_disabled_at = NULL`
- [ ] T025b [US3] **[FR-013]** In `stream_monitoring_service.py` `_get_clipping_disabled_ids`, exclude rows whose `clipping_disabled_at < NOW() - INTERVAL '7 days'` from the disabled set, so a stale-disabled streamer re-enters ranking and gets one more clip attempt
- [ ] T025c [P] [US3] **[FR-007, FR-013]** Tests: a refusal newer than 7 days is skipped, one older is retried; a `clipping_disabled_at` older than 7 days re-enters ranking; a fresh failure resets the timestamp
- [ ] T026 **[migration]** Add both new columns to `infrastructure/postgres/init.sql` (`eventsub_refused_at`, `clipping_disabled_at`) and record the manual `ALTER TABLE` + backfill from `data-model.md` in `OPERATIONS.md` — `init.sql` runs only on a fresh database
- [ ] T027 **[deployment wiring]** Add `COPY reconciler.py` and `COPY eventsub_pool.py` to `services/stream-monitoring/Dockerfile`, and bind-mount both in `docker-compose.yml`. A new module is invisible without both (same class of gap as the Flink `-pyFiles` list)
- [ ] T028 **[deployment wiring]** Update the `user:read:chat` scope note in `docker-compose.yml` where the current scopes are documented
- [ ] T028a **[GATE for Phase 3]** Define and check the go/no-go for IRC removal: the T006 event-time gate passed, T002 confirmed the delivery-lag tail stays under 2 s at 500 channels, EventSub has carried live traffic for at least 2 h with `eventsub_subscription_count` stable at the desired-set size, and the T021 schema test passes. Record the check in `research.md`

**Checkpoint**: EventSub carries live traffic to Kafka and the T028a gate is met.

---

## Phase 3: Remove IRC

**No dual-transport period.** Delete IRC once EventSub carries traffic
(Phase 2 checkpoint met). This is what keeps the service simple.

- [ ] T029 **[FR-001]** Delete `_manage_chat_connections`, `joined_channels`, `_on_chat_ready`, `_on_chat_message`, the `Chat` client, and the `ChatEvent`/`Chat` imports from `stream_monitoring_service.py`
- [ ] T030 Delete `DEAD_CHAT_CONFIRMATION_POLLS`, `_consecutive_dead_chat_polls`, and the dead-chat recovery path — it exists only for IRC's connection-oriented membership
- [ ] T031 Delete `services/stream-monitoring/patches/twitchapi_leave_room_timeout.py` and its two Dockerfile lines (`COPY` + `RUN`). It works around a PART-confirmation hang EventSub cannot have
- [ ] T032 Remove `CHAT_READ` from the scope mapping in `stream_monitoring_service.py` and `chat:read` from required scopes if nothing else needs it (T017 added `user:read:chat`)
- [ ] T033 Delete the `GET_STREAMS_TIMEOUT_SECONDS` chat-hang comment block that references the deleted patch; keep the timeout itself
- [ ] T034 [P] Remove IRC-specific tests from `test_stream_monitoring.py` (join/leave, dead-connection recovery, hysteresis-via-`_manage_chat_connections`); the FR-011 behaviour is now covered by T011
- [ ] T035 Update `KNOWN_ISSUES.md` — IRC-specific entries (the JOIN bucket, the `leave_room` hang, dead-chat recovery) become moot. It is untracked in git; edit it in place

---

## Phase 4: Watermark and detection

- [ ] T036 [US3] **[D4, FR-010]** Set `WATERMARK_OUT_OF_ORDERNESS_SECONDS = 2` in `services/flink-job/spike_detector.py`. Update the comment to cite the T002 measurement, not KNOWN_ISSUES Issue 4. `clip_detector_job.py` and `tools/replay.py` read the same constant
- [ ] T037 [US3] Update the KNOWN_ISSUES Issue 4 "Post-deploy validation" delay arithmetic for the +1 s change
- [ ] T038 [US3] **[SC-005]** Measure the residual late-drop rate at 500 channels with the 2 s watermark, from Flink's late-records-dropped metric on the `chat-messages` source (`numLateRecordsDropped` / total). Record it in `research.md`. It must be below 0.1%
- [ ] T039 [US3] **[US3 Independent Test]** Replay a post-cutover capture through `tools/replay.py` and compare anomaly rate and character against the pre-cutover corpus. Not byte-identical — the input differs — but the rate and shape must be consistent
- [x] T040 [US3] **[R1, FR-014]** SKIPPED 2026-08-28 — T004 measured 0.00% ramp loss and no opening-baseline depression. No warm-up gate is built. Re-open only if twitchAPI is upgraded past 4.5.0 (the synchronous-callback behaviour is version-specific — `research.md` R1)

---

## Phase 5: Verify

- [ ] T041 [US1] **[SC-001]** Cold start to 500 channels: measure time to full coverage. Target under 60 s; fail over 120 s
- [ ] T042 [US2] **[SC-002]** Poll duration flat against desired-set change size (re-run T010 at 500 channels live)
- [ ] T043 **[SC-003]** No `Bucket channel_join got rate limited` anywhere in the logs — the IRC bucket is gone
- [ ] T044 [US1] **[SC-004]** 500 channels sustained for 30+ minutes: `eventsub_subscription_count` within ±1% of the desired-set size, every delta attributable to a logged refusal, a reconnect, or a desired-set change
- [ ] T044a **[SC-007]** Confirm every FR-012 metric (`eventsub_subscription_count`, `reconcile_duration_seconds`, `subscription_create_failures_total`, `eventsub_connection_occupancy`, `reconcile_last_success_timestamp`) is present on `/metrics` and populated while the reconciler runs
- [ ] T045 [US4] **[SC-006]** Kill the service mid-ramp, restart, confirm convergence with no duplicate subscriptions
- [ ] T046 [US3] Confirm `sent_at` in live Kafka messages is epoch ms and within the 2 s watermark of ingestion `timestamp`
- [ ] T047 Run the full `test_stream_monitoring.py` suite; every test passes
- [ ] T048 Run `test_replay.py` and `test_spike_detector.py` unmodified after the T036 constant change; adjust only tests that assert the old `1` value, and only to `2`

---

## Phase 6: Review and handoff

- [ ] T049 Run `/speckit.analyze` — cross-artifact consistency across `spec.md`, `plan.md`, `tasks.md`. Read-only
- [ ] T050 Address any CRITICAL or HIGH findings from T049 before merging
- [ ] T050a Re-run `checklists/requirements.md` against the final spec; close CHK003, CHK013, CHK015, CHK030 or record why each stays open
- [ ] T051 Run `/code-review high` on the diff — targets what tests cannot see: event-time semantics, the live ingestion path, socket-death reconcile, token re-seed
- [ ] T052 Address code-review findings
- [ ] T053 Update `research.md` with final measured numbers, replacing every projection
- [ ] T054 Record in `spec.md` Out of Scope that the clip-budget-and-ranking feature (spec 005) is now the next ceiling, carrying `research.md` §1 and §3 forward
- [ ] T055 Update `OPERATIONS.md` and `QUICKSTART.md` for the transport change (no IRC, the reconciler process, the `user:read:chat` scope, the manual `ALTER TABLE`)

---

## Dependencies

- **Phase 0 blocks Phase 2.** T006 especially: cutting over before T001 knows
  whether event time shifts risks silent detection damage.
- **Phase 1 is transport-independent** and can run alongside Phase 0.
- **Phase 3 depends on the T028a gate** — delete IRC only once EventSub carries
  traffic and the go/no-go check passes.
- T036 depends on T002. T038 depends on T036. T040 depends on T004.
- T022 depends on T012 and T018. T025, T025a, T025b depend on T026 (columns must exist).
- T025b changes poller behaviour that T025a's Flink write feeds; land T025a first.
- T014b, T014c, T014d test the reconciler and depend on T012–T014.
- T028a depends on T006, T002, T021, T022.

## Parallel opportunities

- All four Phase 0 measurement tasks (T001–T004) are independent.
- Phase 1 and Phase 0 run together.
- Within Phase 1: T010, T011, T014a, T014d (tests) are `[P]` against the implementation tasks.
- Within Phase 2: T019a, T021, T025c are `[P]`.
- T034 is `[P]` in Phase 3.

## Scope discipline

`spec.md` Out of Scope lists `ClipCreator`'s unbounded threads and the missing
clip budget. It is real and it will bite at 500 channels — a larger monitored
set detects more anomalies than Twitch will let you clip. The fix is ranking
against a scarce budget, which is a design change and its own spec (005). Do not
pull it in here.

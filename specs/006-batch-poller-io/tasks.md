# Tasks: Batched Poller Datastore I/O

**Input**: Design documents from `specs\006-batch-poller-io\`
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts\poller-batch-contract.md`, `quickstart.md`
**Tests**: Mandatory. Each implementation increment starts with a failing automated test or an explicit external evidence task.
**Organization**: Tasks are grouped by user story. The three P1 stories use technical dependency order (US3, US2, US1) so no story depends on unfinished work in a later phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run concurrently because it changes a different file and has no dependency on incomplete work.
- **[Story]**: Required only in user-story phases.
- Every task names an exact repository file path; external commands additionally name the governing quickstart path.

## Phase 1: Setup - Redis Test Adapter Contract

**Purpose**: Make the in-memory Redis adapter accurately model the redis-py behavior required by every story.

- [ ] T001 Add failing adapter tests for one ordered `mget()` result containing values and `None` entries in `services\stream-monitoring\test_desired_set_store.py`
- [ ] T002 Add failing adapter tests for default transactional and explicit non-transactional pipelines, queued `setex`, and ordered per-command responses in `services\stream-monitoring\test_desired_set_store.py`
- [ ] T003 Add a failing adapter test proving `execute(raise_on_error=True)` applies successful queued commands, retains element-level `ResponseError` positions, and raises the first acknowledged error in `services\stream-monitoring\test_desired_set_store.py`
- [ ] T004 Add failing adapter tests that distinguish transport failure before command application from acknowledgement loss after application in `services\stream-monitoring\test_desired_set_store.py`
- [ ] T005 Implement ordered `FakeRedis.mget()` behavior without changing existing desired-set commands in `services\stream-monitoring\test_support.py`
- [ ] T006 Implement `pipeline(transaction=True|False)`, queued `setex`, and ordered per-command response returns in `services\stream-monitoring\test_support.py`
- [ ] T007 Implement deterministic element `ResponseError`, pre-application failure, and post-application acknowledgement-loss hooks with `raise_on_error=True` semantics in `services\stream-monitoring\test_support.py`

**Checkpoint**: Adapter tests model transactional desired publication and non-transactional online refresh without pretending either failure mode is all-or-nothing.

---

## Phase 2: Foundational - Dispatch and Side-Effect Instrumentation

**Purpose**: Count real client dispatch boundaries and capture prohibited side effects before production orchestration changes.

**Critical**: Complete this phase before any user-story implementation.

- [ ] T008 Add failing assertions that standalone Redis commands count once, each `Pipeline.execute()` counts once, and queued commands do not count as separate dispatches in `services\stream-monitoring\test_desired_set_store.py`
- [ ] T009 Implement phase-aware Redis dispatch recording for standalone commands and pipeline executions while retaining existing call compatibility in `services\stream-monitoring\test_support.py`
- [ ] T010 Add reusable Postgres cursor, connection, and pool proxies with `cursor.connection.encoding`, bytes-returning `mogrify()`, final SQL-byte/row recording, actual `cursor.execute()`/transaction/pool counts, and measured-interval reset in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T011 Add a poll side-effect recorder for lifecycle publications, desired publications, reconciler notifications, final outcomes, and ordered phase boundaries in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T012 Extend the poll test builder to accept explicit ranked `(login, broadcaster_id)` records and injected failures, install the T010 pool proxy, and prohibit mocks around the metadata batch, snapshot, and refresh dispatch paths in `services\stream-monitoring\test_stream_monitoring.py`

**Checkpoint**: Tests can detect helper-internal SQL paging, Redis dispatch multiplication, and every prohibited downstream signal.

---

## Phase 3: User Story 3 - Persist Streamer Metadata Atomically (Priority: P1)

**Goal**: Replace per-row metadata transactions with one final-occurrence-wins statement and one transaction completion while preserving whole-batch failure visibility.

**Independent Test**: Exercise empty, valid, duplicate, statement-failure, commit-failure, poison, recovery, and 900-row cases; the opt-in real-Postgres group proves one measured statement and reusable rollback.

### Tests for User Story 3

- [ ] T013 [US3] Add a failing empty-batch test asserting no pool acquisition, SQL, transaction completion, or metadata failure-streak change (FR-015) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T014 [US3] Add a failing duplicate-ID test using the unpatched real `execute_values()` helper and T010 cursor proxy to assert final-occurrence wins, emitted VALUES-row count, one actual execute, and `page_size=len(unique_rows)` (FR-003, FR-010) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T015 [US3] Add a failing successful-batch test asserting one `cursor.execute()`, one commit, boolean success, clean pool return, database-clock `NOW()`, and `EXCLUDED.last_seen_at` update semantics (FR-009) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T016 [US3] Add a failing statement/row-error test asserting whole-batch rollback, boolean failure, contextual input/unique counts, streak increment, clean pool handling, and no per-row fallback (FR-011) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T017 [US3] Add a failing commit-error/unknown-outcome test asserting rollback is attempted, the connection is returned reusable or explicitly discarded, and metadata success is never reported (FR-011, NFR-004) in `services\stream-monitoring\test_stream_monitoring.py`

### Implementation for User Story 3

- [ ] T018 [US3] Implement a metadata batch helper that deduplicates an ordered normalized record sequence into an insertion-ordered streamer-ID map whose final ranking occurrence supplies the login in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T019 [US3] Implement the non-empty metadata upsert with one `psycopg2.extras.execute_values()` call using outer `VALUES %s`, `template="(%s, %s, NOW())"`, and `page_size=len(rows)` in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T020 [US3] Implement one commit on success plus rollback, reusable/discardable pool return, bounded contextual logging, boolean failure, and no per-row retry on any metadata error in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T021 [US3] Add metadata consecutive-failure state and gauge transitions so failed non-empty batches increment, successful non-empty batches reset, and empty batches preserve the prior value (FR-011, FR-012) in `services\stream-monitoring\stream_monitoring_service.py`

### Real-Postgres Evidence for User Story 3

- [ ] T022 [US3] Extend the isolated-schema fixture with a production-call counting proxy whose measured counters reset after setup SQL and before the batch helper in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T023 [US3] Add an opt-in `StreamerMetadataBatchAgainstPostgres` test with 900 inputs proving one statement/commit, inserts, updates, login changes, duplicate-ID last occurrence, common advancing `last_seen_at`, and preservation of seeded `first_seen_at`, `allows_clipping`, `eventsub_refused_at`, and `clipping_disabled_at` values (FR-019, SC-006) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T024 [US3] Add an opt-in real-Postgres poison-row test proving zero rows from the failed batch commit, rollback completes, and the same pooled connection is immediately reusable in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T025 [US3] Add an opt-in real-Postgres next-poll-path test that fails one poll with poison input, corrects the next ranking, invokes `poll_top_streams()` again, and stores the complete reconstructed batch without per-row fallback in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T026 [US3] On the separate test machine, run the opt-in real-Postgres command and retain its one-statement, rollback, reuse, and retry evidence according to `specs\006-batch-poller-io\quickstart.md`

**Checkpoint**: Metadata behavior is independently correct and measurable before poll orchestration consumes it.

---

## Phase 4: User Story 2 - Preserve Ranking and Lifecycle Meaning (Priority: P1)

**Goal**: Reorder one poll around an immutable current-plus-departed snapshot and one acknowledged refresh while preserving desired-set, hysteresis, and lifecycle semantics.

**Independent Test**: Replay primary, duplicate, empty, failure, unknown-acknowledgement, and recovery scenarios and assert both required outputs and prohibited side effects.

### Membership and Snapshot Tests

- [ ] T027 [US2] Preserve the existing `TestPollWritesIntentOnly` golden assertions except the named FR-008 missing-ID correction, then add failing mixed-case, partial/stable/turnover, login-rank, and metadata-only ID-deduplication tests (FR-001, FR-010, SC-005) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T028 [US2] Add a failing phase-order test proving desired membership and departures are fixed before the first online-state dispatch (FR-002) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T029 [US2] Add a failing test for one `MGET` over the ordered unique current-ranking-plus-departed key union, including repeated current logins (FR-002, FR-003) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T030 [US2] Add a failing short/long `MGET` response test asserting protocol failure and absence of refresh, lifecycle, desired publication, notification, and success outcome in `services\stream-monitoring\test_stream_monitoring.py`

### Membership and Snapshot Implementation

- [ ] T031 [US2] Normalize ranked records once, preserve ranking/login semantics, compute desired membership and departures, then invoke the T018 metadata batch helper exactly once before online-state I/O in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T032 [US2] Build the ordered unique snapshot-key union, skip an empty union, issue one `MGET`, validate response length, and retain an immutable presence snapshot in `services\stream-monitoring\stream_monitoring_service.py`

### Refresh and Lifecycle Tests

- [ ] T033 [US2] Add a failing test asserting one `pipeline(transaction=False).execute(raise_on_error=True)` queues every current `SETEX` in ranking order with the existing TTL/ID and omits execution for no current records (FR-003, FR-005, FR-015) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T034 [US2] Add a failing repeated-login test proving one snapshot key, ranking-order refreshes with the final key value, and at most one online candidate using the best/first rank and that occurrence's broadcaster ID in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T035 [US2] Add failing entry-band, outside-band, and stable-channel tests proving emit-once, refresh-without-event, and no-event behavior from the pre-refresh snapshot (FR-006, FR-007) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T036 [US2] Add failing departed-present and departed-expired tests proving no event for present state and one offline event carrying the previous real broadcaster ID for absent state (FR-008) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T037 [US2] Add failing missing, non-numeric, and non-positive previous-ID tests proving only the corrupt offline event is suppressed, no placeholder ID is emitted, and integrity context is logged in `services\stream-monitoring\test_stream_monitoring.py`

### Refresh and Lifecycle Implementation

- [ ] T038 [US2] Derive online/offline candidates only from the immutable snapshot, deduplicate lifecycle evaluation by login, and preserve best-rank semantics in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T039 [US2] Queue ranking-order current-key refreshes on one non-transactional pipeline and withhold all lifecycle candidates until `execute(raise_on_error=True)` succeeds in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T040 [US2] Publish valid lifecycle candidates after refresh success and log/suppress only departures lacking a valid previous desired-set ID in `services\stream-monitoring\stream_monitoring_service.py`

### Failure, Recovery, and Completion Tests

- [ ] T041 [US2] Add a failing ranking timeout/error test for `ranking_failed` with no metadata, online-state, lifecycle, desired, notification, or success-shaped completion (contract ranking row) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T042 [US2] Add a failing previous-desired-read test for `desired_read_failed` with no metadata or state-dependent downstream work (contract desired-read row) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T043 [US2] Add a failing snapshot transport/protocol test for `online_snapshot_failed` with metadata allowed to remain committed but refresh/events/intent/notification prohibited in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T044 [US2] Add a failing pre-application refresh transport test for `online_refresh_failed` with no applied keys or downstream lifecycle/intent/notification signals in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T045 [US2] Add a failing refresh acknowledgement-loss test proving applied keys may remain visible while lifecycle, desired publication, notification, and poll success are still suppressed in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T046 [US2] Add a failing acknowledged element-level `ResponseError` test proving other commands may apply but the whole refresh phase fails and prohibited downstream signals stay absent in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T047 [US2] Add failing desired-publication rejection and unknown-acknowledgement tests proving prior lifecycle output is not claimed rolled back, notification/success are absent, and the next poll re-reads visible intent in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T048 [US2] Add a failing recovery test where online keys expire during a failed interval and the next healthy poll emits only the normal inside-band events while outside-band channels refresh silently in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T049 [US2] Add failing empty/empty and departures-only tests asserting metadata/snapshot/refresh omissions, required departures snapshot behavior, and atomic empty desired publication in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T050 [US2] Add a failing metadata-only error test proving healthy snapshot, refresh, lifecycle, desired publication, and reconciler notification continue with exactly the distinct `metadata_failed` outcome in `services\stream-monitoring\test_stream_monitoring.py`

### Failure and Completion Implementation

- [ ] T051 [US2] Implement the ranking-fetch and previous-desired-read fatal gates with `ranking_failed`/`desired_read_failed` selection and no prohibited downstream work in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T052 [US2] Implement snapshot and refresh fatal gates for transport, protocol-length, acknowledgement-loss, and element-error paths with `online_snapshot_failed`/`online_refresh_failed` selection in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T053 [US2] Publish desired intent only after acknowledged refresh, notify only after acknowledged desired publication, and carry metadata failure into exactly one `metadata_failed` or fatal/success outcome for the T084 completion path in `services\stream-monitoring\stream_monitoring_service.py`

**Checkpoint**: Golden lifecycle meaning, failure gates, and next-poll recovery are complete before scale claims are measured.

---

## Phase 5: User Story 1 - Poll on Schedule at Production Scale (Priority: P1)

**Goal**: Prove channel-count-independent datastore dispatches and provide reproducible production-equivalent duration, scheduler, steady-state, and cold-start evidence.

**Independent Test**: Local deterministic tests prove driver calculations and dispatch ceilings; execution against real remote stores and Twitch is explicitly deferred to the separate production-equivalent machine.

### Deterministic Fixture and Driver Foundation Tests

- [ ] T054 [US1] Add failing import-smoke and fixture tests for pytest package import plus deterministic A/B generation with disjoint IDs/logins, 100-row pagination, repeatability, and separate driver consumption in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T055 [US1] Add failing tests for direct-script and pytest-module driver loading, the validation JSONL envelope, required `run_id`/UTC timestamp, nearest-rank p95 item 19 of 20, bounded record kinds, and rejection of missing or non-isolated datastore targets in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T056 [P] [US1] Implement deterministic disjoint ranking fixtures, calibrated disabled-record injection, exact 500/900 eligible counts, and recorded test-only thresholds in `services\stream-monitoring\phase5\feature006_fixtures.py`
- [ ] T057 [P] [US1] Implement a CLI/import seam that supports both `python phase5\feature006_driver.py` and pytest module import, plus the JSONL writer, run IDs, isolated-target preflight, argument validation, and nearest-rank percentile helper in `services\stream-monitoring\phase5\feature006_driver.py`

### Dispatch-Count Gates

- [ ] T058 [US1] Add failing 50/500/900 stable-profile assertions using unmocked batch/snapshot/refresh paths and positive counts of exactly one metadata execute/commit, one `MGET`, and one refresh execute at actual dispatch boundaries (FR-003, FR-004, SC-003) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T059 [US1] Add failing 50/500/900 complete-turnover assertions using the unpatched real `execute_values()` helper to prove the same positive counts and expose hidden paging or per-channel Redis/Postgres calls in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T060 [US1] Add failing empty/empty and departures-only dispatch assertions for `0/0/0/1` and `0/1/0/1` metadata/snapshot/refresh/desired-publication counts (FR-015, FR-018) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T061 [US1] Implement the `operation-counts` command against the T057-verified isolated namespace with Redis/pipeline, SQL cursor, and transaction-completion proxies plus stable, turnover, empty, and departures-only records in `services\stream-monitoring\phase5\feature006_driver.py`

### Calibration and Poll-Profile Driver

- [ ] T062 [US1] Add failing calibration tests for at least 20 live page observations, nearest-rank p95 delay selection, exact eligible counts, raw/disabled/page counts, and ranking/non-ranking budgets in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T063 [US1] Implement live `get_streams(first=100)` page-interval calibration and fixture-delay/ranking-budget recording without substituting a zero-latency generator in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T064 [US1] Implement at least 20 separate harmless Redis and Postgres RTT samples, medians, and 40-110 ms acceptance-validity classification in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T065 [US1] Add failing profile tests for stable/turnover state preparation, one warm-up, exactly 20 completed measurements, whole-poll replacement after excluded failures, end-to-end timing boundaries, and zero overlap skips in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T066 [US1] Implement the stable `poll-profile` path with fixture A repetition, seeded desired intent, present current keys, phase durations, dispatch counts, and complete-poll timing in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T067 [US1] Implement complete-turnover A/B alternation, opposite previous intent, absent incoming/departed keys prepared outside timing, excluded-poll replacement, and 500/900 p95 gates in `services\stream-monitoring\phase5\feature006_driver.py`

### Scheduler, Steady-State, and Cold-Start Driver

- [ ] T068 [US1] Add failing driver tests for composed production-plus-recording pass callbacks, monotonic adjacent-gap calculations, post-convergence start, and APScheduler executed/error/missed/max-instance event classification in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T069 [US1] Implement reusable scheduled-poll start/end and APScheduler executed/error/missed/max-instance event recording without altering scheduler policy in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T070 [US1] Implement separate 500/900 `steady-state` runs that compose `active_stream_count.set`, record every `time.monotonic_ns()` pass completion for at least 30 minutes, and calculate 15/20-second maximum-gap gates in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T071 [US1] Add failing cold-start recorder tests for unchanged transport delegation, accepted creates, `RateLimitedError`, backoff intervals, accepted-window coverage growth, poll timelines, and zero-initial-subscription preconditions in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T072 [US1] Implement a recording transport proxy that delegates real list/create/delete behavior unchanged while timestamping accepted creates, rate limits, backoff windows, and coverage in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T073 [US1] Implement the isolated `cold-start` command with warm-process/pool gates, exactly 900 desired IDs, required real backoff, poll schedulability, accepted-window progress, and no new convergence deadline in `services\stream-monitoring\phase5\feature006_driver.py`
- [ ] T074 [US1] Add failing contract-shape tests for every calibration, poll-profile, reconciler-gap, and cold-start required evidence field in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T075 [US1] Add strict record builders that reject incomplete or invalid calibration, profile, gap, and cold-start evidence before writing JSONL in `services\stream-monitoring\phase5\feature006_driver.py`

### External Evidence for User Story 1

- [ ] T076 [US1] On the separate test machine, run 50/500/900 stable/turnover plus empty/departures `operation-counts` gates against isolated namespaces according to `specs\006-batch-poller-io\quickstart.md`
- [ ] T077 [US1] On the separate test machine, run live calibration and all four 500/900 stable/complete-turnover 20-poll profiles, retaining exact-count, latency, p95, phase, and overlap evidence according to `specs\006-batch-poller-io\quickstart.md`
- [ ] T078 [US1] On the separate test machine after convergence, run distinct 30-minute 500/900 steady-state sessions and retain direct pass-gap and scheduler-event evidence according to `specs\006-batch-poller-io\quickstart.md`
- [ ] T079 [US1] In the isolated validation Twitch environment on the separate test machine, run the 900-channel cold-subscription backoff session and retain poll-scheduling and accepted-window progress evidence according to `specs\006-batch-poller-io\quickstart.md`

**Checkpoint**: Local merge gates prove deterministic calculations and dispatch ceilings; real latency, datastore, 30-minute, and Twitch-backoff acceptance runs remain external-machine gates.

---

## Phase 6: User Story 4 - Ramp and Roll Back with Evidence (Priority: P2)

**Goal**: Add bounded production telemetry, freeze production configuration, and document deployment, evidence, later-ramp, and rollback as separate decisions.

**Independent Test**: Unit tests prove bounded metrics and exactly one outcome; source guards prove production remains 150/300/120 and validation modules stay outside the image/mounts; operational execution occurs separately.

### Observability Tests

- [ ] T080 [US4] Add failing tests that full success, metadata-only failure, each fatal phase, and unexpected failure increment exactly one bounded total poll outcome and never a success-shaped alternative (FR-017, NFR-004) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T081 [US4] Add failing tests for all six bounded phase names with only `success|failure|empty` outcomes, including empty metadata/snapshot/refresh reporting and no exception/login label cardinality in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T082 [US4] Add failing structured-log tests for one final outcome plus phase duration, ranked/desired/entered/left counts, failed-phase context, and metadata input/unique/streak context where applicable in `services\stream-monitoring\test_stream_monitoring.py`

### Observability Implementation

- [ ] T083 [US4] Define bounded total-duration and phase-duration Prometheus instruments while reusing the metadata-consecutive-failure gauge owned by T021 and leaving existing metrics unchanged in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T084 [US4] Instrument the sole final structured outcome and total-duration observation through one completion path, timed immediately before ranking retrieval through notification or the failing phase, for every return/exception in `services\stream-monitoring\stream_monitoring_service.py`
- [ ] T085 [US4] Instrument `ranking_fetch`, `metadata_persistence`, `online_snapshot`, `online_refresh`, `lifecycle_publication`, and `desired_set_publication` durations with bounded outcomes and required structured context in `services\stream-monitoring\stream_monitoring_service.py`

### Production Boundary and Operations

- [ ] T086 [US4] Add an automated source guard asserting production Compose remains `JOIN_THRESHOLD=150`, `LEAVE_THRESHOLD=300`, and `CLIPPING_DISABLED_FETCH_BUFFER=120` (FR-021, SC-009) in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T087 [US4] Add an automated source guard asserting `feature006_driver.py` and `feature006_fixtures.py` are absent from production Docker copies and Compose bind mounts in `services\stream-monitoring\test_stream_monitoring.py`
- [ ] T088 [P] [US4] Document unchanged-value deployment, `--force-recreate`, bounded poll/phase/streak evidence collection, and external validation prerequisites in `OPERATIONS.md`
- [ ] T089 [US4] Document that the later channel ramp is a separate decision and that rollback reverts only the feature revision and recreates `stream-monitoring` with no schema/dependency/threshold/Flink reversal in `OPERATIONS.md`
- [ ] T090 [US4] On the separate test machine, deploy at 150/300/120 and retain total/phase timing, failure-streak, overlap, pass-gap, EventSub occupancy, Kafka/Flink lag, and datastore RTT evidence according to `specs\006-batch-poller-io\quickstart.md`
- [ ] T091 [US4] On the separate test machine, rehearse or document the one-revision `stream-monitoring` rollback independently from any later ramp according to `specs\006-batch-poller-io\quickstart.md`
- [ ] T092 [US4] Run the focused poll-outcome, phase-duration, metadata-streak, Compose-value, and Docker-boundary selectors without starting infrastructure using `services\stream-monitoring\test_stream_monitoring.py`

**Checkpoint**: The code deploy and rollback at 150/300/120 are reviewable independently from external scale acceptance and any later production ramp.

---

## Phase 7: Polish and Cross-Cutting Completion

**Purpose**: Execute the final local gate, prove command readiness, and reject scope leakage before implementation review.

- [ ] T093 Exercise every `feature006_driver.py` command's argument/help path without contacting infrastructure and reconcile it with `specs\006-batch-poller-io\quickstart.md`
- [ ] T094 Run `python -m pytest -q test_stream_monitoring.py test_desired_set_store.py` from the existing virtual environment and resolve only feature-caused failures in `services\stream-monitoring\test_stream_monitoring.py` and `services\stream-monitoring\test_desired_set_store.py`
- [ ] T095 Audit the implementation diff and prove no changes to dependency pins, Python image, schemas, scheduler policy, EventSub policy/capacity, Flink, feature 005, Redis key layouts, or production thresholds using `services\stream-monitoring\requirements.txt`, `services\stream-monitoring\Dockerfile`, `docker-compose.yml`, and `infrastructure\postgres\init.sql`
- [ ] T096 Conduct the implementation completion review against every phase/failure/evidence row and record any externally deferred acceptance gate without weakening it in `specs\006-batch-poller-io\contracts\poller-batch-contract.md` and `specs\006-batch-poller-io\quickstart.md`

---

## Dependencies and Execution Order

### Phase Dependencies

- **Phase 1** has no dependencies.
- **Phase 2** depends on Phase 1 and blocks all user stories.
- **Phase 3 / US3 (P1)** depends on Phase 2 and establishes the metadata batch consumed by the poll.
- **Phase 4 / US2 (P1)** depends on Phase 3 and establishes behaviorally correct orchestration and failure gates.
- **Phase 5 / US1 (P1)** depends on Phases 3-4 because scale evidence is valid only for the complete batched poll.
- **Phase 6 / US4 (P2)** depends on the final poll outcomes from Phase 4 and the evidence commands from Phase 5.
- **Phase 7** depends on all implementation phases. External tasks T026 and T076-T079/T090-T091 execute only on the separate machine and do not authorize local infrastructure startup.

### Within Each User Story

- Write the listed failing tests before the implementation tasks in the same increment.
- Do not weaken an assertion to make a test pass; implement the contract boundary it measures.
- Count SQL and Redis at the actual dispatch boundary, never at helper entry.
- Complete each story checkpoint before relying on it in the next phase.

### User Story Dependency Graph

```text
Phase 1 adapter
    -> Phase 2 instrumentation
        -> US3 metadata atomicity
            -> US2 lifecycle-safe orchestration
                -> US1 scale/schedulability evidence
                    -> US4 observability/operations
                        -> final completion gate
```

## Genuine Parallel Opportunities

- After T054-T055 are committed, T056 (`feature006_fixtures.py`) and T057 (`feature006_driver.py`) can proceed concurrently because they create independent files with already-defined contracts.
- After the Phase 5 driver contract is final, T088 (`OPERATIONS.md`) can proceed concurrently with Phase 6 production-code/test instrumentation because it changes a separate file and consumes settled metric/command names.
- No work in `stream_monitoring_service.py` or `test_stream_monitoring.py` is marked parallel merely because it covers a different concept; those tasks share files and ordered state.

## Requirement Traceability

| Requirement | Concrete task coverage |
|---|---|
| FR-001 | T027, T031, T035-T040, T049 |
| FR-002 | T028-T032 |
| FR-003 | T014-T015, T019, T029, T033, T039, T058-T061 |
| FR-004 | T058-T061, T076 |
| FR-005 | T033, T039 |
| FR-006 | T034-T035, T038-T040 |
| FR-007 | T035, T038-T040 |
| FR-008 | T036-T037, T040 |
| FR-009 | T015, T018-T023 |
| FR-010 | T014, T018, T023, T027 |
| FR-011 | T016-T017, T020-T025 |
| FR-012 | T021, T050-T053, T080-T085 |
| FR-013 | T030, T041-T046, T051-T052 |
| FR-014 | T047, T053 |
| FR-015 | T013, T033, T049, T060 |
| FR-016 | T047, T053 |
| FR-017 | T068-T070, T080-T085, T090 |
| FR-018 | T058-T061, T076 |
| FR-019 | T022-T026 |
| FR-020 | T034-T048 |
| FR-021 | T086, T088-T091, T095 |
| FR-022 | T071-T073, T079 |
| NFR-001 | T054-T067, T077 |
| NFR-002 | T068-T070, T078 |
| NFR-003 | T031-T052, T069-T073, T095 |
| NFR-004 | T003-T004, T016-T017, T030, T041-T053, T080-T085 |
| NFR-005 | T086-T089, T095 |

## Success-Criteria Evidence

| Criterion | Evidence-producing tasks |
|---|---|
| SC-001 | T062-T067, T077 |
| SC-002 | T062-T067, T077 |
| SC-003 | T058-T061, T076 |
| SC-004 | T068-T070, T078 |
| SC-005 | T027-T040, T048-T049 |
| SC-006 | T022-T026 |
| SC-007 | T003-T004, T016-T017, T030, T041-T053, T080-T085 |
| SC-008 | T080-T085, T088, T090 |
| SC-009 | T086-T091, T095 |
| SC-010 | T071-T073, T079 |

## Contract Failure Matrix

| Failure row | Test tasks | Implementation tasks |
|---|---|---|
| Ranking fetch/filter | T041, T080-T082 | T051, T084-T085 |
| Previous desired-set read | T042, T080-T082 | T051, T084-T085 |
| Metadata statement/commit | T016-T017, T024-T025, T050, T080-T082 | T020-T021, T053, T084-T085 |
| Online snapshot read/protocol | T030, T043, T080-T082 | T032, T052, T084-T085 |
| Refresh transport before application | T004, T044, T080-T082 | T007, T039, T052, T084-T085 |
| Refresh acknowledgement lost after application | T004, T045, T080-T082 | T007, T039, T052, T084-T085 |
| Acknowledged element-level refresh error | T003, T046, T080-T082 | T007, T039, T052, T084-T085 |
| Missing/invalid previous ID | T037 | T040 |
| Desired publication rejected/unknown | T047, T080-T082 | T053, T084-T085 |
| Unexpected guarded failure | T080-T082 | T084-T085 |

## Quickstart Command Readiness

| Quickstart section | Tasks that create or validate the command surface |
|---|---|
| §1 Preconditions | T086-T089, T093, T095 |
| §2 Unit and behavioral validation | T001-T025, T027-T053, T058-T060, T080-T087, T092-T094 |
| §3 Real-Postgres validation | T022-T026 |
| §4 Dispatch-count gate | T058-T061, T076 |
| §5 Calibration | T062-T064, T077 |
| §6 Poll-duration profiles | T065-T067, T077 |
| §7 Thirty-minute steady state | T068-T070, T078 |
| §8 Cold-subscription backoff | T071-T075, T079 |
| §9 Failure and recovery | T003-T004, T016-T017, T030, T041-T052, T080-T085 |
| §10 Deployment and rollback | T086-T091, T095 |

## Implementation Strategy

### P1 MVP

The minimum deployable implementation is Phases 1-5, not US1 alone: US1's scale claim depends on US3's one-statement metadata behavior and US2's lifecycle-safe snapshot/refresh orchestration. Complete and validate each P1 checkpoint in dependency order.

### Local Merge Gate

Complete adapter, unit, behavior, dispatch-count, source-guard, and driver-calculation tests locally using the existing Python virtual environment. Do not start Redis, Postgres, Kafka, Flink, Compose services, or production-equivalent transport locally.

### External Acceptance Gate

Create all opt-in tests, fixtures, proxies, and driver commands during implementation. Execute T026, T076-T079, and T090-T091 only on the user's separate production-equivalent machine against explicitly isolated state. A deferred external run remains an explicit open acceptance item; it is not converted into a passing local gate.

### Completion Rule

Implementation is ready for review only when all local tasks pass, external tasks are either evidenced or explicitly deferred to the separate machine, no prohibited scope file changed, and every contract failure row has both a test and an implementation path.

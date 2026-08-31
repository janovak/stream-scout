# Implementation Plan: Batched Poller Datastore I/O

**Branch**: `006-batch-poller-io` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/006-batch-poller-io/spec.md`

## Summary

The stream poller currently performs synchronous Redis online-state operations
and a Postgres metadata transaction once per ranked channel. At 500-800
channels, remote round-trip latency consumes most or all of the 120-second poll
interval and starves the reconciler that shares the same process and event
loop.

This feature keeps the synchronous clients and the existing scheduler, but
changes the poll into a fixed sequence of batched remote phases:

1. Fetch, filter, and normalize the ranking; read the previous desired set;
   compute desired membership and departures locally.
2. Persist all unique streamer metadata with one
   `INSERT ... VALUES ... ON CONFLICT` statement and one transaction
   completion. `psycopg2.extras.execute_values()` is called with
   `page_size=len(rows)` so its default 100-row paging cannot create five or
   nine statement dispatches at 500 or 900 rows.
3. Read the union of current and departed online keys with one `MGET`, derive
   all lifecycle decisions from that pre-refresh snapshot, and refresh all
   current keys with one non-transactional Redis pipeline execution.
4. Publish lifecycle events only after the refresh is acknowledged, retain the
   existing atomic desired-set publication, and notify the reconciler only
   after that publication succeeds.

The implementation remains in the existing `stream-monitoring` process. It
does not move per-row work to threads or an executor, alter EventSub policy,
change production thresholds, redesign a schema, or upgrade Python or any
dependency.

## Technical Context

**Language/Version**: Python 3.11 (unchanged; `services/stream-monitoring/Dockerfile`)
**Primary Dependencies**: `psycopg2-binary==2.9.9`, `redis==5.0.1`, `APScheduler==3.10.4`, `prometheus-client==0.19.0`, `confluent-kafka==2.3.0`, `twitchAPI==4.5.0` (all pins unchanged)
**Storage**: Existing Postgres `streamers` table and Redis `streamer:online:*`/desired-set keys; no schema or key-layout change
**Testing**: pytest in `services/stream-monitoring/.venv`; in-memory Redis/pool fakes, opt-in isolated-schema real-Postgres tests, and a validation-only production-equivalent scale driver
**Target Platform**: Existing Linux Docker Compose `stream-monitoring` container with remote Postgres and Redis over Tailscale
**Project Type**: Existing backend service in a multi-service repository
**Process Model**: Poller and reconciler remain asynchronous tasks in one Python process/event loop; synchronous datastore clients batch network work rather than relocating it
**Performance Goals**: Nearest-rank p95 under 5 seconds at 500 and 10 seconds at 900 for 20 stable and 20 complete-turnover polls; no overlap skips; direct reconciler pass gaps at most 15/20 seconds during separate 30-minute post-convergence runs
**Constraints**: Poll interval 120 seconds; online-state TTL 180 seconds; production `JOIN_THRESHOLD=150`, `LEAVE_THRESHOLD=300`, and `CLIPPING_DISABLED_FETCH_BUFFER=120`; EventSub capacity exactly 900; proportional ranking pagination and Kafka lifecycle publication remain allowed
**Scale/Scope**: Operation-count gates at 50, 500, and 900; up to 1,800 current-plus-departed snapshot keys during complete turnover; one metadata row per observed streamer identity

No `NEEDS CLARIFICATION` items remain. Phase 0 decisions are recorded in
[research.md](./research.md).

## Constitution Check

*GATE: Passed before Phase 0 research and re-checked after Phase 1 design.*

| Principle | Pre-design gate | Post-design re-check |
|---|---|---|
| Kafka for inter-service messaging | PASS - lifecycle events remain on `stream-lifecycle`; no new inter-service path | PASS - batching is limited to datastore work and does not replace Kafka |
| Postgres exclusively for persistent storage | PASS - streamer metadata remains in Postgres; Redis remains transient online/intent state | PASS - no schema or persistent store is added |
| PyFlink for stream processing | PASS - no Flink work is in scope | PASS - contracts and validation leave Flink unchanged |
| Twitch API integration | PASS - ranking and EventSub integrations remain | PASS - no API, rate-limit, or transport change is designed |
| Prometheus metrics | PASS - phase and total poll timing plus metadata failure streak are required | PASS - bounded-label metrics are part of the observability contract |
| Grafana/Loki observability | PASS - existing structured logger remains the log path | PASS - every failure outcome has phase, batch, and error context |
| No event-pipeline data loss | PASS with validation gate - lifecycle decisions retain the pre-refresh semantics and Kafka path | PASS - failed/unknown state refresh suppresses speculative events and desired publication; desired publication remains atomic |
| Health endpoints | PASS - existing `/health` is unchanged | PASS - no new process or service is introduced |
| Bot-command filtering | PASS - Flink-side filter is untouched | PASS - no stream-processing change |
| Python virtual environments | PASS - validation uses `services/stream-monitoring/.venv` | PASS - no install or dependency change is required |

There are no constitution violations to justify. The design deliberately
retains Redis only for the existing transient coordination and TTL state; it
does not make Redis a persistent source of truth.

## Project Structure

### Documentation (this feature)

```text
specs/006-batch-poller-io/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── plan-critique.md
├── contracts/
│   └── poller-batch-contract.md
├── checklists/
│   ├── requirements.md
│   └── requirements-quality.md
└── spec.md
```

`tasks.md` is intentionally absent. It is Phase 2 `/speckit.tasks` output and
is not created by this Stage 2 planning run.

### Source Code (implementation target, repository root)

```text
services/stream-monitoring/
├── stream_monitoring_service.py     # batch orchestration, SQL write, Redis snapshot/refresh, timing
├── desired_set_store.py             # unchanged bounded read and atomic publication
├── reconciler.py                    # unchanged policy; existing callback used by validation
├── test_support.py                  # extend fake Redis/pipeline batch behavior
├── test_stream_monitoring.py        # lifecycle, failure, count, metrics, real-Postgres coverage
└── phase5/
    ├── feature006_driver.py         # validation-only scale and schedulability driver
    └── feature006_fixtures.py       # exact eligible-count paginated fixtures

OPERATIONS.md                         # implementation-stage validation/ramp procedure update
docker-compose.yml                    # no value changes; assert 150/300/120 remain
```

**Structure Decision**: Keep the production change in
`stream_monitoring_service.py`, where the poll phases already live. Do not
move the existing desired-set layout or publication into a new abstraction,
and do not modify reconciler policy. Validation-only fixture and driver code
stays outside the Dockerfile and Compose bind mounts so it cannot become part
of the production service accidentally.

## Poll Design

### 1. Normalize and decide membership before online-state I/O

After the existing paginated ranking fetch and bulk clipping-eligibility
lookup:

- Normalize each eligible stream to `(rank, streamer_id, login.lower())`.
- Preserve ranking order and the existing login-keyed desired-set behavior.
- Collect a separate metadata map keyed by `streamer_id`; assigning while
  iterating ranking order makes the final occurrence win.
- Read the previous desired set through `DesiredSetStore.read()` exactly as
  today.
- Compute desired membership with the unchanged `compute_desired_set()`.
- Compute departed logins from the previous desired set before any online key
  is read or refreshed.
- Build an ordered, unique union of current and departed normalized logins for
  the snapshot. Deduplicating snapshot keys changes no login-keyed semantics;
  it only avoids duplicate arguments to the same `MGET`.

The current ranking list is not deduplicated by streamer ID for desired-set
purposes. Only the metadata write is identity-deduplicated, as required by the
existing sequential final-state behavior.

### 2. Persist metadata with one statement

For every non-empty metadata map:

- Acquire one pooled connection.
- Call `psycopg2.extras.execute_values()` once with a single
  `INSERT INTO streamers (...) VALUES %s ON CONFLICT (streamer_id) DO UPDATE`
  template.
- Set `page_size=len(rows)`. The psycopg2 default is 100 and would dispatch
  multiple SQL statements for 500 or 900 rows.
- Use the two-level placeholder shape explicitly:

  ```python
  execute_values(
      cursor,
      "INSERT INTO streamers (...) VALUES %s ON CONFLICT (...) DO UPDATE ...",
      rows,
      template="(%s, %s, NOW())",
      page_size=len(rows),
  )
  ```

  `rows` contains two-tuples of ID/login; the outer SQL `%s` is the expanded
  values list and the template placeholders are per-row values.
- Use Postgres `NOW()` in the values template and assign
  `last_seen_at = EXCLUDED.last_seen_at`. PostgreSQL's transaction timestamp is
  common to every row in the one statement and preserves the existing
  database-clock source.
- Commit once. Count the SQL statement dispatch and transaction completion as
  separate fixed interactions.
- On any row, statement, or commit failure, log the input and unique batch
  sizes plus failure-streak context, attempt rollback, and return the
  connection through the pool in a clean/discardable state. Do not retry rows
  individually and do not re-raise into the healthy state/intent phases.
- Reset the metadata failure streak only after a successful non-empty batch.
  An empty batch issues no connection or SQL operation and does not falsely
  count as recovery.

The next poll reconstructs and retries the entire current metadata batch;
there is no side queue and no per-row poison-record escape hatch.

### 3. Snapshot once, then refresh once

After membership and departures are fixed:

- If the current-plus-departed key union is non-empty, issue one standalone
  Redis `MGET`. A single Redis command supplies the consistent pre-refresh
  view; a non-transactional pipeline of individual reads would not.
- Treat each `None` response as absent and every other value as present.
- Derive online and offline lifecycle candidates exclusively from this stored
  snapshot. For repeated identical logins, evaluate lifecycle presence once at
  the best/first rank, matching the prior loop in which the first refresh made
  later identical-login occurrences present.
- If current ranked logins are non-empty, queue every current `SETEX` in
  ranking order on `pipeline(transaction=False)` and call `execute()` once.
  Keeping repeated login writes in rank order preserves the prior final value
  if malformed upstream input repeats one login with different IDs.
- Use the default `raise_on_error=True`. redis-py collects batch responses and
  raises the first element-level `ResponseError`; any such error fails the
  entire phase even if other commands were applied.
- Publish no lifecycle candidate until the refresh execution is acknowledged
  without any batch-level or element-level error.

If both current and previous sets are empty, skip metadata, `MGET`, and refresh
execution but still atomically publish empty desired intent. If current is
empty and departures exist, perform one `MGET`, skip refresh, evaluate
offline events, and publish empty intent.

### 4. Publish lifecycle and desired intent

After a successful online-state phase:

- Publish `online` only for a login absent in the pre-refresh snapshot whose
  first rank is at or inside `JOIN_THRESHOLD`.
- Refresh but do not emit `online` for a new login outside the entry band.
- Publish `offline` only for a departed login absent in the snapshot.
- Resolve offline broadcaster identity only from the previous desired-set ID
  map. A missing, non-numeric, or non-positive ID is a data-integrity error;
  suppress that event, log the login and previous generation, and continue
  other valid events.
- Call the unchanged `DesiredSetStore.publish()` after lifecycle publication.
  Its existing `DEL`/`ZADD`/`HSET`/`INCR` `MULTI/EXEC` remains the atomic
  publication boundary, including an empty desired set.
- Notify the reconciler only after `publish()` returns successfully.

Kafka lifecycle publication remains per event and non-transactional with
desired publication, matching current behavior and the feature scope. If
desired publication fails after acknowledged refresh and lifecycle output,
those events are not represented as rolled back.

## Remote Interaction Budget

Counts are client-visible dispatches, not Python helper invocations, rows,
queued Redis commands, or datastore-side work.

| Phase | Empty input | Non-empty input | Dispatch boundary |
|---|---:|---:|---|
| Clipping eligibility lookup (existing) | 0 | 1 SQL statement | `cursor.execute()` |
| Previous desired-set read (existing) | 3 Redis commands | 3 Redis commands | `ZRANGE`, `HGETALL`, `GET` |
| Metadata persistence | 0 | exactly 1 SQL statement + 1 transaction completion | `cursor.execute()` inside `execute_values`; `commit()` |
| Online-state snapshot | 0 when current+departed union is empty | at most 1 Redis command | `MGET` |
| Online-state refresh | 0 when current is empty | at most 1 Redis batch execution | `Pipeline.execute()` |
| Desired-set publication (existing) | 1 Redis batch execution | 1 Redis batch execution | atomic `Pipeline.execute()` |

For equivalent non-empty phases, these counts must be identical at 50, 500,
and 900 channels. `execute_values()` paging is observed at
`cursor.execute()`, so one helper call cannot hide multiple statements.
Commands queued inside one acknowledged Redis pipeline count as one remote
cycle, as specified.

## Failure and Completion Contract

| Failure point | Metadata result | Lifecycle / desired intent | Reconciler notification | Poll completion |
|---|---|---|---|---|
| Ranking fetch/filter | Not attempted | Not attempted | Suppressed | Failed at `ranking_fetch` |
| Previous desired-set read | Not attempted | Not attempted | Suppressed | Failed at `desired_read` |
| Metadata statement/commit | Whole batch failed or outcome unknown; streak increments; retry all next poll | Healthy online/lifecycle/intent phases continue | Allowed only after successful intent publish | Distinct `metadata_failed` completion, never metadata success |
| Online snapshot read | Metadata may already have committed | No refresh, events, or intent | Suppressed | Failed at `online_snapshot` |
| Online refresh execution or element | Metadata may already have committed | No events or intent, even if some refreshes applied | Suppressed | Failed at `online_refresh` |
| Missing previous ID for one departure | Unchanged | Suppress only corrupt offline event; other events and intent continue | Allowed after intent publish | Success or metadata-failed, with data-integrity error |
| Desired intent publish / unknown acknowledgement | Unchanged | Prior acknowledged lifecycle output is not rolled back; visible desired version is not assumed | Suppressed | Failed at `desired_publish` |

Every poll records one final bounded outcome. A metadata-only failure uses a
different log message/outcome from full success even when desired intent and
reconciler notification complete.

## Observability Design

Add bounded-label Prometheus instruments in `stream_monitoring_service.py`:

- `stream_poll_duration_seconds{outcome}`: total wall time from immediately
  before ranking retrieval through desired publication and notification, or
  through the failing phase.
- `stream_poll_phase_duration_seconds{phase,outcome}`: durations for
  `ranking_fetch`, `metadata_persistence`, `online_snapshot`,
  `online_refresh`, `lifecycle_publication`, and
  `desired_set_publication`. Outcomes are the bounded values `success`,
  `failure`, and `empty`.
- `stream_metadata_consecutive_failures`: current count of failed non-empty
  metadata batches, reset only by a successful non-empty batch.

Structured logs include final poll outcome, failed phase, total duration,
phase durations, ranked/desired/entered/left counts, metadata input and unique
batch sizes, and metadata failure streak where applicable. Existing
`twitch_api_errors_total`, reconciler metrics, health endpoint, and centralized
logging path remain unchanged.

Operation-count instrumentation belongs in tests and the validation driver,
not in production metrics. It wraps Redis command/pipeline execution,
Postgres `cursor.execute()`, and transaction completion so internal helper
paging cannot be hidden.

## Validation Strategy

### Automated behavior and failure tests

Extend the existing stream-monitoring pytest suite to cover:

- unchanged hysteresis and desired ordering for empty, partial, stable, and
  complete-turnover rankings;
- one pre-refresh snapshot and one refresh execution at 50, 500, and 900;
- empty/empty and departures-only operation omissions;
- entry-band online, outside-band refresh without event, stable state,
  departed present/absent state, real/missing previous IDs, login changes, and
  repeated-login behavior;
- snapshot, whole refresh, element-level refresh, desired-read, desired-publish,
  metadata, and post-failed-refresh-expiry recovery paths;
- metadata failure continuing healthy online/intent work while producing a
  distinct completion outcome and failure streak;
- immutable production Compose values 150/300/120.

The test adapter must model the redis-py surface used by the design rather
than merely count old calls:

- `FakeRedis.mget(keys)` returns one ordered value-or-`None` entry per key;
- `FakeRedis.pipeline(transaction=...)` accepts both desired publication's
  default transactional mode and online refresh's `transaction=False`;
- `FakePipeline.setex()` queues refreshes;
- `FakePipeline.execute(raise_on_error=True)` returns per-command results,
  processes all server-side responses, and raises the first injected
  `redis.exceptions.ResponseError` after applying other successful commands;
- separate deterministic hooks model a transport failure before application
  and an unknown acknowledgement after application.

This distinction is required to test whole-batch errors, acknowledged
element-level errors, and indeterminate outcomes without pretending that a
failed non-transactional pipeline applied nothing.

### Real Postgres

Use the existing opt-in isolated-schema fixture and a connection/cursor proxy
against real Postgres. A 900-row case proves one `cursor.execute()` dispatch,
one commit, insert/update behavior, login changes, duplicate-ID last-wins,
common and advancing `last_seen_at`, rollback of a poison batch, reuse of the
same pooled connection after rollback, and full-batch success on the next
poll attempt. Setup/inspection SQL is excluded from the measured interval.

### Controlled performance fixtures

The validation-only driver uses two deterministic, disjoint paginated ranking
fixtures at each scale:

- raw records include the same-environment measured clipping-disabled
  proportion;
- measurement-only threshold and fetch-capacity overrides yield exactly 500
  or 900 eligible records after filtering;
- page size remains 100;
- stable runs repeat fixture A with all current online keys present;
- turnover runs alternate A/B and prepare both incoming and departed keys as
  absent outside the timed interval, maximizing permitted lifecycle output.

Before each scale run, consume enough live ranking pages in the same
environment to collect at least 20 page-latency observations. Set synthetic
page delay to at least their nearest-rank p95. Record raw count, disabled
count/proportion, page count, eligible count, live p95, fixture delay,
page-count-times-delay budget, datastore RTT medians, and remaining non-ranking
budget.

Each profile runs one warm-up plus 20 completed measured polls. The 19th value
in ascending duration order is the nearest-rank p95. Excluded failures replace
the whole poll; no duration subtraction is allowed.

### Reconciler schedulability and cold start

For separate post-convergence 30-minute runs at 500 and 900:

- attach a composite validation callback to the existing
  `Reconciler.on_pass_complete` seam;
- retain the production gauge callback and append `time.monotonic_ns()` for
  every pass completion;
- attach APScheduler listeners for maximum-instance, missed, error, and
  completion events;
- compute adjacent pass gaps directly, never from the 15-second Prometheus
  scrape.

For the 900-channel cold-subscription run, start only after service
initialization and datastore-pool establishment with zero EventSub
subscriptions in an isolated validation environment. A recording proxy around
the real transport observes successful creates and `RateLimitedError` without
changing policy. Evidence must show at least one real backoff, every scheduled
poll starting without an overlap skip, every non-failing poll under 10
seconds, and increased subscription coverage in each retry window where
creates are accepted. A pass-completion timestamp is not required to advance
while the existing long retrying pass remains in progress.

Detailed execution and evidence fields are in
[quickstart.md](./quickstart.md).

## Deployment and Rollback

- `docker-compose.yml` remains at `JOIN_THRESHOLD=150`,
  `LEAVE_THRESHOLD=300`, and `CLIPPING_DISABLED_FETCH_BUFFER=120`.
- The implementation adds no environment variable required for correctness,
  schema migration, package, Python version, Docker service, bind-mounted
  production module, scheduler change, or feature flag.
- Deployment uses the existing `--force-recreate stream-monitoring` procedure
  because source files are bind-mounted by inode.
- A channel-count increase is a later operational decision after evidence is
  reviewed; it is not part of feature deployment.
- Rollback is one code revision and container recreation. Existing datastore
  contents, schema, dependencies, thresholds, EventSub policy, and Flink
  state require no reversal.

## Explicit Non-Goals

- No thread or executor relocation of per-row calls
- No psycopg 3 or async Redis migration
- No dependency or Python upgrade
- No scheduler, poll interval, TTL, rate-limit, backoff, or retry-policy change
- No EventSub capacity or connection-pool change
- No Flink, clip-budget, anomaly-ranking, clip-creation, or feature 005 work
- No datastore schema or desired-set layout redesign
- No production threshold or clipping-buffer change

## Complexity Tracking

No constitution violation or additional architectural layer requires
justification. The validation-only driver is intentionally excluded from the
production image and does not add a runtime component.

# Research: Batched Poller Datastore I/O

All Phase 0 questions are resolved. There are no remaining
`NEEDS CLARIFICATION` items.

## D1 - Batch remote work in the poller; do not relocate it

**Decision**: Keep the synchronous psycopg2 and redis-py clients on the current
poll path, but replace channel-count-dependent remote calls with one bulk SQL
statement, one online-state snapshot command, and one online-state refresh
pipeline execution.

**Rationale**: The observed failure is remote round-trip multiplication, not
local iteration. Moving the same per-row calls to a thread or executor would
retain channel-count-dependent datastore traffic, introduce cross-thread
coordination, and leave a synchronous event-loop task waiting on the same
total remote work. Batching directly removes the measured cost while
preserving the one-process/one-event-loop architecture required by NFR-003.

**Alternatives considered**:

- `asyncio.to_thread()` or an executor: rejected because it hides rather than
  removes per-row calls and is explicitly out of scope.
- Async Redis or psycopg 3: rejected because it requires dependency and client
  migration without reducing statement/command count by itself.
- A new worker service: rejected because it adds operational complexity and a
  new coordination protocol for a problem solvable inside the poll.

## D2 - Finish desired membership and departures before online-state I/O

**Decision**: Normalize the current ranking, read the previous desired set,
run the existing hysteresis computation, and compute departed logins before
issuing any online-state read or refresh.

**Rationale**: A complete current-plus-departed set is necessary to obtain one
pre-refresh view. It also removes the current order dependence in which each
ranked key is refreshed before later existence checks. Ranking order,
clipping eligibility, login-keyed desired membership, and previous desired
state stay unchanged.

**Alternatives considered**:

- Refresh each current key while iterating and inspect departed keys later:
  rejected because lifecycle decisions then see different points in time and
  require per-key calls.
- Read current keys, compute desired membership, then read departures:
  rejected because it requires two snapshots and allows state to change
  between them.
- Deduplicate the entire ranking by streamer ID: rejected because only
  metadata has identity-deduplication semantics; desired membership remains
  keyed by normalized login under the existing rules.

## D3 - Use one `MGET` for the pre-refresh online-state snapshot

**Decision**: Issue one standalone Redis `MGET` for the ordered unique union
of current and departed online keys. Guard the empty union so no command is
sent.

**Rationale**: `MGET` is one Redis command and therefore one atomic server-side
view of all requested keys. redis-py 5.0.1 returns values in key order and
`None` for absent/expired keys. A pipeline of separate reads can reduce TCP
round trips but, without `MULTI/EXEC`, is not a consistent snapshot because
other client commands can interleave.

**Alternatives considered**:

- Non-transactional pipeline of `EXISTS`: rejected because it is not a
  consistent snapshot.
- Transactional pipeline of `EXISTS`: valid but needlessly sends multiple
  commands and transaction framing when one `MGET` has the required semantics.
- Lua snapshot-and-refresh: rejected because lifecycle must retain the
  pre-refresh view after the call and a script adds unnecessary server code.

**References**:

- Redis `MGET`: <https://redis.io/docs/latest/commands/mget/>
- redis-py pipelines:
  <https://redis.readthedocs.io/en/stable/advanced_features.html#pipelines>
- Local pin: `services/stream-monitoring/requirements.txt`

## D4 - Refresh all current keys with one non-transactional pipeline

**Decision**: Queue current `SETEX` commands in ranking order on
`redis_client.pipeline(transaction=False)` and call `execute()` exactly once.
Do not call it for an empty current ranking. Keep the default
`raise_on_error=True`.

**Rationale**: redis-py packs a non-transactional pipeline into one
client-visible batch execution. Its execution reads every response and raises
the first element-level `ResponseError` by default, which makes any
acknowledged command error a phase failure. Ranking-order queueing preserves
the prior final key value if the same normalized login appears more than once.
Atomic all-key refresh is not required; the specification already defines
partial or unknown refresh outcomes as failures whose next poll relies on
observed state.

**Alternatives considered**:

- `MULTI/EXEC`: rejected as unnecessary transaction framing; command errors
  can still coexist with successful commands in an EXEC response.
- `raise_on_error=False` with manual scanning: rejected because the default
  already has the required behavior and manual scanning is easier to omit.
- One `SETEX` per key: rejected because it recreates the remote-latency defect.

**References**:

- redis-py 5.0.1 `Pipeline.execute()` and `_execute_pipeline()` behavior in
  `redis/client.py`
- Local desired publication intentionally remains transactional in
  `services/stream-monitoring/desired_set_store.py`

## D5 - Deduplicate metadata by streamer ID with the final ranking occurrence

**Decision**: While iterating normalized ranked records in order, assign each
`streamer_id` into an insertion-ordered dictionary. A later occurrence
overwrites that ID's login. Convert the final mapping to one values list for
the SQL statement.

**Rationale**: The current per-row upsert leaves the login from the final
occurrence. Python-side last-assignment-wins reproduces that result and is
required before one Postgres upsert: PostgreSQL rejects an
`ON CONFLICT DO UPDATE` statement that would affect the same target row twice.
The deduplication is identity-specific and does not change desired-set
login/rank semantics.

**Alternatives considered**:

- Send duplicates directly: rejected because Postgres raises
  `ON CONFLICT DO UPDATE command cannot affect row a second time`.
- First occurrence wins: rejected because it differs from the prior sequential
  final state.
- SQL `DISTINCT ON`/CTE deduplication: rejected because it adds ordering logic
  to the statement when the ranking-ordered Python pass already has the exact
  rule.

**References**:

- PostgreSQL `INSERT`: <https://www.postgresql.org/docs/current/sql-insert.html>
- Cardinality violation explanation:
  <https://pganalyze.com/docs/log-insights/app-errors/U126>

## D6 - Force `execute_values` to emit one statement

**Decision**: Use `psycopg2.extras.execute_values()` with
`page_size=len(deduplicated_rows)` for every non-empty batch. Instrument
`cursor.execute()`, not the helper call, in validation.

**Rationale**: In psycopg2 2.9.x, `execute_values` defaults to
`page_size=100` and invokes `cursor.execute()` once per page. The default would
therefore dispatch five statements for 500 rows and nine for 900. Setting the
page size to the actual non-empty batch length produces exactly one statement
at every supported size. Nine hundred two-parameter rows plus `NOW()` are far
below practical Postgres query-size limits.

**Alternatives considered**:

- Default `execute_values`: rejected because helper-level batching would hide
  channel-count-dependent statement dispatches.
- Constant `page_size=1000`: works for the current ceiling but encodes a second
  scale limit; the actual length states the invariant directly.
- `execute_batch` or `executemany`: rejected because they execute multiple SQL
  commands.
- `COPY` through a staging table: rejected because upsert requires an extra
  statement/table step and broadens schema/design scope.

**References**:

- Psycopg2 fast execution helpers:
  <https://www.psycopg.org/docs/extras.html#psycopg2.extras.execute_values>
- Psycopg2 2.9 implementation:
  <https://github.com/psycopg/psycopg2/blob/2_9/lib/extras.py>

## D7 - Keep database time as the `last_seen_at` authority

**Decision**: Put `NOW()` in the one values template and update conflicts with
`last_seen_at = EXCLUDED.last_seen_at`.

**Rationale**: PostgreSQL `NOW()` is the transaction-start timestamp and is
stable across every row in the one statement. This gives the batch a common
observation time while preserving the current database-clock semantics and
avoiding an application-host clock dependency. Assigning from `EXCLUDED`
ensures insert and update paths store the same value.

**Alternatives considered**:

- Capture `datetime.now(timezone.utc)` in Python: testable and valid, but
  rejected because the current implementation uses the database clock and no
  behavior requires moving that authority.
- Call `NOW()` separately in each conflict assignment: still stable in the
  transaction, but using `EXCLUDED.last_seen_at` states the shared-value
  contract explicitly.

**Reference**:

- PostgreSQL date/time functions:
  <https://www.postgresql.org/docs/current/functions-datetime.html>

## D8 - Treat metadata failure as non-fatal but never successful

**Decision**: The metadata helper owns connection acquisition, statement,
commit, rollback, pool return, logging, and a boolean result. Any failure
increments a consecutive-failure streak and returns failure without a per-row
fallback. The poll continues to online state and desired publication, but its
final outcome is `metadata_failed`, not success.

**Rationale**: Metadata and subscription intent have intentionally independent
availability. A poison record must not block online-state freshness or
reconciler progress, but it must make all metadata staleness visible. Reusing
the entire reconstructed batch on the next poll makes retry idempotent through
`ON CONFLICT`.

**Alternatives considered**:

- Re-raise and abort the poll: rejected by FR-012.
- Retry rows individually to isolate poison data: rejected because it violates
  whole-batch atomicity and restores channel-count-dependent calls.
- Drop only the malformed row: rejected because it silently loses metadata and
  changes retry semantics.
- Reset the streak on an empty batch: rejected because no metadata recovery
  was demonstrated.

## D9 - Gate state-dependent work on snapshot and refresh success

**Decision**: Snapshot or refresh failure suppresses lifecycle publication,
desired intent, reconciler notification, and successful completion. Lifecycle
candidates are computed from the snapshot but published only after refresh
success. Desired-publication failure suppresses notification but does not
claim that already published lifecycle events were rolled back.

**Rationale**: This is the only ordering that avoids speculative lifecycle
events when refresh application is partial or unknown. It also preserves the
existing atomic desired-set publication and the explicit non-transactional
boundary between Kafka lifecycle events and Redis intent.

**Alternatives considered**:

- Publish online events before refresh acknowledgement: rejected because an
  event could claim state that did not refresh.
- Publish desired intent after a failed refresh: rejected because the
  reconciler would act on a poll that failed to preserve online state.
- Notify before desired publication returns: rejected because the reconciler
  could read the old generation.

## D10 - Suppress missing-ID offline events as data-integrity failures

**Decision**: Resolve a departed login's broadcaster ID only from the previous
desired ID map. If it is missing, unparseable, or non-positive, log a
data-integrity error and suppress only that offline event.

**Rationale**: The previous desired set is the authoritative identity carried
across polls. Publishing ID `0` fabricates an identity and keys unrelated
events to the same Kafka partition. Continuing other valid events and intent
contains one corrupt mapping without converting it into a full poll outage.

**Alternatives considered**:

- Placeholder ID `0`: rejected explicitly by FR-008.
- Look up the departed login in Postgres or Twitch: rejected because it adds a
  new remote dependency and per-departure latency.
- Abort the whole poll: rejected because the specification permits other valid
  lifecycle and desired work to continue.

## D11 - Count operations at dispatch boundaries

**Decision**: Use validation-only proxies that count:

- standalone Redis command dispatches;
- each Redis `Pipeline.execute()` once, independent of queued command count;
- each Postgres `cursor.execute()` call made inside `execute_values`;
- each transaction `commit()`/`rollback()` completion separately.

Report counts by named phase and compare equivalent non-empty polls at 50,
500, and 900. Include empty/empty and departures-only cases.

**Rationale**: Counting `_upsert_streamer_batch()` once would miss
`execute_values` internal paging. Counting queued Redis commands would
incorrectly treat one acknowledged pipeline as many network cycles. The client
dispatch boundary matches the specification's observable network model.

**Alternatives considered**:

- Count service helper calls: rejected because helpers can page internally.
- Redis server-wide command statistics: rejected because other clients add
  noise and pipeline commands are counted server-side one by one.
- Wall time alone: rejected because fast local tests do not detect an
  accidentally reintroduced per-row remote path.

## D12 - Use controlled fixtures calibrated from live page latency

**Decision**: At each 500/900 scale point, create two deterministic disjoint
paginated ranking fixtures. Derive raw count and test-only fetch buffer from a
same-environment measured clipping-disabled proportion so post-filter eligible
count is exact. Use 100-row pages and delay each page by at least the
nearest-rank p95 of at least 20 same-environment live page observations.

Stable polls repeat fixture A with current online state present. Turnover polls
alternate A and B and prepare both incoming and departed state absent outside
the timed interval. Each profile has one warm-up and 20 measured polls.

**Rationale**: Live rankings do not naturally turn over completely, and a
zero-delay fake omits the proportional ranking cost that shares the end-to-end
budget. Controlled inputs make the worst lifecycle volume reproducible while
live calibration preserves a realistic pagination budget.

**Alternatives considered**:

- Measure only live rankings: rejected because complete turnover is neither
  controllable nor repeatable.
- Fixed synthetic page delay: rejected because it can become unrepresentative
  of the deployment network.
- Generate exactly 500/900 raw records: rejected because clipping filtering
  would make the eligible count short and invalidate the run.
- Change production thresholds/buffer for deployment: rejected; overrides are
  measurement-only and production remains 150/300/120.

## D13 - Capture reconciler completion in-process

**Decision**: For each 30-minute post-convergence scale run, attach a composite
callback to the existing `Reconciler.on_pass_complete` seam. Keep the
production gauge callback and record `time.monotonic_ns()` for every completed
pass. Compute adjacent gaps directly. Attach APScheduler event listeners to
record overlap/misfire/error/completion events.

**Rationale**: The 15-second production metrics scrape interval cannot prove a
15- or 20-second maximum gap. The existing callback fires once for every
completed pass and monotonic nanoseconds avoid wall-clock adjustments.

**Alternatives considered**:

- Sample `reconcile_last_success_timestamp` from Prometheus: rejected because
  the scrape cadence is as large as the 500-channel acceptance bound.
- Parse logs after the run: usable corroboration, but rejected as the primary
  measurement because buffering and timestamp resolution can hide a pass.
- Change reconciler cadence or add a production sampling task: rejected as
  unnecessary and out of scope.

## D14 - Observe cold-start backoff without changing policy

**Decision**: In an isolated validation environment, wrap the real EventSub
transport with a recording proxy that delegates behavior unchanged while
timestamping successful creates and `RateLimitedError`. Start from zero
subscriptions only after process initialization and datastore-pool
establishment. Correlate create windows, backoff logs/counter deltas, scheduled
poll starts/completions, and subscription-count growth.

**Rationale**: A fake backoff proves concurrency but not the current external
acceptance behavior. The feature must demonstrate that the poller stays
schedulable during a real long reconcile pass without imposing a new
convergence deadline or modifying retries.

**Alternatives considered**:

- Shorten backoff for the test: rejected because it changes the policy under
  validation.
- Require `reconcile_last_success_timestamp` to advance during the pass:
  rejected because that gauge denotes pass completion and is allowed to remain
  fixed during one long retrying cold pass.
- Treat 900 as operational headroom: rejected because it is the exact
  three-by-300 transport ceiling.

## D15 - Add bounded phase telemetry and a metadata failure streak

**Decision**: Record total poll duration by final outcome, phase duration by
bounded phase/outcome labels, and the current consecutive non-empty metadata
failure count. Emit distinct final logs for full success, metadata-only
failure, and fatal phase failure.

**Rationale**: Operators need to separate proportional ranking cost, metadata
latency, online-state handling, and desired publication. A total error counter
does not reveal persistent all-row metadata staleness, while a consecutive
gauge and batch-size logs do. Bounded labels avoid uncontrolled Prometheus
cardinality.

**Alternatives considered**:

- Logs only: rejected because the specification requires production telemetry
  suitable for ramp decisions.
- Per-login labels: rejected because they create scale-dependent cardinality.
- Treat metadata-only completion as normal success: rejected because it hides
  stale metadata.

## D16 - Preserve deployment boundaries and rollback simplicity

**Decision**: Change no schema, requirement pin, Python version, scheduler,
EventSub configuration, or production threshold. Validation-only modules are
not copied into the image or bind-mounted. Deploy by recreating only the
existing stream-monitoring container; roll back by reverting the code revision
and recreating it.

**Rationale**: The batch design needs no migration. Separating the code
deployment from any later channel-count ramp preserves the proven 150/300
operating point and makes rollback independent of data/configuration changes.

**Alternatives considered**:

- Raise channel count with the code: rejected because it combines behavioral
  and scale risk.
- Add a compatibility feature flag: rejected because both paths use the same
  data model and a flag would preserve the defective per-row path without a
  migration need.
- Add a staging table or new Redis layout: rejected because neither is needed
  for one-statement/one-command batching.

## Independent Rubber-Duck Critique

An independent read-only plan critique on 2026-08-30 returned **GO** with no
blocking design, requirement-coverage, feasibility, or scope findings. The
review and dispositions are preserved in [plan-critique.md](./plan-critique.md).

Accepted findings:

1. The generated `CLAUDE.md` contained template `src/`/`tests/` paths and a
   nonexistent ruff command. It was corrected to the real
   `services/stream-monitoring` layout and existing pytest command.
2. The draft named `test_support.py` as an implementation target but did not
   spell out the fake Redis surface needed to distinguish element errors from
   transport/unknown outcomes. The plan and contract now require `mget`,
   `pipeline(transaction=...)`, queued `setex`, per-command responses,
   raise-on-error behavior, and separate before/after-application failures.
3. The two placeholder levels in `execute_values` could be misread. The plan
   and contract now include the exact `VALUES %s` plus
   `template="(%s, %s, NOW())"` shape.

No change was made for the critique's naming observation about
`feature006_driver.py`: feature-prefixed names intentionally avoid collisions
with the existing spec-004 `phase5/driver.py` and are validation-only.

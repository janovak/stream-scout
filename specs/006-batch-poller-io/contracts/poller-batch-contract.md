# Poller Batch Contract

This feature adds no public HTTP, GraphQL, Kafka, or datastore schema contract.
The contract is internal: it fixes poll phase ordering, datastore dispatch
ceilings, failure boundaries, lifecycle outputs, and validation evidence.

## Inputs

One poll receives:

- an ordered paginated Twitch ranking;
- the existing clipping-eligibility result;
- the previous `DesiredSet`;
- online-state values from Redis;
- access to the existing Postgres pool, Redis client, Kafka producer, and
  optional reconciler notification seam.

Production configuration remains:

```text
JOIN_THRESHOLD=150
LEAVE_THRESHOLD=300
CLIPPING_DISABLED_FETCH_BUFFER=120
POLL_INTERVAL_SECONDS=120
REDIS_STREAMER_TTL=180
```

Validation may override the first three in process to construct exact 500/900
eligible fixtures. It must record those values and must not alter production
Compose configuration.

## Phase Contract

The required order is:

```text
ranking fetch and clipping filter
    -> normalize current records
    -> read previous desired set
    -> compute desired membership and departures
    -> persist metadata batch (non-fatal to later phases)
    -> read current+departed online snapshot
    -> derive lifecycle candidates from snapshot
    -> refresh all current online keys
    -> publish valid lifecycle candidates
    -> atomically publish desired intent
    -> notify reconciler
    -> emit one final poll outcome
```

No online-state command may occur before desired membership and departures
have been computed.

## Metadata Batch Contract

### Input

An ordered sequence of normalized `(streamer_id, streamer_login)` records in
ranking order.

### Normalization

```text
metadata[streamer_id] = streamer_login
```

is applied in ranking order. The last assignment for an ID wins.

### Empty input

- Acquire no Postgres connection for metadata.
- Dispatch no metadata SQL.
- Do not alter the consecutive-failure streak.
- Return `not_attempted`.

### Non-empty input

- Dispatch exactly one `INSERT ... ON CONFLICT DO UPDATE` statement through
  one `cursor.execute()` call.
- `execute_values(..., page_size=len(rows))` is mandatory.
- The helper uses
  `template="(%s, %s, NOW())"` with two-element `(streamer_id, login)` rows;
  the `%s` in `VALUES %s` is the separate expanded-values placeholder.
- Complete the transaction once.
- Update every unique ID's current normalized login and `last_seen_at`.
- Leave `first_seen_at` and all clipping/EventSub refusal columns unchanged.

### Failure

- No per-row fallback or poison-record omission.
- Attempt rollback before pool return.
- Log at least input batch size, unique size, failure streak, error type, and
  error text.
- Return failure to the poll orchestrator without raising across the
  metadata/state availability boundary.
- Retry the whole current batch when the next poll reconstructs it.
- A failed or unknown commit is never reported as metadata success.

## Online-State Contract

### Snapshot key set

```text
ordered_unique(current ranked logins + departed desired logins)
```

### Snapshot

- Empty key set: no Redis read.
- Non-empty key set: exactly one standalone `MGET`.
- The response length must equal the requested key count; any protocol or
  connection failure fails the phase.
- `None` means absent; any other value means present.
- The stored snapshot is immutable for the rest of the poll.

### Refresh

- Empty current ranking: no pipeline execution.
- Non-empty ranking: queue all current `SETEX` operations in ranking order and
  execute one `pipeline(transaction=False)`.
- TTL and value remain the existing 180 seconds and broadcaster ID.
- Keep `raise_on_error=True`.
- Any batch exception or element-level `ResponseError` fails the entire phase.
- A failed/unknown phase publishes no lifecycle event or desired intent and
  sends no reconciler notification.

Partial Redis command application is not called success. The next poll reads
whatever state Redis then exposes and applies the normal pre-refresh rules.

## Lifecycle Contract

All decisions use only the pre-refresh snapshot.

| Current/Departed | Snapshot | Rank / ID | Output after refresh success |
|---|---|---|---|
| Current | absent | first/best rank `<= JOIN_THRESHOLD`; ID from that occurrence | one `online` event |
| Current | absent | first rank `> JOIN_THRESHOLD` | no event; state was still refreshed |
| Current | present | any | no event |
| Departed | absent | valid prior ID | one `offline` event with prior ID |
| Departed | present | any | no event |
| Departed | absent | missing/invalid prior ID | data-integrity error; no event |

Repeated identical current logins produce at most one online event, using the
best/first rank and the broadcaster ID from that same occurrence. Their
ranking-order refreshes may still leave the final occurrence's ID in the Redis
key. Different normalized logins remain distinct even when an upstream defect
assigns them the same broadcaster ID.

Kafka topic (`stream-lifecycle`), payload fields, broadcaster-ID key,
per-event production, and delivery callback do not change.

## Desired-Set Contract

`DesiredSetStore.read()`, `compute_desired_set()`, and
`DesiredSetStore.publish()` retain their existing behavior.

- The previous read remains bounded.
- Empty desired intent remains valid.
- Publication remains one atomic `MULTI/EXEC` containing
  `DEL`, optional `ZADD`/`HSET`, and `INCR`.
- Publication failure or unknown acknowledgement produces no reconciler
  notification and no successful poll outcome.
- The next poll re-reads and replaces whichever complete intent version is
  visible.

## Dispatch Count Contract

For an equivalent successful non-empty poll:

| Boundary | Required count at 50 | 500 | 900 |
|---|---:|---:|---:|
| Metadata `cursor.execute()` | 1 | 1 | 1 |
| Metadata transaction completion | 1 | 1 | 1 |
| Online snapshot `MGET` | 1 | 1 | 1 |
| Online refresh `Pipeline.execute()` | 1 | 1 | 1 |

Existing bounded clipping lookup, desired read, and desired publication are
recorded separately and must also remain channel-count independent.

Counting rules:

- Count every `cursor.execute()` generated inside a helper.
- Count commit/rollback acknowledgement separately from SQL.
- Count a standalone Redis command once.
- Count a Redis batch execution once.
- Do not count commands queued inside one acknowledged Redis batch as separate
  network cycles.
- Do not count Python helper calls, rows, payload bytes, server-side row work,
  ranking pages, or per-event Kafka production as datastore cycles.

Required omission cases:

| Case | Metadata | Snapshot | Refresh | Desired publish |
|---|---:|---:|---:|---:|
| Current empty, previous empty | 0 | 0 | 0 | 1 |
| Current empty, departures present | 0 | 1 | 0 | 1 |

## Completion and Telemetry Contract

Exactly one final outcome is emitted per invocation:

- `success`;
- `metadata_failed`;
- `ranking_failed`;
- `desired_read_failed`;
- `online_snapshot_failed`;
- `online_refresh_failed`;
- `desired_publish_failed`;
- `unexpected_failure`.

`metadata_failed` may still publish desired intent and notify the reconciler.
No other failure outcome may produce a success-shaped completion.

Required phase names:

```text
ranking_fetch
metadata_persistence
online_snapshot
online_refresh
lifecycle_publication
desired_set_publication
```

Metrics use bounded outcome labels only. Logs include phase, duration, relevant
record counts, and explicit error context.

## Validation Evidence Contract

The scale driver emits append-only JSON Lines. Each record has:

```json
{
  "schema": "stream-scout.feature006.v1",
  "kind": "record-kind",
  "run_id": "operator-assigned-id",
  "timestamp": "RFC3339 UTC"
}
```

### Calibration record

Required fields:

```text
scale
live_page_samples_ms
live_page_p95_ms
fixture_page_delay_ms
raw_records
disabled_records
disabled_proportion
eligible_records
page_size
page_count
ranking_budget_ms
non_ranking_budget_ms
redis_rtt_samples_ms
redis_median_ms
postgres_rtt_samples_ms
postgres_median_ms
```

The run is not acceptance-valid unless eligible count equals scale and each
datastore median is in the operator-observed 40-110 ms range.

### Poll-profile record

Required fields:

```text
scale
profile (stable | complete_turnover)
test_join_threshold
test_leave_threshold
test_fetch_buffer
warmup_duration_ms
measured_durations_ms (exactly 20 completed polls)
nearest_rank_p95_ms (19th ascending value)
overlap_skip_count
excluded_poll_count
phase_durations_ms
dispatch_counts
```

### Reconciler-gap record

Required fields:

```text
scale
post_convergence_started_at
run_duration_seconds (at least 1800)
pass_completion_monotonic_ns
adjacent_gaps_ms
maximum_gap_ms
scheduler_events
```

Pass timestamps come from every in-process completion callback, not metrics
scrapes.

### Cold-start record

Required fields:

```text
target (900)
initialization_complete_at
initial_subscription_count (0)
rate_limit_events
backoff_events
accepted_create_windows
subscription_count_by_window
poll_start_end_monotonic_ns
poll_durations_ms
overlap_skip_count
final_subscription_count
```

At least one backoff is required. Every accepted window must increase coverage;
the existing policy controls whether/when final convergence occurs.

## Compatibility Contract

The implementation must not change:

- dependency files or Python image;
- Postgres schema;
- Redis desired/online key names;
- scheduler timing or overlap policy;
- EventSub capacity, routing, concurrency, retry, or backoff;
- Kafka schemas or Flink behavior;
- production 150/300/120 configuration;
- feature 005 behavior.

## Test Adapter Contract

The in-memory Redis adapter used by automated validation must expose the same
surface relevant to this design:

```text
mget(keys) -> ordered list[value | None]
pipeline(transaction=True | False)
pipeline.setex(key, ttl, value)
pipeline.execute(raise_on_error=True) -> per-command response list
```

An injected element `ResponseError` is retained in the response sequence while
other queued commands are applied, then raised as the first error when
`raise_on_error=True`. Separate hooks represent connection failure before
application and acknowledgement loss after application. This prevents tests
from incorrectly treating every failed non-transactional pipeline as an
all-or-nothing operation.

## Stage 4 Evidence Disposition

The repository implementation, opt-in tests, fixtures, proxies, and command
surfaces satisfy the local evidence contract only after the local pytest and
offline CLI gates pass. They do not substitute for remote acceptance evidence.

Tasks T026, T076-T079, and T090-T091 remain unsatisfied until their commands
are run on the separate production-equivalent machine against explicitly
isolated state and the resulting Postgres, JSONL, timing, scheduler, Twitch,
deployment, and rollback evidence is retained. A deferred task is not a
passing result and does not weaken any field or threshold in this contract.

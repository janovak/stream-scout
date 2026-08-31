# Data Model: Batched Poller Datastore I/O

This feature adds no persistent entity, Postgres column, Redis key, or Kafka
schema. It changes how one poll assembles and moves existing data.

## 1. Ranked Channel

An ephemeral normalized record collected from the eligible Twitch ranking.

| Field | Type | Rules |
|---|---|---|
| `rank` | positive integer | 1 is highest; assigned after clipping eligibility filtering |
| `streamer_id` | positive integer | parsed from Twitch `user_id` |
| `login` | string | `user_login.lower()`; used by desired and online-state keys |

**Relationships**:

- Ordered into the current ranking used by `compute_desired_set()`.
- Contributes to `broadcaster_ids[login]`.
- Contributes to the streamer metadata batch keyed by `streamer_id`.
- Contributes one online-state refresh command in ranking order.

**Duplicate rules**:

- Repeated `streamer_id`: allowed in the ranking; the final occurrence supplies
  the metadata login.
- Repeated identical `login`: desired behavior remains login-keyed. The online
  snapshot contains that key once, the refresh pipeline may repeat it in
  ranking order, and lifecycle presence is evaluated once at its first/best
  rank.
- Different logins for one ID remain different desired/online keys; only their
  metadata row is deduplicated.

## 2. Previous Desired Set

The existing `DesiredSet` value read through `DesiredSetStore.read()`.

| Field | Type | Rules |
|---|---|---|
| `logins` | ordered list of normalized strings | best rank first from the prior atomic publication |
| `ids` | map `login -> streamer_id` | invalid stored IDs are already omitted by the store decoder |
| `generation` | non-negative integer | existing publication generation |

This remains the durable-in-Redis hysteresis input between polls and the only
identity source for departed-login offline events. Its storage layout and
bounded three-command read do not change.

## 3. Current Desired Intent

The existing login-keyed mapping returned by `compute_desired_set()`.

| Field | Type | Rules |
|---|---|---|
| login | normalized string | enters at `rank <= JOIN_THRESHOLD`; retained only while within `LEAVE_THRESHOLD` |
| rank | positive integer | current eligible ranking score |
| broadcaster ID | positive integer | supplied from the current login-to-ID map at publication |

**State transition**:

```text
not desired --current rank <= entry threshold--> desired
desired --current rank <= retention threshold--> desired
desired --missing or past retention threshold--> departed
```

The function, ordering, thresholds, previous-state read, and atomic
`DEL`/`ZADD`/`HSET`/`INCR` publication remain unchanged.

## 4. Departed Login

An ephemeral normalized login in the previous desired set but not in current
desired intent.

| Field | Type | Rules |
|---|---|---|
| `login` | string | prior desired login absent from current desired mapping |
| `previous_streamer_id` | optional positive integer | read only from the previous ID map |

A departure is known before online-state I/O. It is an offline-event candidate
only if its pre-refresh online-state entry is absent. Missing or invalid prior
identity suppresses the event and produces a data-integrity error.

## 5. Streamer Metadata Batch

An ephemeral identity-keyed set written to the existing Postgres `streamers`
table.

| Field | Type | Rules |
|---|---|---|
| `streamer_id` | BIGINT / positive integer | unique within the final values list |
| `streamer_login` | VARCHAR(255) / normalized string | value from the final occurrence of the ID in ranking order |
| `last_seen_at` | TIMESTAMPTZ | common Postgres `NOW()` value for the statement |

Existing table columns such as `first_seen_at`, `allows_clipping`,
`eventsub_refused_at`, and `clipping_disabled_at` are not changed by this
batch.

**Persistence rule**:

```sql
INSERT INTO streamers (streamer_id, streamer_login, last_seen_at)
VALUES (...), (...), ...
ON CONFLICT (streamer_id) DO UPDATE
SET streamer_login = EXCLUDED.streamer_login,
    last_seen_at = EXCLUDED.last_seen_at
```

The actual implementation uses `execute_values()` with one values placeholder
and `page_size` equal to the non-empty deduplicated row count.

**Transaction states**:

```text
not attempted (empty)
    |
    +-- non-empty --> statement dispatched --> commit acknowledged --> success
                                      |
                                      +-- row/statement/commit failure
                                              --> rollback attempted
                                              --> connection returned/discarded cleanly
                                              --> whole batch failed
```

There is no partial-success model and no per-row retry. The next poll rebuilds
the whole current batch.

## 6. Online-State Snapshot

An ephemeral map from normalized login to the state observed by one Redis
`MGET` before any current key is refreshed.

| Field | Type | Rules |
|---|---|---|
| `login` | string | ordered unique union of current ranked and departed logins |
| `raw_value` | string or `None` | Redis value; content is not used for lifecycle presence |
| `was_present` | boolean | `raw_value is not None` |

The snapshot is empty without Redis I/O only when both the current ranking and
departed set are empty.

**Lifecycle derivation**:

- current + absent + first rank inside entry boundary -> `online` candidate;
- current + absent + outside entry boundary -> refresh only;
- current + present -> refresh only;
- departed + absent + valid previous ID -> `offline` candidate;
- departed + present -> no event;
- departed + absent + missing/invalid previous ID -> integrity error, no event.

The snapshot remains immutable through refresh and publication.

## 7. Online-State Refresh Batch

The existing Redis `streamer:online:{login}` key update represented as one
pipeline execution.

| Field | Type | Rules |
|---|---|---|
| key | string | `streamer:online:{normalized_login}` |
| TTL | integer | existing `REDIS_STREAMER_TTL=180` |
| value | streamer ID | current ranking's broadcaster ID |
| order | ranking order | preserves final value for repeated login input |

No current records means no pipeline execution. Any connection/protocol error
or element-level error changes the entire phase result to failed, even if some
commands were applied.

## 8. Lifecycle Decision

An ephemeral event candidate derived from the immutable snapshot and published
only after refresh success.

| Field | Type | Online | Offline |
|---|---|---|---|
| `event_type` | enum | `online` | `offline` |
| `broadcaster_id` | positive integer | current ranked ID | previous desired ID |
| `broadcaster_login` | normalized string | current login | departed login |
| `rank` | integer | first/best current rank | existing sentinel `0` |
| `timestamp` | epoch seconds | publication time | publication time |

Kafka topic, keying, payload, producer behavior, and delivery callback remain
unchanged.

## 9. Metadata Failure Streak

In-process operational state mirrored to a Prometheus gauge and structured
logs.

| Field | Type | Rules |
|---|---|---|
| `consecutive_failures` | non-negative integer | increment after each failed non-empty metadata batch |
| `last_batch_size` | integer | log context only |
| `last_unique_size` | integer | log context only |

**Transitions**:

```text
initial = 0
failed non-empty batch: n -> n + 1
successful non-empty batch: n -> 0
empty batch: n -> n
```

The streak does not decide behavior; all current rows are retried naturally on
the next poll.

## 10. Poll Outcome

The final state used for logs and total-duration telemetry.

| Outcome | Meaning | Desired published? | Reconciler notified? |
|---|---|---:|---:|
| `success` | all attempted phases succeeded | yes | yes, when configured |
| `metadata_failed` | metadata failed, state and intent succeeded | yes | yes, when configured |
| `ranking_failed` | ranking fetch/filter failed | no | no |
| `desired_read_failed` | previous desired set unavailable | no | no |
| `online_snapshot_failed` | pre-refresh `MGET` failed | no | no |
| `online_refresh_failed` | refresh batch failed or reported an element error | no | no |
| `desired_publish_failed` | atomic intent acknowledgement failed/unknown | unknown | no |
| `unexpected_failure` | uncategorized guarded poll failure | no success claim | no success claim |

Phase timings use only `success`, `failure`, or `empty` labels. Final outcome
labels are a fixed enumeration, never exception text or login data.

## 11. Validation Fixture

A validation-only paginated ranking source.

| Field | Type | Rules |
|---|---|---|
| `scale` | enum | 500 or 900 eligible records |
| `fixture_id` | enum | A or B; A and B have disjoint IDs and logins |
| `raw_records` | ordered list | includes measured representative clipping-disabled proportion |
| `eligible_count` | integer | exactly equal to `scale` after filtering |
| `page_size` | integer | 100 |
| `page_delay_seconds` | float | at least same-environment live page p95 |
| `profile` | enum | stable or complete-turnover |

Test-only threshold/fetch overrides are recorded as evidence and never written
to production Compose configuration.

## Relationships and Invariants

```text
Ranked Channel ----> Current Desired Intent ----> atomic desired publication
       |                       |
       |                       +----> Departed Login
       |                                      |
       +----> Metadata Batch                  |
       |                                      |
       +------------------+-------------------+
                          |
                    Online Snapshot
                          |
                    Refresh Batch
                          |
                  Lifecycle Decisions
```

Global invariants:

1. Desired intent and departures exist before the online snapshot.
2. Metadata contains one final row per streamer ID.
3. Non-empty metadata uses one statement and one transaction completion.
4. Online lifecycle meaning comes only from the pre-refresh snapshot.
5. Lifecycle candidates are not published until refresh success.
6. Desired publication is not attempted after snapshot/refresh failure.
7. Reconciler notification follows only acknowledged desired publication.
8. Empty batches issue no metadata or refresh operation.
9. Existing thresholds, TTLs, key layouts, and schemas do not change.

# Validation Quickstart: Batched Poller Datastore I/O

This is the implementation-stage validation procedure designed by Stage 2.
Do not use it to change production thresholds. Integration and scale runs
belong on the separate production-equivalent machine with isolated test
state; this planning run does not start local Postgres, Redis, Kafka, Flink, or
the service.

## 1. Preconditions

Use the existing stream-monitoring virtual environment and pinned
dependencies:

```bash
cd services/stream-monitoring
source .venv/bin/activate
python --version
python -m pytest --version
```

Before acceptance runs, record:

```bash
git rev-parse HEAD
git diff -- docker-compose.yml requirements.txt Dockerfile
grep -E 'JOIN_THRESHOLD|LEAVE_THRESHOLD|CLIPPING_DISABLED_FETCH_BUFFER' \
  ../../docker-compose.yml
```

Expected production values:

```text
JOIN_THRESHOLD=150
LEAVE_THRESHOLD=300
CLIPPING_DISABLED_FETCH_BUFFER=120
```

There must be no requirement, Python image, schema, scheduler, EventSub policy,
Flink, or feature 005 change in the implementation diff.

The validation driver refuses implicit or production-like datastore targets.
Before any driver command, provide an explicitly isolated Redis database,
Postgres test database/schema namespace, operator-assigned run ID, and the
production-equivalent host adapter:

```bash
export TEST_REDIS_URL='redis://.../15'
export TEST_POSTGRES_URL='postgresql://.../twitch_test'
export FEATURE006_NAMESPACE='feature006-acceptance'
export FEATURE006_RUN_ID='feature006-operator-run-id'
export FEATURE006_RUNTIME_FACTORY='feature006_environment:build_runtime'
```

Every driver invocation must also include `--confirm-isolated-targets`. The
flag is an explicit operator acknowledgement; it does not weaken the driver's
tokenized rejection of production-like host, database, or namespace names.

The runtime factory is the deployment adapter on the separate validation
machine. It receives the parsed command arguments and supplies the real
initialized process/client callbacks requested by that command. The tracked
driver owns target validation, fixtures, dispatch proxies, measurements,
acceptance calculations, and JSONL validation; the adapter owns credentials
and references to that machine's isolated process. Missing targets or a
missing factory fail before any datastore or Twitch call.

| Command | Runtime adapter surface |
|---|---|
| `operation-counts` | `run_operation_count(case, scale, fixture, counter)` |
| `calibrate` | `get_streams()` or `fetch_live_page()`, plus `redis_round_trip()`, `postgres_round_trip()`, and `observed_disabled_proportion()` |
| `poll-profile` | `prepare_profile_state(profile, fixture, opposite_fixture)` and `run_profile_poll(fixture)` |
| `steady-state` | `wait_for_convergence(scale)`, the existing production pass callback as `production_pass_callback`, and `run_steady_state(...)` |
| `cold-start` | `initialize_cold_start(target)` and `run_cold_start(...)`, using the supplied recording transport, poll recorder, and scheduler callback |

Callbacks may be synchronous or asynchronous. The factory must delegate the
real production behavior unchanged; it must not shorten backoff, replace the
poll/reconciler policy, or return synthetic success evidence.

For operation counts and poll profiles, the adapter returns the actual
post-filter ranked count and effective entry/retention/fetch values from the
completed service invocation (the service exposes these in its immutable
`last_poll_result`). Returning fixture intent without observing the invocation
does not satisfy the driver. Operation-count callbacks must instrument the
supplied counter; a separate count map is accepted only when it exactly agrees
with that counter. The observed final outcome must be `success`; a caught
`metadata_failed`, `desired_publish_failed`, or other failed invocation is
excluded rather than counted as valid evidence.

## 2. Unit and Behavioral Validation

Run the targeted poller, desired-store, reconciler, metrics, and failure tests:

```bash
python -m pytest -q \
  test_desired_set_store.py \
  test_stream_monitoring.py
```

The feature-specific assertions must cover:

1. Hysteresis and desired ordering for empty, partial, stable, and completely
   changed rankings.
2. Desired membership and departures computed before the first online-state
   command.
3. One `MGET` for current-plus-departed state and one refresh pipeline for all
   current keys at 50, 500, and 900.
4. No metadata/snapshot/refresh operation for empty-current/empty-previous.
5. One snapshot but no metadata/refresh for departures-only.
6. Entry-band online, outside-band no-event refresh, stable channels,
   departed-present, departed-absent, missing prior ID, login changes, and
   duplicate IDs/logins.
7. Snapshot, refresh transport, refresh element, desired-read,
   desired-publish, metadata, and failed-refresh-expiry recovery behavior.
8. Metadata-only failure continuing healthy intent work with a distinct final
   outcome and increasing failure streak.
9. Production Compose values staying 150/300/120.

Operation-count assertions must be phase-specific. Do not infer SQL statement
count from one call to a batch helper.

## 3. Real-Postgres Validation

Point `TEST_POSTGRES_URL` only at the existing explicit test database or
another intentionally selected non-production Postgres. The fixture must
create and verify an isolated schema before touching `streamers`.

```bash
export TEST_POSTGRES_URL='postgresql://.../twitch_test'
python -m pytest -q test_stream_monitoring.py \
  -k 'StreamerMetadataBatchAgainstPostgres'
```

The real-Postgres group must prove in one run:

- 900 input rows dispatch one metadata `cursor.execute()` and one commit;
- every unique row is inserted;
- existing rows update;
- changed logins persist;
- duplicate streamer IDs store the last ranking occurrence;
- every affected `last_seen_at` advances and the batch shares one timestamp;
- a deliberately overlong or otherwise invalid login rolls back the entire
  batch;
- the pooled connection is immediately reusable after rollback;
- correcting the poison input and invoking the next-poll path retries and
  stores the complete batch.

Fixture setup, teardown, and verification SQL must not be included in the
measured metadata dispatch count. The connection/cursor proxy resets its
counter immediately before the production batch helper.

## 4. Datastore Dispatch-Count Gate

Run the validation driver against isolated Redis/Postgres namespaces:

```bash
python phase5/feature006_driver.py operation-counts \
  --confirm-isolated-targets \
  --scales 50 500 900 \
  --output /tmp/feature006-operation-counts.jsonl
```

For equivalent non-empty polls, require identical counts:

| Phase | 50 | 500 | 900 |
|---|---:|---:|---:|
| Metadata SQL statement | 1 | 1 | 1 |
| Metadata transaction completion | 1 | 1 | 1 |
| Online snapshot | 1 | 1 | 1 |
| Online refresh batch execution | 1 | 1 | 1 |

Record the existing clipping-eligibility SQL, desired read commands, and
desired publication batch separately. They must remain bounded and equal by
scale. Each non-empty record must also report the actual eligible count and
effective test-only values; 500/900 fixture labels do not substitute for
observing 500/900 processed records. Any unexpected nonzero datastore
boundary, including a per-channel `EXISTS`, `SETEX`, or extra SQL dispatch,
fails the record.

Also run:

```bash
python phase5/feature006_driver.py operation-counts \
  --confirm-isolated-targets \
  --case empty-empty \
  --case departures-only \
  --output /tmp/feature006-empty-counts.jsonl
```

Expected online/metadata omissions:

```text
empty-empty:      metadata=0 snapshot=0 refresh=0
departures-only:  metadata=0 snapshot=1 refresh=0
```

Both cases still publish desired intent atomically.

## 5. Live Ranking and Datastore Calibration

Run calibration on the same host, network, credentials, and datastore
endpoints used for the performance profiles:

```bash
python phase5/feature006_driver.py calibrate \
  --confirm-isolated-targets \
  --minimum-page-samples 20 \
  --output /tmp/feature006-calibration.jsonl
```

Calibration must:

1. Consume live `get_streams(first=100)` pages and timestamp page response
   intervals.
2. Collect at least 20 completed page observations.
3. Compute nearest-rank p95 and set fixture page delay to that value or higher.
4. Measure at least 20 harmless Redis and Postgres request round trips
   separately.
5. Record datastore medians; an acceptance run is valid only when each median
   is 40-110 ms.
6. Record the observed clipping-disabled proportion used to size raw fixtures.

For each scale, the driver then records:

```text
raw record count
disabled count/proportion
eligible count (must equal exactly 500 or 900)
page count and page size 100
live page p95 and chosen fixture delay
page_count * delay ranking budget
5,000 ms or 10,000 ms minus ranking budget
```

Do not substitute a local zero-latency generator or production Prometheus
scrape for this calibration.

## 6. Controlled Poll-Duration Profiles

Fixture A and fixture B must be disjoint in both login and broadcaster ID.
Raw fixture rows include the calibrated disabled proportion and use
measurement-only threshold/fetch overrides that leave exactly the nominal
eligible count.

Stable profile setup:

- repeat fixture A;
- seed previous desired intent with A;
- ensure all current online keys are present before each timed poll.

Complete-turnover setup:

- alternate A and B every poll;
- seed previous desired intent with the opposite fixture;
- remove incoming and departed online keys outside the timed interval before
  each poll so the poll includes maximum permitted online/offline output.

Run:

```bash
for scale in 500 900; do
  for profile in stable complete-turnover; do
    python phase5/feature006_driver.py poll-profile \
      --confirm-isolated-targets \
      --scale "$scale" \
      --profile "$profile" \
      --warmups 1 \
      --measured-polls 20 \
      --calibration /tmp/feature006-calibration.jsonl \
      --output "/tmp/feature006-${scale}-${profile}.jsonl"
  done
done
```

Timing begins immediately before ranking retrieval and ends after desired
intent publication and reconciler notification. It includes ranking
pagination/filtering, datastore phases, lifecycle publication, and desired
publication. State preparation and scheduler queue time remain outside.

Sort the 20 completed durations ascending and use item 19 (one-based) as
nearest-rank p95. Replace a whole poll that encounters an excluded ranking,
datastore, or broker failure; never subtract failed time.

Every completed measured poll must report the actual post-filter eligible
count and effective test-only entry, retention, and fetch-buffer values. The
driver rejects the profile if any count differs from the nominal scale or if
the effective configuration differs from the fixture. Warm-up and all 20
measured outcomes must be `success`; the `excluded` flag cannot convert a
failed bounded outcome into a completed sample.

Acceptance:

| Scale | Profile | p95 | Additional gate |
|---|---|---:|---|
| 500 | stable | `< 5,000 ms` | exactly 500 eligible |
| 500 | complete turnover | `< 5,000 ms` | exactly 500 eligible |
| 900 | stable | `< 10,000 ms` | no individual poll reaches 120 s |
| 900 | complete turnover | `< 10,000 ms` | no individual poll reaches 120 s |

Every profile must report zero overlap skips.

## 7. Thirty-Minute Reconciler Schedulability

Use the production-equivalent process with the real reconciler and transport.
Begin timing only after subscription count has converged to desired count.

The driver must compose, not replace, the existing pass callback:

```text
active_stream_count.set(count)
record(time.monotonic_ns(), count)
```

It must also attach APScheduler listeners for executed, error, missed, and
maximum-instance events.

Run separately:

```bash
python phase5/feature006_driver.py steady-state \
  --confirm-isolated-targets \
  --scale 500 --minutes 30 \
  --output /tmp/feature006-steady-500.jsonl

python phase5/feature006_driver.py steady-state \
  --confirm-isolated-targets \
  --scale 900 --minutes 30 \
  --output /tmp/feature006-steady-900.jsonl
```

Acceptance:

- all scheduled polls complete;
- completed `poll_streams` scheduler events cover the full 120-second cadence
  across the run (an empty scheduler event list is invalid);
- no maximum-instance/overlap skip occurs;
- every pass completion is recorded directly in process;
- convergence and every pass callback report the requested 500/900
  subscription count;
- leading and trailing run-boundary gaps are included with adjacent pass gaps;
- maximum adjacent completion gap is at most 15 seconds at 500;
- maximum adjacent completion gap is at most 20 seconds at 900.

The 15-second Prometheus scrape interval may corroborate the run but cannot
calculate these gaps.

## 8. Cold-Subscription 900 Backoff Run

Use an isolated validation Twitch account/environment where deleting and
recreating all subscriptions is intentional. Do not perform this against a
production subscription set.

Start the evidence interval only after:

- process initialization completed;
- Postgres and Redis pools/connections are warm;
- the EventSub transport is started;
- actual subscription count is confirmed as zero;
- desired fixture contains exactly 900 IDs.

Run:

```bash
python phase5/feature006_driver.py cold-start \
  --confirm-isolated-targets \
  --scale 900 \
  --require-rate-limit-backoff \
  --output /tmp/feature006-cold-900.jsonl
```

The recording transport proxy delegates the real create/list/delete methods
unchanged and timestamps:

- each accepted create;
- each `RateLimitedError`;
- each logged backoff interval;
- subscription coverage before and after accepted windows.

At the same time, record every scheduled poll start/end and APScheduler
overlap event.

Acceptance:

- at least one subscription-create backoff occurs;
- every scheduled poll starts without overlap skip;
- every recorded poll interval has one completed `poll_streams` scheduler
  event, with no missed/error event;
- every non-failing poll completes under 10 seconds;
- subscription coverage increases after every retry window in which Twitch
  accepts creates;
- retry/backoff/concurrency/round configuration equals production;
- no requirement is imposed on last-success advancement while the one long
  pass is still in progress;
- eventual final convergence remains conditional on existing policy and
  external acceptance.

## 9. Failure and Recovery Evidence

Preserve structured logs and metric snapshots for forced:

- previous desired-set read failure;
- online snapshot failure;
- refresh transport/unknown failure;
- acknowledged element-level refresh error;
- online-key expiry before the recovery poll;
- desired publication failure/unknown acknowledgement;
- metadata row/statement/commit failure;
- repeated poison-record metadata failure;
- missing prior broadcaster ID.

For every case, assert the prohibited lifecycle, intent, notification, and
success signals are absent. Then run a healthy poll and prove recovery from
the state actually visible at that time.

## 10. Deployment and Rollback Check

Deploy at the unchanged production values:

```bash
docker compose up -d --force-recreate stream-monitoring
```

`restart` is insufficient for bind-mounted files whose inode changed.

Before any later ramp, inspect total/phase timing, metadata failure streak,
poll overlap events, reconciler pass gaps, EventSub occupancy, Kafka/Flink lag,
and datastore RTTs at 150/300/120. The code deployment and channel-count ramp
are separate decisions.

Rollback requires only reverting the feature revision and recreating
`stream-monitoring`. There is no schema, dependency, threshold, Redis layout,
EventSub policy, or Flink rollback.

## 11. Stage 4 Evidence Status

The local implementation gate creates and tests the opt-in Postgres cases,
deterministic fixtures, dispatch proxies, evidence builders, and all five
driver commands without contacting infrastructure.

The following execution tasks remain explicitly deferred to the separate
production-equivalent machine:

```text
T026       real-Postgres atomicity, rollback, reuse, and next-poll retry
T076       isolated 50/500/900 operation-count and empty-case runs
T077       live calibration and four 20-poll duration profiles
T078       separate 30-minute 500/900 steady-state sessions
T079       isolated 900-channel real-backoff cold-start session
T090-T091 unchanged-value deployment evidence and one-revision rollback evidence
```

Do not mark any of these tasks complete from unit tests, fixture calculations,
help output, or a locally generated record. Completion requires the external
evidence described in the corresponding sections above.

# Stream Scout Operations Guide

This guide covers the full restart procedure, per-service restart steps, and troubleshooting. For the short version, see `QUICKSTART.md`.

## Prerequisites

1. **Docker** installed and running. Check with `docker info`.
2. **Environment variables** in a `.env` file in the project root:
   ```
   TWITCH_CLIENT_ID=your_client_id_here
   TWITCH_CLIENT_SECRET=your_client_secret_here
   ```
3. **Twitch tokens** in `./secrets/twitch_user_tokens.json`. Run `python seed_twitch_tokens.py` if this file is missing. The token must carry **two scopes**, and `seed_twitch_tokens.py` asks for exactly these:
   - **`user:read:chat`** — the EventSub `channel.chat.message` subscription, which is how chat arrives. Without it every subscription refuses, and the service logs that at ERROR on start-up rather than going quiet.
   - **`clips:edit`** — clip creation, shared with the Flink job.

   `chat:read` is **no longer required and no longer requested**. It was the IRC scope, and IRC was removed in spec 004 Phase 3. A token seeded before that still works, because a superset is fine, but re-seeding drops it.

---

## How the Flink job runs

The Clip Detector job runs under Flink Application Mode. There is no separate "submit the job" step, ever. The job's code runs as part of the `flink-jobmanager` container's own startup — starting that container **is** starting the job. `docker-entrypoint-job.sh` does this by launching `standalone-job.sh` with `--job-classname org.apache.flink.client.python.PythonDriver`, Flink's own entry point for running a Python job this way.

This has one important consequence: **the job's lifecycle and the container's lifecycle are the same thing.** If the job fails to start, the whole `flink-jobmanager` container exits — it does not stay up in a broken state. If the job later reaches any terminal state (cancelled, finished, or failed for good), the container exits then too. Docker's `restart: unless-stopped` policy brings the container back afterward, which starts the job fresh.

This applies to a manual `flink cancel` too, not just a crash. Cancelling the job restarts the whole container, not just the job.

There is nothing to submit by hand and no `-pyFiles` command to run. To restart the job, restart the container:
```bash
docker compose restart flink-jobmanager
```

If you are restarting to pick up a **code change**, use `--force-recreate` instead — a plain restart does not re-resolve a single-file bind mount. See "Note on image rebuilds" below.

`-pyFiles` (here, `-pyfs`) comes from the `FLINK_PYFILES` environment variable in `docker-compose.yml`, not hardcoded in the entrypoint script. Changing which files it lists needs no image rebuild — see "Adding a new Python module" below.

---

## How chat ingestion runs

Chat arrives over **EventSub websockets**. There is no IRC client, no chat rooms
to join, and no JOIN rate limit — all of that was removed in spec 004 Phase 3.

Two pieces do the work, and **both live inside the one `stream-monitoring`
container**:

- **The poller** runs on the APScheduler tick. It ranks the top live streams,
  applies the `JOIN_THRESHOLD` / `LEAVE_THRESHOLD` hysteresis band, and writes
  the wanted channel set to Redis (`chat:desired`, plus the login-to-id map in
  `chat:desired:ids`). It does no network work for subscriptions and its
  duration does not grow with the size of the change.
- **The reconciler** is an **asyncio task in the same process**, started next to
  the poll job. It reads that wanted set and drives the live subscriptions
  toward it — creating and deleting concurrently, up to `RECONCILE_CONCURRENCY`
  (default 10) at a time. It wakes when the poller bumps the generation counter,
  or every 5 s, whichever comes first.

**There is no separate container or service to start, stop, or check.** If
`stream-monitoring` is up **and it had a usable user token at start-up**, the
reconciler is up. That qualifier matters: with no token file, or one that is
expired, scope-reduced or hand-edited, the service deliberately keeps running
with the poller alone and **no reconciler at all** — it logs `No user
authentication, so no chat transport and no reconciler` at ERROR and `/health`
still returns OK. A green container is therefore not by itself proof that chat
is being ingested; `eventsub_subscription_count` is. Restarting the container
restarts the reconciler, and it converges from whatever state it finds —
existing subscriptions are adopted, not duplicated.

Every few minutes (`RECONCILE_READOPT_INTERVAL_SECONDS`, default 300) it also
re-lists the subscriptions from Twitch rather than trusting the set it holds in
memory. That is the backstop for a subscription lost by a route the pool cannot
observe — the known one is the library's reconnect re-subscribing part way and
giving up — where the count would otherwise keep reporting a channel that no
longer exists.

Subscriptions are spread over a pool of websocket connections, **300
subscriptions per connection** (Twitch's cap). Feature 007 requires two
subscriptions per monitored channel: `channel.chat.message` and
`channel.chat.notification`. One session can therefore hold at most 150
complete channel pairs.

**This transport tops out at 900 subscriptions, not 900 dual-covered
channels.** Twitch allows a maximum of **3 websocket connections with enabled
subscriptions** per client-id/user-id pair, at 300 each. Feature 007 sets a firm
400-channel ceiling: 400 channels use 800 subscriptions and reserve the
remaining 100 globally for reconnect and adoption safety. The pool refuses to
open a fourth connection rather than let Twitch reject subscriptions one by
one, so exhaustion shows up as `pool is at its 3-connection limit`. See
"Feature 007: gift/raid suppression operations" below.

### Reading the reconciler metrics

All of these are on the stream-monitoring metrics endpoint, port **9100**:

```bash
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_|reconcile_|subscription_create)'
```

| Metric | Read it as |
|---|---|
| `eventsub_subscription_count` | Live subscriptions held, in **subscription** units. With complete Feature 007 coverage it should be twice `ZCARD chat:desired`, up to 800; use `eventsub_channel_coverage`, not this gauge, for channel counts. It moves DURING a pass, not only at the end: it steps up as a cold start creates, and drops the moment a socket loss is reported |
| `reconcile_last_success_timestamp` | Unix time of the last pass that **ran to completion**. **This is the stalled-reconciler alarm.** If it stops advancing while polls keep succeeding, the reconciler is stuck and the subscription set is frozen — the poller cannot tell you this, because it still works. Two things it does **not** mean: it is not "a pass with no failures" (at 500 channels one broadcaster refuses every pass, so gating on that would freeze the gauge and destroy the signal — per-channel failures are `subscription_create_failures_total`); and **a gap of a few minutes during a cold start is normal, not a stall**. A pass that hits 429s backs off and retries inside the pass, up to `RECONCILE_MAX_RETRY_ROUNDS` × `RECONCILE_RATE_LIMIT_BACKOFF_SECONDS` ≈ 200 s at the defaults, and the stamp only lands when the pass ends. Before restarting anything, check `reconcile_duration_seconds` and whether the subscription count is still climbing |
| `reconcile_duration_seconds` | Histogram of pass duration. A converged pass is milliseconds. Buckets run to 120 s because a cold start to 500 channels takes ~51 s |
| `subscription_create_failures_total` | Counter, labelled by `reason`. **No series at all is the healthy state**, not a broken exporter: this client registers a labelled series on its first increment |
| `eventsub_connection_occupancy{connection}` | Subscriptions per connection. None may exceed 300 |

---

## Ramping the monitored channel count

The ladder below records the historical single-subscription ramp through
feature 006. Feature 007 supersedes its operating point with a firm
`JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=400` ceiling because every channel now
uses two subscriptions. Do not use the historical 800/900 result to raise a
feature-007 deployment; follow the dedicated rollout and rollback below.

### What `JOIN_THRESHOLD` and `LEAVE_THRESHOLD` actually do

`LEAVE_THRESHOLD` sets the monitored count, not `JOIN_THRESHOLD`. A channel
enters the set on reaching the top `JOIN_THRESHOLD` by viewer rank and stays
until it drops out of the top `LEAVE_THRESHOLD`. So 800 / 900 monitors roughly
720–800 channels (the top 900 minus the ~20% with clipping disabled), with an
800-deep entry gate that keeps a channel near the boundary from flapping in and
out. To run near a round number of channels, set `LEAVE_THRESHOLD` to it and
`JOIN_THRESHOLD` to something below.

### How to change the thresholds

The poller reads both from the environment. `JOIN_THRESHOLD` must not exceed
`LEAVE_THRESHOLD`; equality is valid and deliberately used by feature 007.

1. Set both variables in the `stream-monitoring` `environment:` block in
   `docker-compose.yml`.
2. Restart only that container:
   ```bash
   docker compose up -d --force-recreate stream-monitoring
   ```
3. The reconciler converges to the new set over the next few passes. A larger
   set costs a longer cold start (about 2 minutes to ~480 channels), not a
   failed one.

`CLIPPING_DISABLED_FETCH_PAD_FRACTION` (default 0.30) pads the Helix fetch so
the ~20% of top channels with clipping disabled do not shrink the candidate
pool below `LEAVE_THRESHOLD`. It is a fraction of `LEAVE_THRESHOLD`, so it
scales with the threshold on its own. Raise it if the log line `Fewer than
LEAVE_THRESHOLD clip-allowed streams found even after padding fetch` appears.

### The step ladder

| Step | `JOIN_THRESHOLD` / `LEAVE_THRESHOLD` | Channels monitored | Result |
|---|---|---|---|
| 0 | 15 / 30 | ~20 | baseline |
| 1 | 50 / 100 | ~55 | clean |
| 2 | 150 / 300 | ~290 | clean |
| 3 | 300 / 500 | ~320 | clean |
| 4 | 480 / 500 | ~485 | clean |
| 5 | 800 / 1000 | ~800 | **did not converge, rolled back** (pre feature 006) — see below |
| 6 | 800 / 900 | ~720–800 | clean on 2026-08-31, post feature 006 — historical single-subscription result |

Soak each step for at least 30 minutes before the next.

Step 6 cold start showed the expected 429 backoff burst (four rounds,
~333 → 22 rate-limited channels, cleared in ~2 minutes) and then converged.
Feature 006's batched poll phases keep `stream_poll_duration_seconds` near 3 s
at 900 ranked, so the poll no longer stalls the way step 5 did.

### What to check at each step

| Signal | Healthy | Stop and investigate |
|---|---|---|
| `eventsub_subscription_count` vs `ZCARD chat:desired` | historical single-subscription transport: equal; Feature 007 complete coverage: twice the desired count, within one pass interval | a gap that does not close |
| `reconcile_duration_seconds` | a converged pass is milliseconds at ~50 channels, ~2–4 s at 300–485 | the median grows faster than the channel count |
| `subscription_create_failures_total{reason="429"}` | absent, or brief cold-start bursts all retried | keeps rising after convergence |
| `subscription_create_failures_total{reason="transient_session"}` | a cold-start burst (seen up to ~70), then static | keeps rising during steady state |
| `eventsub_connection_occupancy` | no connection over 300; the pool grows at the cap | `pool is at its 3-connection limit` (the 900-subscription hard cap) |
| Kafka producer lag, Flink source watermark lag | flat | either grows and does not recover |
| Flink TaskManager heap | flat (measured ~2.6 GB RSS through 485 channels, cap 6 GB) | approaches the cap |
| Flink TaskManager thread count | flat (~135) | climbing — `ClipCreator` pileup |
| `clips_created_failed_total` by reason | `api_error` / `metadata_fetch` only, scaling with volume | a `rate_limited` reason appears |

### What the 2026-08-30 ramp found

The ladder was walked 15/30 → 50/100 → 150/300 → 300/500 → 480/500, ~25 min
soak each. **The system stayed clean through ~485 real channels.** Subscriptions
tracked the desired set on every sample, TaskManager RSS held flat at ~2.6 GB,
thread count held at ~135, and clip creation never returned a rate limit — every
create logged `status=202`.

**The clip ceiling was not reached, contrary to `research.md` §1/§3.** The
corpus figure of ~2.2 detections per broadcaster-hour came from the top ~72
channels, which are far hotter than the rest. The measured rate across ~485
channels was **~0.3–0.5 per broadcaster-hour** — the rank 150–500 band is much
quieter (`research.md` §1: median 32 messages per 60 s). At ~485 channels that
is roughly 150 clip attempts per hour, well under any Twitch per-account limit.

So the clip budget is a real constraint but a distant one — and the 800/1000
attempt below hit the poller's ceiling well before the clip budget's. **The
`rate_limited` reason on `clips_created_failed_total` is the trigger for spec
005** — anomaly ranking against a scarce clip budget. It did not appear at any
tested channel count.

Two rough edges, neither blocking:

- **Cold-start transient burst.** Every `--force-recreate` throws a burst of
  `transient_session` failures (11 to 71, non-deterministic) as the first
  websocket session reconnects under the create load. The reconciler recovers
  every one on the next pass. Classified apart from real failures since
  2026-08-30.
- **Reconcile pass duration** grows from milliseconds at 50 channels to ~2–4 s
  at 300–485. Still well inside the 5 s idle interval, but watch it past 500.

### Historical 800/1000 attempt before datastore batching

A jump to `JOIN_THRESHOLD` 800 / `LEAVE_THRESHOLD` 1000 (~800 channels) did
**not** converge and was rolled back after ~10 minutes. Two limits bind before
the transport's 900-connection cap does:

- **The poll job stalls.** `poll_top_streams` does one Helix page fetch, one
  Postgres `INSERT ... ON CONFLICT`, and one Kafka lifecycle publish *per
  channel*. Postgres is remote (over Tailscale), so ~800 upserts per poll plus
  ~12 Helix pages plus hundreds of lifecycle publishes pushed one poll past
  100 s. APScheduler runs the poll `max_instances=1`, so the next poll was
  `missed by 0:01:44`. A delayed poll stops refreshing `streamer:online:*`
  (180 s TTL) and streamers start to flap. This is the FR-003 "poll never
  blocks" guarantee breaking at scale — the per-channel remote write in the
  poll loop is the bottleneck, and fixing it is a design change, not a config.
- **The reconciler cannot outrun the 429 budget.** Twitch's create burst
  budget is ~360–420 per token (`research.md` D2). 800 channels from cold is
  twice that, so a pass drains the budget, backs off, drains again, and takes
  200 s+ while `reconcile_last_success_timestamp` sits frozen well past the
  "normal cold start" window.

This result predates feature 006's batched metadata and online-state phases.
It remains the reason a later ramp needs feature 006 acceptance evidence; it
is not evidence that the deployed thresholds should change with the code.
Subscription-create policy and the external 429 budget remain unchanged.

### Feature 006 deployment and evidence gate

Deploy the batched poller as a code-only change at the existing production
values:

```text
JOIN_THRESHOLD=150
LEAVE_THRESHOLD=300
CLIPPING_DISABLED_FETCH_PAD_FRACTION=0.30
```

(Feature 006 shipped at 150 / 300; production moved to 800 / 900 the next day —
step 6 above — once the batched poll phases were confirmed healthy.)

Recreate only the existing service so Docker resolves the replaced bind-mounted
file:

```bash
docker compose up -d --force-recreate stream-monitoring
```

Do not combine this deployment with a channel-count ramp. Before considering a
later ramp, collect the separate production-equivalent evidence in
`specs/006-batch-poller-io/quickstart.md` against explicitly isolated Redis,
Postgres, and Twitch state. That evidence includes the real-Postgres rollback
case, 50/500/900 dispatch counts, four calibrated duration profiles, separate
30-minute steady-state runs, and the isolated 900-channel cold-backoff run.

At the current operating point, retain:

- `stream_poll_duration_seconds{outcome}` and
  `stream_poll_phase_duration_seconds{phase,outcome}`;
- `stream_metadata_consecutive_failures`;
- poll overlap/misfire events and direct reconciler pass-completion gaps;
- EventSub subscription count and connection occupancy;
- Kafka/Flink lag and datastore round-trip measurements.

Every invocation emits one `Poll finished` log with a bounded `outcome`, phase
durations, ranked/desired/entered/left counts, and metadata batch context.
`metadata_failed` means intent work continued but metadata is stale; any other
failure outcome is not a successful poll.

Only after that evidence is reviewed should a later threshold ramp be approved
as its own operational change. Feature 006 does not alter the reconciler
cadence, EventSub capacity, create concurrency, retry/backoff policy, online
TTL, poll interval, schema, dependencies, Kafka/Flink behavior, or feature 005.

To roll back the code deployment, revert only the feature 006 revision and run:

```bash
docker compose up -d --force-recreate stream-monitoring
```

No schema, dependency, Redis layout, threshold, EventSub-policy, or Flink
reversal is required. If a later ramp has happened, decide its threshold
rollback separately rather than coupling it to the code rollback.

### Rolling back a pre-007 single-subscription step

This historical procedure applies only when no
`channel.chat.notification` subscriptions exist. Lower both numbers and restart
`stream-monitoring`; the reconciler drops the now-unwanted subscriptions on its
next pass. For Feature 007, use the capacity-safe rollback order below and
**never raise either threshold above 400 while any notification subscription
remains**.

---

## Feature 007: gift/raid suppression operations

This section is the operator runbook for
`specs/007-suppress-gift-raid-bursts`. Live validation is performed by an
operator on the configured machine. Local tests, fixtures, static assertions,
and replay output do **not** establish deployed evidence E1-E5.

### Capacity and coverage contract

- The firm monitored-set ceiling is **400 channels**, enforced by
  `JOIN_THRESHOLD=400` and `LEAVE_THRESHOLD=400`. Do not hide a different
  hysteresis policy in code.
- Each monitored channel needs two independent subscriptions:
  `channel.chat.message` for chat and `channel.chat.notification` for gift/raid
  notices. At the ceiling this is 400 channels × 2 = **800 subscriptions** of
  the 900-subscription account limit, leaving **100 subscriptions of global
  reconnect/adoption headroom**.
- Each websocket session is capped at 300 **subscriptions**, so it can hold at
  most **150 complete channel pairs**. A pair may be split across sessions.
- Keep units explicit: `active_stream_count` and
  `eventsub_channel_coverage` count **channels**;
  `eventsub_subscription_count` and `eventsub_connection_occupancy` count
  **subscriptions**.

Coverage is a per-channel derived state:

| State | Meaning and operator interpretation |
|---|---|
| `complete` | Both subscriptions are live. Compare this channel count with `ZCARD chat:desired`; suppression coverage is available |
| `chat_only` | Chat is live but notification is missing and repair is eligible. Chat and clipping continue; suppression fails open until repair |
| `notification_only` | Notification is live but chat is missing. The reconciler repairs chat; no chat means no clip decisions for that channel meanwhile |
| `degraded_chat_only` | Twitch refused the auxiliary notification subscription while chat remained live. Chat is deliberately retained and suppression fails open; the retry hold-off is active |

An auxiliary refusal starts a
`AUXILIARY_REFUSAL_RETRY_SECONDS=3600` hold-off so the reconciler does not
hot-loop. On expiry the state returns to `chat_only` and normal repair resumes.
A websocket reconnect or connection retirement makes it eligible immediately;
a successful create or 409 adoption clears the hold-off. It never writes the
seven-day whole-channel refusal that would evict chat.

`desired_set_churn_total` increments by **entered channels + departed channels**
after each successful desired-set publication. NFR-007 requires the average to
be **at most 8 membership changes per poll** over a full deployed 24-hour
observation at 400/400. Read a counter delta, not its lifetime value, and divide
the 24-hour delta by the number of successfully published polls in the same
window (`Poll finished` provides `entered`, `left`, `desired`, and outcome
context). Above 8 blocks suppression gating. The only resolution is a
specification change to a narrower join threshold inside the firm 400 ceiling,
never an undocumented configuration or code workaround.

### Suppression topic and policy

`kafka-init` creates `suppression-events` before the Flink job starts. The topic
has **four partitions**, replication factor one, and **one-hour retention**
(`retention.ms=3600000`). Its Kafka key is the UTF-8 string form of
`broadcaster_id`, and the producer verifies that it agrees with the payload.

Version 1 carries required `schema_version=1`, integer `broadcaster_id`,
`notice_type`, and Twitch-clock epoch-millisecond `occurred_at_ms`. Optional
`notice_id`, `received_at_ms`, and raid `viewer_count` are diagnostic only.
Records carry notices, not computed deadlines or window policy.

Only `community_sub_gift`, `sub_gift`, and `raid` trigger suppression.
`unraid`, plain `sub`, `resub`, and every other category are excluded. Gift
notices use 120 seconds and raids use 180 seconds; raid viewer count never
scales the duration.

The active interval is notice-bounded and half-open:
`[occurrence, deadline)`. A peak before the notice remains eligible even when
reported later, a peak at the notice is suppressed, and a peak exactly at the
deadline is eligible. An extending overlap retains the earliest chain start
and the maximum candidate deadline; an earlier/equal candidate is a complete
no-op, and a notice at or after the old deadline starts a new interval.

The fixed future-time trust bound is 30 seconds, not an environment setting.
`occurred_at_ms <= consumer_receipt_ms + 30000` is accepted; accepted future
skew is clamped to delivery age zero and logged. A value even 1 ms farther
ahead is rejected as `reason="fields"` before delivery observation or
suppression-state access. Missing, malformed, late, or unavailable suppression
signals always **fail open**: normal clip eligibility continues, and an
already-emitted clip is never retracted.

`docker-compose.yml` deliberately checks in
`SUPPRESSION_GATING_ENABLED=false` in both Flink blocks. The code default is
`true`, so verify the deployed compose value and the startup log
`SUPPRESSION_GATING_ENABLED: False`; do not rely on an omitted variable.
Operators change both compose values to `true` only after the rollout gates
below pass.

### Signal reference

Counters reset with their process. Use Prometheus `increase(...[W])` or
`rate(...[W])`, not raw counter subtraction across restarts. Gauges are current
levels; histogram observations are per trusted record.

| Signal | Unit and exact interpretation |
|---|---|
| `clips_suppressed_total{broadcaster_id,notice_type}` | Counter of would-have-clipped decisions stopped by an active window, attributed to channel and the notice that moved the deadline. `increase(...[W])` is suppressions during `W`. `anomalies_detected_total` **still increments** for the same decision |
| `eventsub_channel_coverage{state}` | Gauge in **channels** for `complete`, `chat_only`, `notification_only`, and `degraded_chat_only`. Compare `state="complete"` with the Redis desired count; inspect partial/degraded series rather than inferring coverage from subscription totals |
| `eventsub_connection_occupancy{connection}` | Gauge in **subscriptions per websocket connection**, including the connection label. Every value must remain at or below 300 |
| `eventsub_subscription_count` | Gauge in **subscriptions across the pool**. Complete dual coverage is approximately 2 × desired channels and no more than 800 steady state |
| `active_stream_count` | Gauge in **channels** in the reconciler's actual set after a completed pass. It is not a subscription count; read it with desired count and coverage state |
| `suppression_notices_ignored_total{notice_type}` | Producer counter for deliberately excluded categories; known categories retain their name and unknown/absent categories use bounded `other`. Traffic here is not malformed |
| `suppression_notices_malformed_total{reason}` | Producer counter for trigger notices dropped without publication: bounded reasons `identity` or `occurred_at`. Any increase is a producer/input fault, not ordinary excluded traffic |
| `suppression_records_rejected_total{reason}` | Consumer counter for records ignored as `decode`, `schema_version`, or `fields`; over-30-second future timestamps are `fields`. Rejected records create no delivery sample or suppression state |
| `suppression_records_consumed_total{lag_class}` | Counter for trusted records actually consumed/applied. `healthy` means clamped age ≤30 s; `lagging` means >30 s. There is intentionally no `idle` series |
| `suppression_delivery_age_seconds` | Histogram, one observation per trusted record, in **seconds**, of `max(0, consumer receipt - occurred_at)`. Use its bucket/rate distribution for percentiles; rejected records and silence add no observation |
| `desired_set_churn_total` | Unlabelled counter in **channel membership changes**: entered + departed after successful publication. Its release reading is the 24-hour delta divided by successful polls, bounded at ≤8 changes/poll |

For a Prometheus window `W`, read delivery state in this order:

1. `sum(increase(suppression_records_consumed_total[W])) == 0` means
   **idle/unknown**, never healthy.
2. Otherwise,
   `sum(increase(suppression_records_consumed_total{lag_class="lagging"}[W])) > 0`
   means **lagging**.
3. Otherwise the received records in that window are **healthy**.

Age is computed only for trusted records as
`max(0, consumer_receipt_ms - occurred_at_ms)`: at or below 30 seconds is
healthy and above 30 seconds is lagging. Optional producer `received_at_ms` may
split the diagnosis into Twitch-to-producer and producer-to-consumer delay, but
it never changes classification. Complete coverage plus topic silence proves
only that subscriptions exist; it does not prove broker delivery health. This
feature cannot distinguish a stalled path from legitimate silence without a
real notice.

### Structured logs and diagnosis

Use the existing `docker logs` patterns in Parts 3 and 5 and Prometheus UI in
Part 2. The following exact message prefixes and fields separate the failure
domains:

| Message or signal | Meaning / fields / next check |
|---|---|
| `Twitch refused the chat-notification subscription, keeping chat and degrading suppression coverage for this channel` | Auxiliary refusal; fields include `broadcaster_id`, `retry_after_seconds`, and `error`. Expect `degraded_chat_only`, retained chat, and a bounded retry |
| `Chat-notification hold-off cleared, the channel is repairable again` | Recovery; `reason` identifies notification coverage, reconnect, retirement, or channel drop. Confirm transition through `chat_only` to `complete` |
| `Suppression notice dropped as untrustworthy` | Producer rejected a trigger before Kafka; inspect bounded `reason`, `notice_type`, and `notice_id`, and correlate with `suppression_notices_malformed_total` |
| `Failed to publish suppression event` / `Kafka delivery failed` | Producer/broker path failed after mapping. Chat remains live and the detector fails open |
| `Suppression notice refused for broadcaster ...` | Consumer rejected an occurrence beyond the fixed future bound; the line includes channel, type, occurrence, consumer receipt, bound, and `no deadline written`. Correlate with `suppression_records_rejected_total{reason="fields"}` and host clocks |
| `Suppression clock skew for broadcaster ...` | Accepted occurrence is ahead by no more than 30 s; age was clamped to zero and the notice was still applied |
| `Suppression delivery lag for broadcaster ...` | Trusted age exceeded 30 s; the line includes channel, type, and age. The notice is late but still applied |
| `CLIP SUPPRESSED for broadcaster ...` | Suppression decision; includes channel, notice type, peak, interval bounds, notice time, and intensity. It must pair with one `clips_suppressed_total` increment and no clip yield |
| `Error applying suppression notice for broadcaster ...` | Unexpected consumer exception. Treat as fail-open and inspect the traceback plus rejection/consumption counters |
| `Subscription operation failed` with `pool is at its 3-connection limit` | Capacity refusal, distinct from malformed or delivery failures. Inspect total subscriptions, per-connection occupancy, coverage, desired count, and thresholds |
| `Poll finished` | Churn context; `entered` + `left` is that successful publication's increment, with `desired` and `outcome` for the 24-hour calculation |

Decode, unknown-version, and ordinary field/category consumer rejections have
no dedicated payload log; distinguish them by
`suppression_records_rejected_total{reason}`. This avoids logging untrusted
payloads.

Troubleshoot in this order:

1. **Coverage missing:** compare desired channels with
   `eventsub_channel_coverage` and `active_stream_count`, then compare the
   expected two subscriptions per complete channel with total and per-connection
   occupancy. A partial state is coverage loss; `degraded_chat_only` plus the
   refusal log is bounded auxiliary refusal, not a broker-lag diagnosis.
2. **Producer input or publication:** ignored notices are policy; malformed
   notices indicate identity/time failure; publication/delivery errors indicate
   Kafka transport. All fail open.
3. **Consumer rejection:** split `decode`, `schema_version`, and `fields`.
   Future-time `fields` plus the refusal log requires a clock check; the record
   did not contribute a health sample or state.
4. **Delivery:** apply the three-state Prometheus reading above. Lagging records
   remain applied. Silence is idle/unknown; do not substitute complete coverage
   for delivery evidence.
5. **Decision:** correlate `CLIP SUPPRESSED`,
   `clips_suppressed_total`, and the still-incrementing
   `anomalies_detected_total`. If policy is suspect, turn gating off first.
6. **Capacity/churn:** verify ≤400 desired channels, ≤800 steady subscriptions,
   every connection ≤300, and the 24-hour churn average ≤8/poll. A 401st
   channel must be refused without consuming the 100-slot headroom.

### Forward rollout and pending evidence

Run these gates in order on the configured machine:

1. **B0 — current single-subscription revision:** set 400/400 using the
   threshold procedure above and wait for Redis `chat:desired` and
   `eventsub_subscription_count` to converge near 400. This is unconditionally
   safe at 400 × 1; E1 does not block this preliminary ramp-down.
2. **Deploy with gating off:** ensure `kafka-init` has created
   `suppression-events` **before** starting the feature Flink job. Deploy the
   feature revision with the checked-in `SUPPRESSION_GATING_ENABLED=false` and
   let dual coverage converge. Clip behavior remains pre-007.
3. **E1 — mixed types and cost:** enumerate live EventSub subscriptions, prove
   both types coexist on live sessions, and prove `total_cost=0` against
   `max_total_cost=10`. Non-zero cost blocks the feature; use the rollback below.
4. **E2 — capacity and churn:** prove 400 complete channels, 800 subscriptions,
   no connection above 300, at least 100 slots free, and refusal of the 401st
   channel. Observe 400/400 for a full 24 hours and require average desired-set
   entries + departures ≤8 per poll.
5. **E3 — sparse-source watermark:** keep the topic silent for at least one
   hour and prove chat detection/watermark behavior does not stall. Then observe
   one isolated notice after silence and prove detection resumes and the source
   re-idles within
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`.
6. **Enable, then E4:** only after E1, all of E2 including the 24-hour churn
   gate, and both E3 cases pass, change
   `SUPPRESSION_GATING_ENABLED=true` in both Flink compose blocks and recreate
   the Flink components using the existing procedure. Capture real gift and
   raid slices and verify mapping, trusted-record age, delivery classification,
   intended suppression, unaffected pre-notice/outside-window peaks, and the
   120/180-second defaults. Check and record NTP/clock synchronization on the
   producer and consumer hosts with approved host tooling: skew over 30 seconds
   makes notices reject and therefore fail open.
7. **E5 — rollback rehearsal:** exercise the exact capacity-safe order below.

Evidence remains pending until an operator records it:

| Evidence | Required deployed observation | Status |
|---|---|---|
| E1 | Live mixed subscription types; cost 0 of 10 | [ ] Pending operator run |
| E2 | 400/800 convergence, ≤300 per connection, 100 headroom, 401st refusal, and 24-hour churn ≤8/poll | [ ] Pending operator run |
| E3 | One-hour silence does not stall; isolated-notice hold stays within the documented re-idle bound | [ ] Pending operator run |
| E4 | Real gift/raid mapping, age and clock behavior, in-window suppression, and unaffected outside/pre-notice peaks | [ ] Pending operator run |
| E5 | Kill switch and capacity-safe rollback rehearsal | [ ] Pending operator run |

### Capacity-safe rollback

The governing invariant is absolute: **thresholds MUST NEVER be raised above
400 while any `channel.chat.notification` subscription exists.**

1. Set `SUPPRESSION_GATING_ENABLED=false` first. This alone is the complete
   response to a detection-policy-only incident; leave subscriptions and topic
   in place while diagnosing.
2. For a transport rollback, unwind or revert the dual-subscription revision
   while `JOIN_THRESHOLD` and `LEAVE_THRESHOLD` remain 400/400.
3. Wait until subscription enumeration contains no
   `channel.chat.notification`, `eventsub_subscription_count` is approximately
   the desired channel count (~400 rather than ~800), and coverage plus desired
   metrics are stable.
4. Only then raise thresholds back toward the prior single-subscription ramp.

The `suppression-events` topic may remain. There is no database schema, Redis
layout, token, or dependency rollback.

---

## Important: Postgres and Redis are remote

Postgres and Redis do **not** run on this machine. They run on the Tailscale host `streamer-summaries-api` (100.112.97.111). `docker-compose.override.yml` points `api-frontend`, `stream-monitoring`, and both Flink containers at that host.

The `postgres` and `redis` services in `docker-compose.yml` exist but stay off. They sit behind the `local-db` compose profile, so `docker compose up -d` does not start them.

This machine also runs two unrelated standalone containers named `postgres16` and `redis`. **Stream Scout does not use them.** Do not restart them or query them for Stream Scout data — they belong to a different project.

To check the remote database from this machine, do not run `psql` or `redis-cli` directly against a local container. Instead, check through the app:
```bash
curl http://localhost:5000/health
docker logs streamscout-api-frontend --tail 20
```
A working `/v1.0/clip` response or a "Database connection pool initialized" log line confirms the remote database is reachable. To restart Postgres or Redis, you need access to the `streamer-summaries-api` host — this guide does not cover that host.

---

## Database migrations (manual)

`infrastructure/postgres/init.sql` runs **only when the database is created**.
The deployed database on `streamer-summaries-api` was created long ago, so a new
column in `init.sql` never reaches it. Every schema change needs the equivalent
`ALTER TABLE` run by hand against the remote host, before the code that reads
the column is deployed. Code that queries a column the database does not have
throws on every call.

Run the statement from this machine with the stream-monitoring virtualenv, which
already has `psycopg2` and can reach the Tailscale host:

```bash
cd services/stream-monitoring
.venv/bin/python -c "
import psycopg2
conn = psycopg2.connect('postgresql://twitch:twitch_password@100.112.97.111:5432/twitch')
with conn, conn.cursor() as cur:
    cur.execute(open('/path/to/migration.sql').read())
"
```

Wrap every migration in one transaction. Postgres DDL is transactional, so a
`BEGIN ... COMMIT` block cannot half-apply and leave the schema in a state
neither the old nor the new code understands.

### Spec 004 Phase 2 — the two self-heal timestamps

Applied 2026-08-28 to the deployed database: 1,404 rows, 134 backfilled. This is
the **only** migration spec 004 needs; it is complete as written below. Adds `eventsub_refused_at` (the reconciler writes it when a
channel refuses the chat subscription) and `clipping_disabled_at` (the Flink job
writes it beside every `allows_clipping = FALSE`). Both carry the same 7-day
re-check, so a channel that fixes its settings stops being skipped forever.

```sql
BEGIN;
ALTER TABLE streamers ADD COLUMN eventsub_refused_at TIMESTAMPTZ;
ALTER TABLE streamers ADD COLUMN clipping_disabled_at TIMESTAMPTZ;
-- Backfill, so the rows already disabled do not all look "stale" (older than
-- 7 days, therefore due a retry) the moment the new code starts:
UPDATE streamers SET clipping_disabled_at = NOW() WHERE allows_clipping = FALSE;
COMMIT;
```

Note: the deployed `streamers` table declares its existing time columns as
`timestamp without time zone`, while `init.sql` says `TIMESTAMPTZ`. The two new
columns follow `init.sql`. Both types compare correctly against `NOW()`, which is
all the 7-day rule needs.

---

## Part 1: Full restart

The normal way to restart is the script:
```bash
cd ~/stream-scout
./start.sh
```
This stops all containers, starts them again, and waits for every container with a health check to report healthy. flink-jobmanager is one of them. It takes well under a minute in the common case.

It can take longer if a container is slow to start. Kafka's own health check allows up to about 150 seconds. flink-jobmanager's container does not even start until Kafka is healthy and `kafka-init` has finished creating the Kafka topics. Once flink-jobmanager's container does start, its own health check allows up to about 210 more seconds.

The script does not submit the Flink job, and never needs to. The flink-jobmanager container runs the job as part of its own startup — see "How the Flink job runs" above. If the job fails to start, the container itself never reports healthy, so a broken job shows up as a failed restart, not a silently-empty one.

**After it finishes, confirm the Flink job is running:**
```bash
docker exec streamscout-flink-jobmanager flink list
```
This should show one "Clip Detector Job (RUNNING)". If flink-jobmanager did not become healthy, the job failed to start — check `docker logs streamscout-flink-jobmanager` for the error (a Python traceback, most often).

**If `start.sh` is not available**, run the same steps manually:
```bash
docker compose down
docker compose up -d --wait --wait-timeout 500
docker exec streamscout-flink-jobmanager flink list
```

**Then verify:**
```bash
curl http://localhost:5000/health
```
Open http://localhost:8081 for the Flink dashboard, and http://localhost:3000 (`admin`/`admin`) for Grafana.

**Note on image rebuilds:** `start.sh` does not rebuild images. If you changed anything not bind-mounted into a container — `docker-entrypoint-job.sh`, the Dockerfile, `flink-conf.yaml` — rebuild it first:
```bash
docker compose build flink-jobmanager flink-taskmanager
```
The four `.py` job files and `secrets/` are bind-mounted instead. See the `volumes:` section for `flink-jobmanager` in `docker-compose.yml`. Editing those needs no rebuild.

**But a restart is not always enough — use `--force-recreate` after anything that replaces the file.** Those `.py` files are bind-mounted **individually**, and a single-file bind mount follows the **inode**, not the path. Docker resolves it when the container is *created*. So any edit that writes a new file in place of the old one — `git checkout`, `git switch`, `sed -i`, an editor that saves by write-and-rename — leaves the container holding the old inode. The host file reads the new content and the container keeps running the old, with no error anywhere:

```bash
docker compose up -d --force-recreate flink-jobmanager flink-taskmanager
```

Editing a file *in place* (appending, or an editor that truncates and rewrites the same inode) does survive a plain `docker compose restart`. Do not rely on knowing which kind of edit you just made.

**This has already bitten once.** After spec 004 Phase 3 merged, `stream-monitoring` went on running the Phase-2-era service — IRC client still in it — for hours, because the branch checkout replaced `stream_monitoring_service.py` and the container kept the old inode. It was found only by comparing checksums. See `specs/004-eventsub-parallel-reconciler/research.md`, "Deployment trap found while verifying".

**Verify the deploy rather than assuming it.** Read the value back out of the running container:
```bash
docker exec streamscout-stream-monitoring md5sum /app/stream_monitoring_service.py
md5sum services/stream-monitoring/stream_monitoring_service.py    # must match

docker exec streamscout-flink-jobmanager python3 -c \
  "import sys; sys.path.insert(0,'/opt/flink/usrlib'); import spike_detector as s; \
   print(s.WATERMARK_OUT_OF_ORDERNESS_SECONDS, s.WATERMARK_IDLENESS_SECONDS)"
```

---

## Part 2: Checking system status

```bash
docker compose ps
```
All listed services should show `running`. flink-jobmanager should also show `healthy`.

```bash
docker exec streamscout-flink-jobmanager flink list
```
Should show one "Clip Detector Job (RUNNING)".

```bash
curl -s "http://localhost:5000/v1.0/clip?limit=5" | python3 -m json.tool
```
Should return recent clips, or an empty array if none exist yet.

---

## Part 3: Restarting individual components

Use these steps when one component fails. Skip Postgres and Redis — see "Important" above.

### Kafka

**When:** connection errors, or messages not flowing.
```bash
docker compose restart kafka
docker compose up -d --wait --wait-timeout 180 kafka
docker exec streamscout-kafka kafka-topics --bootstrap-server localhost:9092 --list
```
Should list `chat-messages`, `stream-lifecycle`, and `suppression-events`.

**After a Kafka restart, also restart these** — they hold open connections to Kafka that do not reconnect on their own:
```bash
docker compose restart stream-monitoring flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```
Restarting flink-jobmanager runs the job fresh — see "How the Flink job runs" above. Confirm: `docker exec streamscout-flink-jobmanager flink list`.

### Stream monitoring service

**When:** chat messages stop reaching Kafka, Twitch API errors, or websocket failures.
```bash
docker logs streamscout-stream-monitoring --tail 20
docker compose restart stream-monitoring
docker logs -f streamscout-stream-monitoring
```
Expect to see: `Stream Monitoring Service started`, `Reconciler started`, `Adopted existing subscriptions`, `Polling for top streams`, `Poll finished`. Press `Ctrl+C` to stop following.

To pick up a **code change**, use `docker compose up -d --force-recreate stream-monitoring` instead of `restart` — see "Note on image rebuilds".

### Flink job (Clip Detector)

**When:** no clips are appearing, the job shows FAILED, or TaskManager reports heartbeat timeouts.

There is no separate job to cancel and resubmit — restarting flink-jobmanager restarts the job:
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```
Confirm it is running, then check that it is processing:
```bash
docker exec streamscout-flink-jobmanager flink list
docker logs -f streamscout-flink-taskmanager 2>&1 | grep -iE "token|kafka|baseline"
```

### API and frontend service

**When:** the API does not respond, returns 500 errors, or the frontend does not load.
```bash
docker compose restart api-frontend
curl http://localhost:5000/health
curl -s "http://localhost:5000/v1.0/clip?limit=1" | python3 -m json.tool
```

### Prometheus

**When:** metrics do not appear in Grafana.
```bash
docker compose restart prometheus
```
Check http://localhost:9090.

### Grafana

**When:** dashboards do not load, or login fails.
```bash
docker compose restart grafana
```
Check http://localhost:3000 (`admin`/`admin`).

### Loki and Promtail

**When:** logs do not appear in Grafana.
```bash
docker compose restart loki promtail
curl http://localhost:3100/ready
```
Should print `ready`.

---

## Part 4: Complete shutdown

**Stop all containers, keep data:**
```bash
docker compose down
```

**Stop all containers and delete local data:**
```bash
docker compose down -v
```
**Warning:** `-v` deletes local volumes — Kafka data, Prometheus/Grafana/Loki history. It does **not** touch clips or the database, because those live on the remote host, not in a local volume.

---

## Part 5: Viewing logs

```bash
docker logs streamscout-<service-name>
```
Service names: `kafka`, `stream-monitoring`, `flink-jobmanager`, `flink-taskmanager`, `api-frontend`, `prometheus`, `grafana`, `loki`, `promtail`, `alertmanager`, `node-exporter`.

```bash
docker logs streamscout-stream-monitoring --tail 50   # last 50 lines
docker logs -f streamscout-stream-monitoring           # follow live
docker logs -t streamscout-stream-monitoring --tail 20 # with timestamps
```

---

## Part 6: Common problems

### "No running jobs" in Flink
This means flink-jobmanager's container is not healthy, or has restarted and is still starting up. Check:
```bash
docker compose ps flink-jobmanager
docker logs streamscout-flink-jobmanager --tail 50
```
A Python traceback near the end of the log is the usual cause — a bad token file, a Kafka connection problem, or similar. Fix the cause, then restart the container:
```bash
docker compose restart flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### Flink job fails with "heartbeat timeout"
```bash
docker compose restart flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### "Token file not found" in Flink logs
```bash
ls -la ./secrets/twitch_user_tokens.json
```
If missing, run `python seed_twitch_tokens.py`, then restart Flink:
```bash
docker compose restart flink-jobmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
```

### Clips stop being created, but anomaly detection keeps running

On the **StreamScout Overview** dashboard, "Clips Created" falls to zero and
"Clips Failed" climbs while the anomaly line is unaffected. In
`streamscout-flink-taskmanager` logs:

```
Create clip API response: status=401 ... "Invalid OAuth token"
Create clip exception ...: [Errno 13] Permission denied: '/opt/flink/secrets/.tmp-tokens-....json'
CLIP CREATION FAILED ... reason=api_error
```

The access token expired and the job cannot persist a refreshed one: the
bind-mounted `secrets/` directory is not writable by the container user
(uid:gid 9999). Anomaly detection needs no token, so it is unaffected. The
job keeps serving the last token it holds and logs
`could not be written to ... using it in memory only` at ERROR every refresh,
so clips limp on for one token lifetime (~4 h) and then fail hard.

```bash
ls -ld ./secrets                 # want: group 9999 (twitchtoken), mode 2775
sudo chgrp -R 9999 ./secrets && sudo chmod 2775 ./secrets
docker compose restart flink-jobmanager flink-taskmanager
```

A re-seed (`seed_twitch_tokens.py`) run by a host user not in gid 9999, or a
bind source Docker first created as `root:root`, is the usual cause. `start.sh`
re-normalises `secrets/` on every startup (from a throwaway `busybox` container
so it needs no host root); `seed_twitch_tokens.py` attempts the same on write
and prints the exact `chgrp`/`chmod` to run if it cannot. A stale checkout that
predates those is the likely culprit here.

### Adding a new Python module
Two steps, not one:
1. Add a bind-mount line for the new file, under both `flink-jobmanager` and `flink-taskmanager` in `docker-compose.yml` (`volumes:`), matching the existing four.
2. Add the new path to the `FLINK_PYFILES` environment variable, under `flink-jobmanager`.

Then `docker compose up -d` — no image rebuild needed for either step.

### No chat messages are reaching Kafka

Chat is EventSub now, so there are no chat rooms to join. Work down the chain:

```bash
docker logs streamscout-stream-monitoring --tail 30
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_subscription_count|reconcile_last_success_timestamp|subscription_create_failures_total)'
```

1. **`eventsub_subscription_count` is 0 and every create fails.** Look for
   `subscription missing proper authorization` on every channel. That is the
   token, not the broadcasters: it has no **`user:read:chat`** scope. The
   service also logs this at ERROR on start-up. Re-seed and force-recreate:
   ```bash
   python seed_twitch_tokens.py
   docker compose up -d --force-recreate stream-monitoring
   ```
   The service deliberately does **not** persist refusals while that scope is
   missing, so a token mistake cannot mark the whole monitored set as refused
   for seven days. Nothing needs undoing in the database afterwards.
2. **`reconcile_last_success_timestamp` is not advancing.** Check whether this
   is a cold start first: a pass that is backing off 429s can legitimately run
   for about 200 s at the default settings, and the subscription count will be
   climbing throughout. Restart the container only if the count is flat and
   `reconcile_duration_seconds` shows no pass completing.
3. **The count is right but Kafka is empty.** The subscriptions exist and are
   silent, so the problem is downstream — check the Kafka producer logs and
   `kafka_messages_produced`.
4. **The count has plateaued and the log says `pool is at its 3-connection
   limit`.** This is not a fault to restart: the monitored set or in-flight
   work has exceeded transport capacity. Twitch allows 3 websocket connections
   × 300 = **900 subscriptions**, not channels. Feature 007 permits at most 400
   dual-covered channels (800 steady subscriptions) and reserves 100 slots;
   do not admit a 401st channel or raise thresholds. Inspect coverage,
   occupancy, desired count, and the capacity-safe rollback above.
   Re-seeding the token and restarting both achieve nothing here.
5. **Authentication errors** — check which kind before touching the token. A
   token that is missing, expired, scope-reduced or hand-edited is logged as
   `No usable user token, running without user auth (chat will not work)` and
   the service keeps polling with no reconciler; that one needs a re-seed. A
   transient Twitch or network failure during token validation is **not**
   treated as expiry — it is deliberately allowed to crash the container so
   Docker restarts it, and it recovers on its own. If the container is
   restart-looping with 5xx or connection errors in the log, wait for Twitch
   rather than rotating a valid token.

A single channel refusing on every pass is normal — roughly 1 in 500 does — and
it is recorded in `streamers.eventsub_refused_at` and retried after 7 days.

### No clips after 5+ minutes
Check each of these in order:
1. Flink job running? `docker exec streamscout-flink-jobmanager flink list`
2. Messages in Kafka? `docker exec streamscout-kafka kafka-console-consumer --bootstrap-server localhost:9092 --topic chat-messages --max-messages 3 --timeout-ms 10000`
3. Stream monitoring sending messages? `docker logs streamscout-stream-monitoring --tail 30`
4. Baseline still building? The job needs 5 minutes of data before it detects anomalies: `docker logs streamscout-flink-taskmanager --tail 50 2>&1 | grep -i baseline`

### "403 Forbidden — User not authorized to create clips"
This is expected. Some streamers turn off clip creation. The system still creates clips for streamers who allow it.

### API returns an empty clips array
1. No clips created yet — wait 5+ minutes after startup.
2. Database connection issue — restart the API: `docker compose restart api-frontend`
3. Check the remote database has clips — see "Important: Postgres and Redis are remote" above for how to check without a local `psql`.

---

## Quick reference: URLs

| Service | URL |
|---|---|
| Frontend / API | http://localhost:5000 |
| Flink Web UI | http://localhost:8081 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

## Quick reference: commands

| Action | Command |
|---|---|
| Full restart | `./start.sh` |
| Stop everything | `docker compose down` |
| Check status | `docker compose ps` |
| Check Flink job | `docker exec streamscout-flink-jobmanager flink list` |
| Restart the Flink job | `docker compose restart flink-jobmanager` |
| Rebuild the Flink images | `docker compose build flink-jobmanager flink-taskmanager` |
| View service logs | `docker logs streamscout-<service-name>` |
| Restart a service | `docker compose restart <service-name>` |

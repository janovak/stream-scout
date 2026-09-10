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
subscriptions** per client-id/user-id pair, at 300 each. Feature 007 sets an
entry threshold of 400 channels and a retention-and-maximum threshold of 450:
450 dual-covered channels consume all 900 subscriptions, so at the maximum the
account is **exactly full and no slot is held in reserve**. The pool refuses to
open a fourth connection rather than let Twitch reject subscriptions one by
one, so exhaustion is logged with the prefix
`The transport has no free subscription slot`. See
"Feature 007: gift/raid suppression operations" below.

### Reading the reconciler metrics

All of these are on the stream-monitoring metrics endpoint, port **9100**:

```bash
curl -s http://localhost:9100/metrics | grep -E '^(eventsub_|reconcile_|subscription_create)'
```

| Metric | Read it as |
|---|---|
| `eventsub_subscription_count` | Live subscriptions held, in **subscription** units. With complete Feature 007 coverage it should be twice `ZCARD chat:desired`, up to 900 at the 450-channel maximum; use `eventsub_channel_coverage`, not this gauge, for channel counts. It moves DURING a pass, not only at the end: it steps up as a cold start creates, and drops the moment a socket loss is reported |
| `reconcile_last_success_timestamp` | Unix time of the last pass that **ran to completion**. **This is the stalled-reconciler alarm.** If it stops advancing while polls keep succeeding, the reconciler is stuck and the subscription set is frozen — the poller cannot tell you this, because it still works. Two things it does **not** mean: it is not "a pass with no failures" (at 500 channels one broadcaster refuses every pass, so gating on that would freeze the gauge and destroy the signal — per-channel failures are `subscription_create_failures_total`); and **a gap of a few minutes during a cold start is normal, not a stall**. A pass that hits 429s backs off and retries inside the pass, up to `RECONCILE_MAX_RETRY_ROUNDS` × `RECONCILE_RATE_LIMIT_BACKOFF_SECONDS` ≈ 200 s at the defaults, and the stamp only lands when the pass ends. Before restarting anything, check `reconcile_duration_seconds` and whether the subscription count is still climbing |
| `reconcile_duration_seconds` | Histogram of pass duration. A converged pass is milliseconds. Buckets run to 120 s because a cold start to 500 channels takes ~51 s |
| `subscription_create_failures_total` | Counter, labelled by `reason`. **No series at all is the healthy state**, not a broken exporter: this client registers a labelled series on its first increment |
| `eventsub_connection_occupancy{connection}` | Subscriptions per connection. None may exceed 300 |

---

## Ramping the monitored channel count

The ladder below records the historical single-subscription ramp through
feature 006. Feature 007 supersedes its operating point with an entry
`JOIN_THRESHOLD=400` and a retention-and-maximum `LEAVE_THRESHOLD=450` because
every channel now uses two subscriptions. That is the same shape as the
historical 800/900 step, halved: a deep entry gate, a retention band above it,
and a ceiling equal to the account limit. Do not use the historical 800/900
result to raise a feature-007 deployment; follow the dedicated rollout and
rollback below.

### What `JOIN_THRESHOLD` and `LEAVE_THRESHOLD` actually do

`LEAVE_THRESHOLD` sets the monitored maximum, not `JOIN_THRESHOLD`. A channel
enters the set on reaching the top `JOIN_THRESHOLD` by viewer rank and stays
until it drops out of the top `LEAVE_THRESHOLD`. So 800 / 900 monitored roughly
720–800 channels (the top 900 minus the ~20% with clipping disabled), with an
800-deep entry gate that keeps a channel near the boundary from flapping in and
out. Feature 007's 400 / 450 works identically at half the scale: a channel not
already monitored must be inside the top 400 to enter, an incumbent is retained
through the top 450, and the set therefore tops out at 450 channels. To run
near a round number of channels, set `LEAVE_THRESHOLD` to it and
`JOIN_THRESHOLD` to something below.

This also means that raising only `LEAVE_THRESHOLD` from 400 to 450 does not
instantly add the channels currently ranked 401-450. Starting from 400, the
retained band fills only as ranking turnover admits new top-400 channels while
former top-400 incumbents remain at ranks 401-450, or through an explicit safe
validation seed.

### How to change the thresholds

The poller reads both from the environment. `JOIN_THRESHOLD` must not exceed
`LEAVE_THRESHOLD`; equality is valid and is used for feature 007's first
dual-coverage deployment at 400/400. The final target deliberately restores
the 50-channel retention band at 400/450.

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
| `subscription_create_failures_total{reason="rate_limited"}` | absent, or brief cold-start bursts all retried | keeps rising after convergence |
| `subscription_create_failures_total{reason="transient_session"}` | a cold-start burst (seen up to ~70), then static | keeps rising during steady state |
| `eventsub_connection_occupancy{connection}` | each value ≤300; summed occupancy equals `eventsub_subscription_count` | any value >300 or a sum mismatch |
| `eventsub_connection_full{connection}` / `eventsub_connection_free_slots{connection}` | fullness agrees with zero usable slots; summed free slots equal 900 minus total subscriptions | contradictory fullness/free-slot values |
| `eventsub_connection_full_below_cap{connection}` | 0 after convergence | 1 means provider-reported fullness is stranding nominal capacity |
| `subscription_create_failures_total{reason="capacity"}` | static unless a deliberate full-pool drill runs | rises below the intended ceiling; inspect free slots, `full_at`, foreign subscriptions, and failed deletes |
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
next pass. For Feature 007, use the capacity-safe rollback order below: the
retention threshold is **never above 450 while any notification subscription
remains**, and it must be back at **400** before the dual transport is
unwound.

---

## Feature 007: gift/raid suppression operations

This section is the operator runbook for
`specs/007-suppress-gift-raid-bursts`. Live validation is performed by an
operator on the configured machine. Local tests, fixtures, static assertions,
and replay output do **not** establish deployed evidence E1-E5.

**Capacity amendment, 2026-09-05.** The capacity contract below describes the
approved entry-400 / retention-and-maximum-450 model with exact
900-subscription occupancy (autonomous decisions 27-28). The amended runtime,
safe compose default, metrics, and deterministic tests are implemented and
tested locally. They have not been deployed: E1, E2a, E2b, E3, E4, and E5 all
remain pending operator evidence.

### Capacity and coverage contract

- The monitored set is bounded by **two different thresholds**: an entry
  threshold `JOIN_THRESHOLD=400` and a retention-and-maximum threshold
  `LEAVE_THRESHOLD=450`. A channel that is not already monitored enters only
  inside the top 400 by rank; an incumbent is retained through rank 450 and
  leaves beyond it. A channel newly ranked 401-450 therefore does **not** join,
  while a channel already monitored at that rank stays. The monitored set never
  exceeds **450 channels**. Do not hide a different hysteresis policy in code.
- The safe checked-in compose default is
  `LEAVE_THRESHOLD=${LEAVE_THRESHOLD:-400}` with literal
  `JOIN_THRESHOLD=400`. The first dual-coverage deployment therefore runs at
  400/400 without an override. The operator sets `LEAVE_THRESHOLD=450` only
  after E1 and E2a pass and the account-wide sweep is clean. `450` is the final
  operator-selected target, not a checked-in live literal.
- Each monitored channel needs two independent subscriptions:
  `channel.chat.message` for chat and `channel.chat.notification` for gift/raid
  notices. At the 450-channel maximum this is 450 × 2 = **900 subscriptions** of
  the 900-subscription account limit. **Capacity is exact: there is no reserve
  and no guaranteed free slot.**
- Reconnect and adoption normally consume no *new* slot. Ending a session
  disables its subscriptions and disabled subscriptions stop counting, and a
  409 adoption records a subscription that already exists. What exact capacity
  exposes instead is parity fragmentation, in-flight create/delete overlap,
  enabled subscriptions the pool does not own, failed deletes, and a connection
  that reports itself full below the cap — see the capacity signals and
  troubleshooting below.
- Each websocket session is capped at 300 **subscriptions**, so it can hold at
  most **150 complete channel pairs**. Placement first co-locates a pair on an
  existing connection, then grows while fewer than three connections exist. A
  pair may be split across sessions only at the three-connection maximum or
  after growth fails: when no connection holds two free slots but the pool
  holds at least two usable slots, one slot is reserved on each of two
  connections, all-or-nothing.
- A hard capacity exhaustion is an **expected operating state** at the maximum,
  not an anomaly. It is reported under its own capacity classification —
  distinct from a Twitch refusal and from a transient transport fault — never
  writes the seven-day per-channel refusal, never evicts existing coverage, and
  does not arm a transient growth backoff, because waiting cannot create a slot.
- A connection's `full_at` is cleared and re-evaluated when that connection
  reconnects or is retired. A connection reporting itself full **below** 300 is
  stranded capacity and is visible as such; at exact capacity it is the
  difference between converging at 450 and stalling short of it.
- Capacity preflight shares one bounded account-wide adoption snapshot across
  blocked creates, preventing a full pool from causing a per-channel Helix
  enumeration storm. That protection does not replace deployed observation:
  E2b still checks the live `reason="rate_limited"` failure series.
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
after each successful desired-set publication. It is **advisory telemetry**: it
shows how much the monitored set moves, and no numeric bound, observation
window, or release gate is attached to it. Read a counter delta, not its
lifetime value, and divide by the number of successfully published polls in the
same window when you want a per-poll rate (`Poll finished` provides `entered`,
`left`, `desired`, and outcome context). The 400/450 thresholds keep a
50-channel retention band, so boundary-rank flapping is damped by
configuration rather than measured against a release bound. Any change to the
configured thresholds remains a specification change, never an undocumented
configuration or code workaround.

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
It is enforced at both source timestamp assignment and operator validation
using the same receipt/source wall-clock basis. At exactly
`source_clock_ms + 30_000`, `occurred_at_ms` is assigned as event time; at
+30,001 ms, as for missing/unreadable occurrence time, the assigner uses Kafka
`record_timestamp` so the untrusted value cannot advance the watermark. This
does not rewrite or validate the payload. `process_element2` still receives
the original value and rejects it as `reason="fields"` with the existing
warning before delivery observation or suppression-state access. Accepted
future skew is clamped to delivery age zero and logged. Missing, malformed,
late, or unavailable suppression signals always **fail open**: normal clip
eligibility continues, and an already-emitted clip is never retracted.

The assigner only runs where the job builds its real `WatermarkStrategy` in
the exact order bounded out-of-orderness → `with_idleness()` →
`with_timestamp_assigner()` last, then attaches it with
`assign_timestamps_and_watermarks()` **after** `from_source`. PyFlink 1.18
silently ignores a Python timestamp assigner handed to `from_source`, and event
time then degrades to the Kafka record timestamp on that stream — no error, no
log line. Its `with_idleness()` also returns a fresh wrapper and drops an
assigner bound before it. Both the chat stream (`sent_at`) and the suppression
stream
(`occurred_at_ms`) depend on this attachment, so a job whose event time tracks
broker ingestion time rather than Twitch's clock is a defect, not a tuning
question. The chat assigner accepts only plain integer (not boolean) `sent_at`
through `source_clock_ms + 30_000`; missing/null, string, float, bool, and
+30,001 ms use Kafka record time for event-time assignment without rewriting,
rejecting, or dropping the chat record. Because watermarks and idleness are
then emitted by that assignment operator per subtask rather than per Kafka
split, the deployment invariant is:
**topic partitions, source parallelism, and assignment parallelism must all be
4, chained one-to-one with no repartition between the source and the
assigner.** Any change to partitions, `FLINK_PARALLELISM`, or the operator
chain invalidates the idleness reasoning behind the sparse-source design and
must be revalidated with E3 before gating is enabled.

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
| `eventsub_connection_occupancy{connection}` | Gauge in **subscriptions per websocket connection**, including the connection label. Every value must remain at or below 300, and at the 450-channel maximum their sum is exactly 900 |
| `eventsub_connection_full{connection}` | Gauge equal to 1 exactly when that connection has **zero usable free slots**. It includes ordinary fullness at occupancy 300 and provider-reported fullness below 300; use `eventsub_connection_full_below_cap` to distinguish the stranded-capacity case |
| `eventsub_connection_free_slots{connection}` | Gauge of the exact **usable** subscription slots remaining on that connection after reservations and any provider-reported `full_at` limit. Sum it for pool free capacity; do not derive usable free slots as `300 - occupancy` |
| `eventsub_connection_full_below_cap{connection}` | Gauge equal to 1 only when Twitch reported the connection full at an occupancy below 300. Those nominal slots are stranded until the observation is cleared and re-evaluated on reconnect or retirement |
| `eventsub_subscription_count` | Gauge in **subscriptions across the pool**. Complete dual coverage is exactly 2 × desired channels, up to 900 at the maximum. Read it as a relation: `== 2 ×` complete channels, and `== 900` at the ceiling. Both equalities are mandatory when their conditions apply |
| `active_stream_count` | Gauge in **channels** in the reconciler's actual set after a completed pass. It is not a subscription count; read it with desired count and coverage state |
| `suppression_notices_ignored_total{notice_type}` | Producer counter for deliberately excluded categories; known categories retain their name and unknown/absent categories use bounded `other`. Traffic here is not malformed |
| `suppression_notices_malformed_total{reason}` | Producer counter for trigger notices dropped without publication: bounded reasons `identity` or `occurred_at`. Any increase is a producer/input fault, not ordinary excluded traffic |
| `suppression_records_rejected_total{reason}` | Consumer counter for records ignored as `decode`, `schema_version`, or `fields`; over-30-second future timestamps are `fields`. Source timestamp fallback does not hide them: the unchanged payload reaches `process_element2`, and rejected records create no delivery sample or suppression state |
| `suppression_records_consumed_total{lag_class}` | Counter for trusted records actually consumed/applied. `healthy` means clamped age ≤30 s; `lagging` means >30 s. There is intentionally no `idle` series |
| `suppression_delivery_age_seconds` | Histogram, one observation per trusted record, in **seconds**, of `max(0, consumer receipt - occurred_at)`. Use its bucket/rate distribution for percentiles; rejected records and silence add no observation |
| `desired_set_churn_total` | Unlabelled counter in **channel membership changes**: entered + departed after successful publication. **Advisory only** — no bound, no observation window, and no power to block enabling gating |
| `subscription_create_failures_total{reason="capacity"}` | Counter of create attempts that found no usable slot anywhere in the pool. This is the exact hard-capacity classification, distinct from provider refusal and transient transport failure; it does not arm rate-limit backoff or write a durable refusal |
| `subscription_create_failures_total{reason="rate_limited"}` | Counter of provider HTTP 429 create responses. A cold-start increment may be retried; continued increase after convergence is a live rate-limit failure, not capacity exhaustion |

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
| `Suppression notice refused for broadcaster ...` | Consumer rejected the original occurrence beyond the fixed future bound after the assigner used Kafka record time for watermark assignment; the line includes channel, type, occurrence, consumer receipt, bound, and `no deadline written`. Correlate with `suppression_records_rejected_total{reason="fields"}`, source/operator host clocks, and watermark continuity |
| `Suppression clock skew for broadcaster ...` | Accepted occurrence is ahead by no more than 30 s; age was clamped to zero and the notice was still applied |
| `Suppression delivery lag for broadcaster ...` | Trusted age exceeded 30 s; the line includes channel, type, and age. The notice is late but still applied |
| `CLIP SUPPRESSED for broadcaster ...` | Suppression decision; includes channel, notice type, peak, interval bounds, notice time, and intensity. It must pair with one `clips_suppressed_total` increment and no clip yield |
| `Error applying suppression notice for broadcaster ...` | Unexpected consumer exception. Treat as fail-open and inspect the traceback plus rejection/consumption counters |
| `The transport has no free subscription slot` | Capacity warning prefix, distinct from malformed, provider-refusal, and delivery failures. Inspect `subscription_create_failures_total{reason="capacity"}`, total subscriptions, all three connection-capacity gauges, coverage, desired count, and thresholds |
| `Poll finished` | Churn context; `entered` + `left` is that successful publication's increment, with `desired` and `outcome` for advisory reading |

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
   did not contribute a health sample or state. Verify its source timestamp was
   Kafka record time, not the untrusted occurrence value; a watermark
   jump indicates the final-review fix is absent or not deployed. If the
   assigner appears to have no effect at all — event time tracking broker
   ingestion rather than `sent_at`/`occurred_at_ms` on either stream — check
   that the job attaches each strategy with `assign_timestamps_and_watermarks`
   after `from_source` rather than passing it into `from_source`, and that each
   strategy was built bounded out-of-orderness → idleness → assigner last.
   PyFlink 1.18 ignores the former and `with_idleness()` drops an assigner
   stored before it. For chat, verify invalid-type or over-bound `sent_at`
   falls back to broker time but the message still reaches counting/command
   processing.
4. **Delivery:** apply the three-state Prometheus reading above. Lagging records
   remain applied. Silence is idle/unknown; do not substitute complete coverage
   for delivery evidence.
5. **Decision:** correlate `CLIP SUPPRESSED`,
   `clips_suppressed_total`, and the still-incrementing
   `anomalies_detected_total`. If policy is suspect, turn gating off first.
6. **Capacity/churn:** verify the desired set is within its threshold for the
   current stage (≤400 while retention is 400, ≤450 at the final
   400/450), `complete == desired`,
   `eventsub_subscription_count == 2 × desired`, every connection occupancy is
   ≤300, and the sum of occupancies equals `eventsub_subscription_count`.
   Eligible supply may leave desired below 450; that is healthy when all four
   relations hold. When a maximum validation reaches 450 desired channels,
   require **exactly 900** subscriptions and **zero** usable free slots, and
   exclude a 451st channel at the desired-set layer.

   If convergence fails below the target, first inspect
   `eventsub_connection_full_below_cap{connection}` together with exact free
   slots and occupancy. A value of 1 is a stranded provider `full_at`, not
   ordinary occupancy-300 fullness. Reconnect the affected socket so `full_at`
   is cleared and re-evaluated; if it persists, restart stream-monitoring using
   the existing restart procedure and verify the capacity gauges are rebuilt.
   Then check the account-wide subscription enumeration for foreign enabled
   rows, failed deletes, and split-pair fragmentation. A rising
   `reason="capacity"` series confirms the pool had no usable slot; it does not
   prove Twitch refused a request. `desired_set_churn_total` remains advisory
   context, never a gate.

### Forward rollout and pending evidence

Run these gates in order on the configured machine:

1. **B0 — current single-subscription revision:** set 400/400 using the
   threshold procedure above and wait for Redis `chat:desired` and
   `eventsub_subscription_count` to satisfy subscriptions == desired with
   desired ≤400. Do not proceed unless eligible supply can support the
   subsequent 400-channel E2a. This is unconditionally safe at 400 × 1; E1 does
   not block this preliminary ramp-down.
2. **Deploy with gating off and retention still 400:** ensure `kafka-init` has
   created `suppression-events` **before** starting the feature Flink job.
   Deploy the feature revision with the checked-in
   `SUPPRESSION_GATING_ENABLED=false` and the safe checked-in compose default
   `LEAVE_THRESHOLD=${LEAVE_THRESHOLD:-400}`. First dual convergence is 400
   channels / 800 subscriptions with exactly 100 usable pool slots. Clip
   behavior remains pre-007.
3. **E1 — mixed types and cost:** enumerate live EventSub subscriptions, prove
   both types coexist on live sessions, and prove `total_cost=0` against
   `max_total_cost=10`. Non-zero cost blocks the feature; use the rollback below.
4. **E2a — dual coverage at 400 channels:** prove 400 complete channels, 800
   subscriptions, no connection above 300, exactly 100 usable pool slots, and
   exclusion of the 401st channel while retention is still 400. Read
   `desired_set_churn_total` as advisory context; no observation window gates
   progression.
5. **Sweep the account, then ramp to the final 400/450:** enumerate **every
   enabled subscription of every type on every session** for the client-id /
   user-id pair and remove anything the pool does not own — an earlier
   revision's leftovers, an orphan from a failed delete, or another process's
   subscription. The account-wide enabled total must then equal
   `eventsub_subscription_count`; otherwise do not ramp. Set the service
   environment to `LEAVE_THRESHOLD=450` and recreate only stream-monitoring.
   This is a configuration-only selection of the final operator target; the
   checked-in safe default remains 400. It does not admit current
   rank-401-450 channels. The desired set grows beyond 400 only through ranking
   turnover — new top-400 channels entering while displaced incumbents are
   retained at ranks 401-450 — or through an explicit safe validation seed.
6. **E2b — relational convergence and conditional exact-capacity drills.**
   Read these as relations, not approximations, at every desired count ≤450:
   - desired **≤450**;
   - `eventsub_channel_coverage{state="complete"}` **== desired**;
   - `eventsub_subscription_count` **== 2 × desired**;
   - every `eventsub_connection_occupancy` **≤300**, and their sum
     **== `eventsub_subscription_count`**;
   - summed `eventsub_connection_free_slots{connection}` **== 900 −
     `eventsub_subscription_count`**, with
     `eventsub_connection_full{connection}` and
     `eventsub_connection_full_below_cap{connection}` consistent with usable
     and stranded capacity;
   - the account-wide, all-type, all-session enabled subscription total
     **== `eventsub_subscription_count`**.

   Lower eligible supply is not a failure when those relations hold. Exact-450
   validation is conditional on 450 valid incumbents accumulating through
   ranking turnover or being supplied by an explicit safe validation seed.
   Once that condition holds, require desired **==450**, subscriptions
   **==900**, summed occupancy **==900**, summed usable free slots **==0**, and
   exclusion of the 451st qualifying channel.

   At that maximum, exercise all exact-capacity paths: co-location first,
   growth while below three connections, then split placement across two
   connections only at the maximum or after growth fails;
   loss and replacement of one split fragment; complete socket loss and
   convergence back to complete coverage; reconnect and 409 adoption with no
   additional slot consumed; a failed delete remaining visible as occupied
   capacity; below-cap `full_at` visibility followed by reconnect/restart
   re-evaluation; and `reason="capacity"` classification without provider
   refusal, durable refusal, coverage eviction, or transient backoff. Confirm
   the shared adoption snapshot prevents a per-channel Helix request storm,
   while `subscription_create_failures_total{reason="rate_limited"}` does not
   continue rising after convergence.
7. **E3 — sparse-source watermark:** keep the topic silent for at least one
   hour and prove chat detection/watermark behavior does not stall. Then observe
   one isolated notice after silence and prove detection resumes and the source
   re-idles within
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`.
   Next, deliver a controlled record beyond `source_clock_ms + 30000`, let
   chat idle and resume, and prove the stream uses Kafka record time, the
   combined watermark does not jump to the untrusted occurrence value,
   remains monotonic, and real-time timers continue.
   Finally, prove the timestamp assigners actually run and survive strategy
   construction: watermarks originate
   from the post-source `assign_timestamps_and_watermarks` operator on **both**
   the chat and suppression streams. Verify trusted plain-integer chat
   `sent_at` and trusted suppression `occurred_at_ms` drive event time; exact
   +30,000 ms is accepted; +30,001 ms falls back; and chat
   missing/null/string/float/bool values fall back to Kafka record time without
   losing the message. Confirm neither stream's fallback advances the combined
   watermark from an untrusted value. Inspect the deployed graph to confirm
   `with_timestamp_assigner()` remained last after idleness and each assignment
   subtask maps to one topic partition at parallelism 4. Record TaskManager
   Python process count and aggregate/per-process RSS before and after this
   revision: post-source assignment adds two Python stages at parallelism four,
   and their worker/process footprint is deployed evidence only. Without these
   checks, the earlier cases may describe Kafka record time or an unmeasured
   Python-worker regression.
8. **Enable, then E4:** only after E1, E2a, E2b including every exact-capacity
   drill, and all four E3 cases pass, change
   `SUPPRESSION_GATING_ENABLED=true` in both Flink compose blocks and recreate
   the Flink components using the existing procedure. Capture real gift and
   raid slices and verify mapping, trusted-record age, delivery classification,
   intended suppression, unaffected pre-notice/outside-window peaks, and the
   120/180-second defaults. Check and record NTP/clock synchronization on the
   producer and consumer hosts with approved host tooling: skew over 30 seconds
   makes notices use Kafka record time upstream, then reject downstream and
   therefore fail open. Confirm the original payload remains visible through
   `reason="fields"` and the existing refusal warning.
9. **E5 — rollback rehearsal:** exercise the exact capacity-safe order below.

Evidence remains pending until an operator records it:

| Evidence | Required deployed observation | Status |
|---|---|---|
| E1 | Live mixed subscription types; cost 0 of 10 | [ ] Pending operator run |
| E2a | Dual coverage at 400 channels / 800 subscriptions with retention still 400: ≤300 per connection, exactly 100 usable pool slots, 401st excluded. Gates the ramp to 450 | [ ] Pending operator run |
| E2b | After the all-type/all-session account sweep and operator selection of 400/450: at every desired ≤450, account enabled total == local subscriptions, complete == desired, subscriptions == 2 × desired, summed occupancy == subscriptions, and summed usable free slots == 900 − subscriptions. Exact-450 checks are conditional on 450 valid incumbents accumulating through turnover or an explicit safe seed; then subscriptions == 900 and free slots == 0. Includes grow-before-split placement/replacement, socket loss, reconnect/adoption without an extra slot, failed delete, `subscription_create_failures_total{reason="capacity"}`, below-cap `full_at` recovery, and live rate-limit observation | [ ] Pending operator run |
| E3 | One-hour silence and isolated-notice bounds pass; both assigner-last strategies run post-source; trusted payload time and exact/+1 ms/type fallbacks behave correctly without chat loss or watermark poisoning; one partition maps to each parallelism-four assignment subtask; and TaskManager Python process count/RSS for the two added stages is recorded | [ ] Pending operator run |
| E4 | Real gift/raid mapping, age and clock behavior, downstream over-future rejection visibility after source fallback, in-window suppression, and unaffected outside/pre-notice peaks, all read on Twitch-clock event time rather than broker ingestion time | [ ] Pending operator run |
| E5 | Kill switch and capacity-safe rollback rehearsal, including lowering retention to 400 and reconverging before the transport is unwound | [ ] Pending operator run |

### Capacity-safe rollback

The governing invariant is absolute: **the retention threshold is never above
450 while any `channel.chat.notification` subscription exists, and it must be
back at 400 before the dual transport is unwound.**

1. Set `SUPPRESSION_GATING_ENABLED=false` first. This alone is the complete
   response to a detection-policy-only incident; leave subscriptions and topic
   in place while diagnosing. It is **not** the lever for a capacity incident.
2. For a capacity incident, set `LEAVE_THRESHOLD=400` (`JOIN_THRESHOLD` is
   already 400) and wait for the monitored set to reconverge at 400/400:
   desired ≤400, complete == desired, subscriptions == 2 × desired, and summed
   occupancy == subscriptions. At 400 desired channels this is exactly
   400 channels / 800 subscriptions / 100 usable pool slots. Keep dual coverage
   intact while fragmentation, a stranded below-cap `full_at`, a foreign
   subscription, or a failed delete is diagnosed.
3. For a transport rollback, unwind or revert the dual-subscription revision
   while `JOIN_THRESHOLD` and `LEAVE_THRESHOLD` are 400/400.
4. Wait until subscription enumeration contains no
   `channel.chat.notification`, `eventsub_subscription_count` equals the
   desired channel count, and coverage plus desired metrics are stable.
5. Only then restore the prior single-subscription ramp.

The `suppression-events` topic may remain. There is no database schema, Redis
layout, token, or dependency rollback.

---

## Feature 008: quiet-stream minimum-lift rollout

The detector still requires `DETECTION_STD_DEV_THRESHOLD=4.0`. Feature 008
adds a second condition: the five-second message count must exceed its baseline
expectation by at least `DETECTION_MIN_EXCESS_MESSAGES=2.0`. This prevents a
tiny positive baseline standard deviation from turning trivial absolute
activity into a clip. Intensity itself is unchanged.

The checked-in `DETECTION_MIN_EXCESS_GATING_ENABLED=false` is shadow mode.
In shadow mode, detector output and state follow the previous policy, while
each per-second reading that passes 4 sigma but misses the two-message lift is
logged as `MINIMUM LIFT CANDIDATE` and increments:

```text
anomaly_min_lift_candidates_total{broadcaster_id,mode="shadow"}
```

This counter measures candidate seconds, not counterfactual clips. The same
message can remain inside the five-second window for several evaluations. Each
log line also states `would_open`. The pure detector calculates that field
after dropping an over-age hold and with the same cooldown predicate used by
the state machine, so it identifies candidates capable of opening a new hold
under shadow policy.

### P0 — offline counterfactual

If the captured corpus is available, compare the exact detector output with
the gate off and on before deployment. This starts no service or
infrastructure:

```bash
cd services/flink-job
CORPUS=~/stream-scout-corpus/chat-corpus.jsonl

replay_min_lift() {
  DETECTION_WINDOW_SECONDS=5 \
  DETECTION_BASELINE_SECONDS=300 \
  DETECTION_STD_DEV_THRESHOLD=4.0 \
  DETECTION_MIN_EXCESS_MESSAGES=2.0 \
  DETECTION_MIN_EXCESS_GATING_ENABLED="$1" \
  DETECTION_HOLD_CAP_SECONDS=25 \
  DETECTION_COOLDOWN_SECONDS=30 \
  DETECTION_MIN_BASELINE_FRACTION=0.8 \
    python3 tools/replay.py "$CORPUS" | grep ' SPIKE '
}

replay_min_lift false | sort > /tmp/min-lift-shadow.spikes
replay_min_lift true  | sort > /tmp/min-lift-enforced.spikes
comm -23 /tmp/min-lift-shadow.spikes /tmp/min-lift-enforced.spikes \
  > /tmp/min-lift-removed.spikes
comm -13 /tmp/min-lift-shadow.spikes /tmp/min-lift-enforced.spikes \
  > /tmp/min-lift-added.spikes
wc -l /tmp/min-lift-{shadow,enforced,removed,added}.spikes
```

Record and review every changed event in the two diff files. Each line contains
the broadcaster, peak time, count, baseline mean, and intensity. Inspect the
corresponding clip when one exists, or the surrounding corpus messages.
Enforcement is blocked if it removes a desirable highlight or if an added
event cannot be explained by removing an earlier low-lift hold/cooldown.

The corpus characterizes the change; its channel mix may be stale, so it does
not predict current production volume. If the corpus is absent, record that
fact and continue to P1, but do not enable enforcement until live shadow
traffic has supplied candidate episodes for review. `tools/analyze_corpus.py`
now models Feature 008 by default; pass `--min-excess-messages 0` to reproduce
the pre-008 Plan 06 tables.

### P1 — shadow validation

1. Deploy with `DETECTION_MIN_EXCESS_MESSAGES=2.0` and
   `DETECTION_MIN_EXCESS_GATING_ENABLED=false` in both Flink service blocks.
   Force-recreate both Flink containers because the Python files are
   individually bind-mounted:
   ```bash
   docker compose up -d --force-recreate flink-jobmanager flink-taskmanager
   docker compose up -d --wait --wait-timeout 500 flink-jobmanager
   ```
2. Require both running containers to carry the same settings:
   ```bash
   docker exec streamscout-flink-taskmanager env | grep '^DETECTION_MIN_EXCESS'
   docker exec streamscout-flink-jobmanager env | grep '^DETECTION_MIN_EXCESS'
   ```
   Both must report `2.0` and `false`. The TaskManager environment is
   authoritative because `AnomalyDetector.open()` reads the worker
   configuration. The JobManager submission log is only submission-side
   evidence; confirm it as a secondary check:
   ```text
   DETECTION_MIN_EXCESS_MESSAGES: 2.0
   DETECTION_MIN_EXCESS_GATING_ENABLED: False
   ```
3. Exclude the first five minutes after the force-recreate from all rate
   comparisons while each channel rebuilds its baseline. Confirm the job is
   running and its four worker metric targets are live:
   ```promql
   sum(up{job="clip-detector"})
   ```
   The result must be `4`.
4. Run shadow mode without a restart for at least 24 hours, covering one
   representative peak period. During that single deployment epoch, read the
   raw counters rather than extrapolating a range longer than the counter has
   existed:
   ```promql
   sum by (broadcaster_id) (
     anomaly_min_lift_candidates_total{mode="shadow"}
   )
   ```
   Require at least one candidate in either P0 or this live soak; otherwise the
   new behavior has not been exercised and enforcement remains blocked.
5. Review the matching `MINIMUM LIFT CANDIDATE` records in Loki.
   `would_open=true` identifies readings capable of opening a new hold under
   shadow policy. Extract the emitted anomalies below the lift boundary:
   ```bash
   docker logs streamscout-flink-taskmanager --since 24h 2>&1 \
     | grep 'ANOMALY DETECTED' \
     | sed -E 's/.*broadcaster ([0-9]+):.*count=([0-9]+), mean=([0-9.]+).*/\1 \2 \3/' \
     | awk '$2 - $3*5 <= 2.025'
   ```
   This computes `count - mean × 5`; because the log rounds `mean` to two
   decimals, the result has at most ±0.025 message of error. The conservative
   `2.025` boundary prevents a real sub-2.0 case from being rounded out of the
   review set; it may include a few safe extra clips. Review every resulting
   clip. Proceed only if these are the unwanted low-volume cases characterized
   by P0 and none is a desirable highlight.
6. Record these as diagnostic context for P2, by broadcaster:
   ```promql
   sum by (broadcaster_id) (increase(anomalies_detected_total[24h]))
   sum by (broadcaster_id) (increase(clips_created_success_total[24h]))
   ```
   They are not percentage gates; P0's exact output diff and manual clip
   review are the acceptance gate.

### P2 — enforce and verify

Set `DETECTION_MIN_EXCESS_GATING_ENABLED=true` in **both** Flink service
blocks and force-recreate both containers:

```bash
docker compose up -d --force-recreate flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
docker exec streamscout-flink-taskmanager env | grep '^DETECTION_MIN_EXCESS'
docker exec streamscout-flink-jobmanager env | grep '^DETECTION_MIN_EXCESS'
```

Both containers must report `2.0` and `true`; the TaskManager value is
authoritative. Exclude the first five minutes while baselines rebuild. During
the next representative busy period:

- `anomaly_min_lift_candidates_total{mode="enforced"}` advances for quiet
  z-score candidates;
- a candidate with `would_open=true` does not produce an anomaly carrying
  that candidate's peak second;
- a candidate with `would_open=false` may end and emit an earlier valid peak,
  or may already be inside cooldown, as designed;
- sampled multi-message reactions still produce anomalies and clips;
- changed outputs retain the low-volume shape accepted in P0/P1; and
- Flink restarts/errors, watermark health, and clip-creation failures remain at
  their pre-change levels.

Roll back immediately if a desirable highlight is blocked, the gate emits a
candidate peak that should have been rejected, or detector health regresses.

### Rollback

Set `DETECTION_MIN_EXCESS_GATING_ENABLED=false` in both Flink service blocks
and force-recreate both Flink containers:

```bash
docker compose up -d --force-recreate flink-jobmanager flink-taskmanager
docker compose up -d --wait --wait-timeout 500 flink-jobmanager
docker exec streamscout-flink-taskmanager env | grep '^DETECTION_MIN_EXCESS'
docker exec streamscout-flink-jobmanager env | grep '^DETECTION_MIN_EXCESS'
```

Both must report `2.0` and `false`; the TaskManager value is authoritative.
Allow five minutes for baseline rebuild before judging clip recovery. A later
candidate must carry `mode="shadow"`. There is no database, Kafka, checkpoint,
or state migration to reverse.

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
4. **The count has plateaued and the log starts
   `The transport has no free subscription slot`.** This is not a fault to
   restart: the monitored set or in-flight
   work has exceeded transport capacity. Twitch allows 3 websocket connections
   × 300 = **900 subscriptions**, not channels. Feature 007 permits at most 450
   dual-covered channels, which is exactly 900 subscriptions with **no
   reserve**; at that maximum this message is an expected capacity condition
   rather than a defect, and it should carry the capacity classification rather
   than a refusal reason. Below the maximum, look for a connection full under
   300, an enabled subscription the pool does not own, or a failed delete. Do
   not admit a 451st channel or raise thresholds. Inspect coverage, occupancy,
   desired count, and the capacity-safe rollback above — lowering
   `LEAVE_THRESHOLD` to 400 is the fastest way back to a cushion.
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

# Validation Quickstart: Suppress Gift and Raid Chat Bursts

**Feature**: `007-suppress-gift-raid-bursts` | **Date**: 2026-09-04 (capacity amendment 2026-09-05)
**Companions**: [plan.md](./plan.md), [research.md](./research.md),
[data-model.md](./data-model.md),
[contracts/suppression-events.schema.md](./contracts/suppression-events.schema.md)

This procedure is split in two, and the split is not advisory.

- **Part A — offline validation** runs on the development workstation. It is
  unit tests, pure replay, static assertions on configuration files, and
  nothing else.
- **Part B — deployed validation** runs on the configured machine, by the
  operator. It is the only place any live claim can be made.

---

## 0. Machine policy (read before running anything)

**On the development workstation, do not start any service or infrastructure.**
Specifically: no `docker compose up`, no Kafka, no Flink cluster or MiniCluster,
no Redis, no Postgres, no `stream-monitoring` process, no EventSub websocket, no
Twitch API call, and no token or credential use. There is no `.env` and no
`secrets/` here, and that is intentional.

What is allowed here: `pytest` on the co-located test files, `python -c`
imports, `tools/replay.py` over a local JSONL file, static reads of
`docker-compose.yml`, and `git diff`.

Two consequences that follow from that, and that must be respected when
reporting status:

- Postgres-backed test classes in `test_stream_monitoring.py` self-skip unless
  `TEST_POSTGRES_URL` is set. **Leave it unset.** A skip here is the correct
  result, not a gap.
- `test_replay.py::test_dev_slice_replay_is_byte_identical_across_runs`
  self-skips unless `services/flink-job/corpus/dev-slice.jsonl` exists (it is
  gitignored). Cutting a corpus slice requires live capture and belongs to
  Part B.

---

## Part A — offline validation (development workstation)

### A1. Preconditions

Use the virtual environments that already exist for each service. Do not
install packages: this feature changes no dependency manifest, so a missing
package means the environment is wrong, not that a pin moved.

```bash
# stream-monitoring
cd services/stream-monitoring
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
python --version                   # expect 3.11.x
python -m pytest --version
```

```bash
# flink-job (pure modules only)
cd services/flink-job
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
python --version
```

Record the revision under test and confirm the diff is confined to the feature's
surfaces:

```bash
git rev-parse HEAD
git --no-pager diff --stat main...HEAD
```

There must be **no** change to `requirements.txt`, either `Dockerfile`,
`infrastructure/postgres/init.sql`, `seed_twitch_tokens.py` `REQUIRED_SCOPES`,
or the `chat-messages` schema. Any of those appearing in the diff is a scope
escape.

### A2. Stream-monitoring unit validation

```bash
cd services/stream-monitoring
python -m pytest -q test_stream_monitoring.py test_desired_set_store.py
```

While iterating on one cluster, narrow with `-k`:

```bash
python -m pytest -q test_stream_monitoring.py -k "Pool or Coverage or Suppression or Threshold"
```

The feature-specific assertions must cover, without a broker, a socket, or a
Twitch call:

1. **Pair creation** — a new channel produces exactly two subscriptions, one of
   each coverage type, and both ids are tracked independently.
2. **Session packing** — 150 channels fill one session to 300 subscriptions;
   the 151st channel routes to another connection; mixed types coexist on one
   session.
3. **Partial coverage, both directions** — chat present without notification,
   and notification present without chat; each converges by creating only the
   missing type, and never re-creates or duplicates the surviving one (FR-002,
   data-model I2).
4. **Enumeration** — `list()` joins both type walks and yields a channel only
   when both are `enabled`; a failure in **either** walk marks the enumeration
   incomplete so drops stay held back (research R1).
5. **409 adoption for either type** — a conflict on chat and a conflict on
   notification each adopt the existing subscription of that type on a live
   session, and refuse to claim one on a session the pool does not hold.
6. **Delete** — dropping a channel deletes both types; "already gone" is
   success; a partial delete failure leaves the surviving slot for the next
   pass.
7. **Revocation** — revoking one type leaves the other in place and moves the
   channel to a partial state; a revoked id the pool has never seen (rotated by
   a reconnect) still resolves to the right channel **and type**.
8. **Reconnect staleness** — a rotated subscription id and a changed session id
   are detected per type; a chat-only channel must **not** read as current for
   the notification type (this is the `_connection_holds` type-filter bug the
   plan calls out).
9. **Socket death and rebalance** — retiring a connection clears both slots for
   its co-located channels; mid-ramp reconnect does not oversubscribe a session.
10. **Auxiliary refusal** — a 403 on the notification type while chat is live
    does not mark the channel refused, keeps chat, and reports
    `degraded_chat_only` (research D2). The hold-off is bounded: repeated
    notification creates are suppressed for `AUXILIARY_REFUSAL_RETRY_SECONDS`
    (3600 s) and no longer; the channel is eligible again immediately after a
    websocket reconnect or connection retirement; a successful create or 409
    adoption clears the state; and while the hold-off is active the channel is
    reported as actual-but-degraded so the reconciler does not hot-loop
    (FR-001, NFR-003, SC-001).
11. **Units** — occupancy counts subscriptions; the coverage gauge counts
    channels; the two never mix (FR-015, data-model I4).
12. **Capacity arithmetic** — entry 400, retention and maximum 450: a fresh
    channel ranked 401-450 does not enter, an incumbent at that rank is
    retained, and the desired set never exceeds 450; at the maximum, 450
    channels ⇒ exactly 900 subscriptions, no connection above 300, zero free
    slots, and the 451st candidate is not admitted (FR-013, FR-014, SC-006).
12a. **Exact-capacity placement and legibility** — placement co-locates a pair
    on an existing connection, grows while fewer than three connections exist,
    and only at the maximum or after growth fails places one slot on each of
    two connections when the pool has at least two usable slots. The split
    reservation is atomic and retains the successful half on partial failure;
    a hard capacity exhaustion raises the distinct capacity error and
    classification without arming a transient growth backoff and without
    writing the durable refusal cache; `full_at` is cleared and re-evaluated on
    reconnect and retirement, and a below-cap full connection is exposed
    (NFR-001, data-model I25-I27, decision 28).
13. **Mapping and publication** — the three trigger notices map to contract
    records keyed by `broadcaster_id`; `unraid`, `sub`, `resub`, an unknown
    category, and an absent `notice_type` produce nothing; missing identity or
    unparseable timestamp produce nothing and increment the malformed counter
    (FR-005, FR-017).

### A3. Flink pure-module validation

```bash
cd services/flink-job
python -m pytest -q test_spike_detector.py test_replay.py test_clip_attempt.py
```

These three files import no PyFlink and start nothing, so they always run.
`test_spike_detector.py` is the **non-skippable** evidence for this feature's
suppression arithmetic *and* for its source configuration.

`test_clip_detector.py` imports `clip_detector_job`, which imports PyFlink. It
uses fakes and starts no cluster and no MiniCluster, so it is safe to run **if**
the pinned `apache-flink==1.18.0` is already installed in this environment —
that package being present is its **only** condition. Otherwise skip it here and
run it in Part B. Do not install PyFlink to make it run, and do not report its
absence as coverage of the arithmetic or the source settings: those are asserted
in `test_spike_detector.py`, which does not skip.

Required assertions:

1. **Notice-bounded interval transition** — an overlapping extending notice
   preserves/minimizes `suppress_from_ms` and advances `suppress_until_ms`; an
   earlier/equal candidate is a complete-state no-op; a notice at or after the
   old deadline starts a new interval at its occurrence; duplicate application
   is idempotent. Arbitrary ordering must preserve the maximum deadline, but
   the test must not assert full-state order independence (data-model §3.1).
2. **Per-category windows** — gift 120 s and raid 180 s by default, both
   configurable; the later candidate wins when they overlap (SC-005).
3. **Gate predicate** — the **peak** second decides, not the report second, and
   is gated exactly when
   `suppress_from_ms <= peak_second * 1000 < suppress_until_ms`. A pre-notice
   peak remains eligible even if reported later; the notice boundary is
   inclusive and deadline boundary exclusive (research D5, decision 22).
   Absent state never suppresses (FR-011).
4. **Excluded categories** — a record with a non-trigger `notice_type` is
   ignored by the consumer as well as the producer.
5. **Malformed records** — bad JSON, unknown `schema_version`, wrong field
   types, and `occurred_at_ms` one millisecond beyond
   `consumer_receipt_ms + 30_000`: each ignored, counted, and never raised.
   Before watermark generation, the source timestamp assigner uses
   `occurred_at_ms` at exactly `source_clock_ms + 30_000`, but uses Kafka
   `record_timestamp` for +30,001 ms and for missing/unreadable values without
   rewriting the payload. The over-future original payload is then checked
   after decode/field validation and produces the existing `reason="fields"`
   warning/count, no delivery observation, and no state access.
   Separately, the chat timestamp assigner accepts only plain `int` (not
   `bool`) `sent_at` through `source_clock_ms + 30_000`. Missing/null, string,
   float, bool, and +30,001 ms values use Kafka `record_timestamp` for event
   time while the original chat record continues through the pipeline; no chat
   record is rejected or dropped by this fallback.
6. **Suppression source settings** — the pure `SuppressionSourceSettings`
   construct in `spike_detector.py` names the `suppression-events` topic,
   `latest()` starting offsets, bounded out-of-orderness equal to
   `WATERMARK_OUT_OF_ORDERNESS_SECONDS`, `SUPPRESSION_IDLENESS_SECONDS = 5`
   strictly below chat's 10 s, 4 expected partitions matching parallelism 4,
   `delivery_lag_warn_seconds=30`,
   fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`, and
   `checked_in_gating_enabled=False`. `SuppressionConfig.from_env()` remains
   the runtime reader and its gating code default remains `true`; the future
   trust bound has no environment variable. The timestamp assigner and
   operator use the same injectable/current receipt/source wall-clock basis.
   Asserted here, with no PyFlink import (research D16).
7. **Delivery classification** — capture `consumer_receipt_ms` from an
   injected clock at `process_element2` receipt and classify
   `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`: at or below
   the warning threshold is healthy, above it is lagging, and no record at all
   produces no sample and no health claim. Include a case with small producer
   delay but injected consumer delay above 30 seconds, plus negative raw age
   at exactly 30 seconds future accepted and clamped to zero with a structured
   clock-skew diagnostic, and one millisecond beyond rejected as malformed
   before observation/state. Optional `received_at_ms` remains diagnostic-only
   (NFR-005, FR-017, research D13).
8. **Watermark attachment wiring** — where `apache-flink==1.18.0` is already
   installed, assert against fakes that `clip_detector_job` passes
   `WatermarkStrategy.no_watermarks()` to `env.from_source` for **both** the
   chat and suppression sources and then calls
   `assign_timestamps_and_watermarks(real_strategy)` on each returned stream,
   with each real strategy built in the exact binding order bounded
   out-of-orderness → `with_idleness()` → `with_timestamp_assigner()` last.
   PyFlink 1.18's `with_idleness()` otherwise returns a fresh wrapper and drops
   an assigner stored earlier (research §4.1.2, decisions 25-26). This is a wiring assertion
   only. It does **not** show that PyFlink executed the assigner, that
   watermarks flowed from the assignment operator, or that idleness applied per
   subtask, nor measure the two added parallelism-four Python stages'
   TaskManager process/RSS cost; those are E3, and reporting them from this
   test is a defect.

### A4. Emission-gating equivalence (the SC-004 check)

The decisive offline evidence. Replay one fixture twice — once with an empty
suppression input, once with notices that cover the same spikes — and compare
detector state, not just output:

```bash
cd services/flink-job
python -m pytest -q test_replay.py -k "suppress"
```

The comparison must assert **all** of:

- identical per-second `message_count` and baseline readings;
- identical `hold` trajectory (open, peak, close seconds);
- identical `last_fire_second` trajectory (research D6);
- identical `expired_buckets` sequences;
- the only differences are: the suppressed run yields no clip for the covered
  spikes, and records one suppression metric increment and one structured log
  per suppressed would-have-clipped spike (FR-012).

Also assert, in the same file:

- **Silent suppression stream** — chat evaluations continue at the same seconds
  when the suppression input produces nothing for the whole replay (research
  R3, in the harness's simplified watermark model — the real two-input
  behaviour is E3 in Part B).
- **Sparse idle → active re-entry** — after a long silence, a single isolated
  notice may hold the harness's simplified two-input watermark, but by no more
  than `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS`
  before the input is treated as idle again; a sustained run of notices advances
  the watermark normally and never reaches that bound (research R10,
  data-model I16). This is the simplified-model case only; the deployed
  measurement is E3.
- **Late notice** — a notice delivered after a spike has already emitted does
  not retract it, and its unexpired deadline applies to later decisions only
  (FR-018).
- **Pre-notice peak with later report** — a spike peaking before the notice
  remains eligible when its hold reports after the notice; a peak exactly at
  the notice is suppressed, while a peak exactly at the deadline is eligible.
- **Future trust bound** — a record exactly 30 seconds ahead is accepted with
  its occurrence time assigned upstream, age zero, and the skew diagnostic;
  one millisecond farther ahead is assigned Kafka record time upstream without
  payload mutation, then rejected, counted and logged downstream with no
  delivery sample or state access. Replay performs assignment before rejection
  and asserts that the combined watermark is monotonic and never advances from
  the untrusted occurrence value. The harness models the assigner directly, so
  it also cannot show that the real job reaches its assigner; that depends on
  the post-source `assign_timestamps_and_watermarks` wiring (A3 item 8) and is
  proven only by E3.

Determinism is still a hard requirement: running the harness twice over the
same input must diff empty.

### A5. Static configuration assertions

These are assertions in tests over the checked-in files, not deployments.

```bash
cd services/stream-monitoring
python -m pytest -q test_stream_monitoring.py -k "Threshold or Compose"
```

This stream-monitoring selection owns:

- `docker-compose.yml` `stream-monitoring` carries the checked-in safe default
  `JOIN_THRESHOLD=400`, `LEAVE_THRESHOLD=${LEAVE_THRESHOLD:-400}`, and
  `AUXILIARY_REFUSAL_RETRY_SECONDS=3600`. Pure desired-set tests separately
  assert the operator-selected final target: entry 400, retention and maximum
  450.
- The pure desired-set assertions cover the asymmetry the thresholds create —
  a channel ranked 401-450 does not enter a set it is not already in, an
  incumbent at that rank is retained, and the set never exceeds 450 — plus
  exact capacity: 450 × 2 = 900, no connection above 300, zero free slots, and
  the 451st channel excluded.
- `kafka-init` creates `suppression-events` with **4** partitions, matching
  `FLINK_PARALLELISM=4`.

```bash
cd services/flink-job
python -m pytest -q test_spike_detector.py -k "Settings or Compose"
```

This PyFlink-free Flink selection owns:

- `SUPPRESSION_GIFT_WINDOW_SECONDS`, `SUPPRESSION_RAID_WINDOW_SECONDS`,
  `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS=30`, and `SUPPRESSION_GATING_ENABLED`
  are present on **both** the `flink-jobmanager` and `flink-taskmanager`
  environment blocks.
- The checked-in value of `SUPPRESSION_GATING_ENABLED` is **`false`** on both
  blocks, while the code-level `SuppressionConfig` default is `true`. A deploy
  is therefore inert until an operator flips the compose value, and only after
  E1, E2a, E2b, and E3 have passed (decisions 21 and 27).
- The pure `SuppressionSourceSettings` fields are
  `delivery_lag_warn_seconds=30` and
  `checked_in_gating_enabled=False`; expected partitions and expected
  parallelism are both 4. The pure
  `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` contract constant is asserted
  separately and has no compose/environment entry.
- `FLINK_PYFILES` is unchanged (no new module).

Neither selection starts a service or infrastructure process.

### A6. What Part A can and cannot conclude

Part A can conclude: the pool's two-slot state machine is correct and
capacity-safe under deterministic fixtures, including the bounded
auxiliary-refusal hold-off, the 400/450 entry-retention asymmetry, exact
450 × 2 = 900 occupancy, split-pair reservation and its atomicity, the distinct
capacity classification, and `full_at` clearing/exposure; the mapping and the
contract agree and the producer keeps key/payload equality; the gate arithmetic
is correct; the suppression source settings are exactly as designed; and gating
changes emission and nothing else.

Part A **cannot** conclude anything about: real Twitch behaviour with mixed
subscription types, subscription cost, real convergence at 400 or 450 channels,
whether the account holds enabled subscriptions this feature did not create,
real desired-set churn, PyFlink's actual two-input
watermark and idleness behaviour — including the idle → active re-entry bound,
which A4 exercises only in the harness's simplified model, whether the
assigner-last builder order and post-source attachment cause the Python
assigners to run, and the process/RSS cost of the two Python stages, all of
which A3 item 8 checks only as wiring — delivery lag,
or whether the window defaults are well tuned. Those are Part B, and reporting
them as passed from Part A evidence is a defect.

---

## Part B — deployed validation (configured machine, operator-run)

Run in this order. Each gate blocks the next.

### B0. Ramp down first, on the current revision

**Before deploying the feature revision**, set `JOIN_THRESHOLD=400` and
`LEAVE_THRESHOLD=400` on the **existing** single-subscription revision and let
the desired set converge:

```bash
docker compose up -d --force-recreate stream-monitoring
```

Wait until `eventsub_subscription_count` and `ZCARD chat:desired` both settle at
~400 before continuing.

Why this comes first: the desired set is stored in Redis, and on restart the
reconciler converges to whatever is already there while the poller only rewrites
it on its next 120 s tick. Deploying the two-subscription transport against a
Redis set still holding ~800 channels would ask for ~1,600 subscriptions against
a 900 ceiling. Lowering the ramp on the old code is always safe (400 × 1 = 400)
and guarantees the new transport never sees an oversized set (plan, "The
deployment-ordering hazard").

**This step is not gated by E1.** At one subscription per channel the
arithmetic is unconditionally safe regardless of what E1 later shows. E1 gates
dual-coverage sign-off and enabling gating — B2 onward — not this ramp-down.

### B1. Deploy the feature revision with gating off — and with retention still 400

Deploy the feature revision. `SUPPRESSION_GATING_ENABLED=false` is already
checked into both Flink blocks in `docker-compose.yml`, so no operator action is
required to keep gating off; leave it alone.

Leave the checked-in safe default
`LEAVE_THRESHOLD=${LEAVE_THRESHOLD:-400}` in force for this stage. First
dual-coverage convergence is then 400 channels / 800 subscriptions, with
roughly 100 slots of cushion, which is where E1 and E2a are taken. Proving the
dual transport and reaching exact capacity are two separate risks and are never
taken in the same step (decision 27). The pool converges to ~800 subscriptions
over the same 400 channels, and detection behaviour must be indistinguishable
from before the deploy.

### B2. E1 — capacity and cost, at the first opportunity

Enumerate subscriptions and confirm both types exist on live sessions, and that
`total_cost` is still 0 against `max_total_cost` 10. Check this as early in
convergence as possible: **a non-zero cost blocks dual coverage and therefore
the whole feature** (research R6), and the correct response is to revert the
transport revision immediately, following the rollback order in B7. The
preliminary 400/400 ramp-down from B0 stays in place and does not need to be
reversed.

### B3. Confirm the ramp is in effect

Thresholds are 400/400 at this stage — the checked-in safe default. Confirm
they survived the deploy and that the desired set is capped at 400.

### B4. E2a — dual coverage at 400 channels, with a cushion

Confirm:

- `eventsub_channel_coverage{state="complete"}` equals `ZCARD chat:desired`;
- `eventsub_subscription_count` is twice that and no more than 800;
- no connection exceeds 300 (`eventsub_connection_occupancy`);
- roughly 100 subscription slots remain free at this stage;
- partial-coverage series settle back to zero after convergence;
- the monitored set does not exceed 400 while retention is still 400.

Read `desired_set_churn_total` here as **advisory context only**: it shows how
much the monitored set moves, and nothing about it gates progression. The
amended thresholds restore a 50-channel retention band, so no observation
window and no numeric bound apply (NFR-007, SC-011, decision 27).

### B4a. Sweep the account, then ramp retention to 450

Before spending the last 100 slots, enumerate **every** enabled subscription on
the client-id / user-id pair and remove anything the pool does not own — an
earlier revision's leftovers, an orphan from a failed delete, another process's
subscription. At exact capacity such a subscription is indistinguishable from a
defect and consumes a slot the model has already allocated (research R15, R17).

Then set the deployment environment to `LEAVE_THRESHOLD=450`, the
operator-selected final target; the checked-in safe default remains 400.
Changing only the leave threshold admits no current rank-401-450 channel.
Starting from 400, the retained band grows only through ranking turnover — new
top-400 channels enter while displaced incumbents remain at ranks 401-450 — or
through an explicit safe validation seed.

### B4b. E2b — relational convergence and conditional exact-capacity drills

Read these as **relations, not approximations** at every desired count ≤450:

- `eventsub_channel_coverage{state="complete"}` **== desired**;
- `eventsub_subscription_count` **== 2 ×**
  `eventsub_channel_coverage{state="complete"}`;
- every `eventsub_connection_occupancy{connection}` **≤ 300**, and their sum
  **== `eventsub_subscription_count`**;
- summed `eventsub_connection_free_slots{connection}` **== 900 −
  `eventsub_subscription_count`**;
- `eventsub_connection_full{connection}` and
  `eventsub_connection_full_below_cap{connection}` agree with usable free
  slots and provider-reported stranded capacity;
- the 451st qualifying channel is **excluded**.

Exact-450 validation is conditional on 450 valid incumbents accumulating
through ranking turnover or being supplied by an explicit safe validation
seed. Once that condition holds, additionally require
`eventsub_subscription_count == 900`, summed free slots `== 0`, and desired
`== 450`, then exercise the exact-capacity paths (decision 28):

1. **Split placement.** After co-location is unavailable and growth is
   impossible at the three-connection maximum or has failed, with at least two
   usable slots in the pool, a channel's pair is placed one slot on each of two
   connections and both halves are recorded.
2. **Fragment replacement.** Remove one half of a split pair and confirm the
   channel converges back to `complete` without exceeding 900 subscriptions.
3. **Capacity classification.** Force a create at a genuinely full pool and
   confirm it is reported under its own capacity classification — not as a
   provider refusal, and not as a transient fault — that no channel is written
   to the durable refusal cache, that no existing coverage is evicted, and that
   no transient growth backoff is armed.
4. **`full_at` re-evaluation.** Confirm a connection reporting full **below**
   300 is visible as stranded capacity, and that reconnecting or retiring that
   connection clears and re-evaluates the condition.

### B5. E3 — the watermark gate

Four measurements, all required:

1. **Prolonged silence.** With the suppression topic silent for at least one
   hour, confirm chat detection continues and Flink's source watermark lag on
   the chat input is unchanged from the pre-007 baseline. This is the check that
   the second input never becomes the binding minimum in steady state
   (research R3).
2. **An isolated notice after silence.** Deliver or wait for a single notice
   following that silence and confirm the operator's watermark is held for no
   longer than
   `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before
   the source returns to idle. This is the idle → active re-entry case
   (research R10, data-model I16); silence alone does not exercise it, and E3 is
   not complete without it.
3. **An over-future notice cannot poison event time.** With controlled
   timestamps, deliver a record beyond `source_clock_ms + 30_000`, then let
   chat idle and resume. Confirm the suppression stream used Kafka record time,
   the connected operator watermark did not jump to the untrusted occurrence
   value, remained monotonic, and real-time timers continue after chat resumes.
4. **The assigners actually run and remain bound after idleness.** Confirm from the deployed job graph and
   metrics that watermarks originate from the post-source
   `assign_timestamps_and_watermarks` operator on **both** streams, that chat
   event time tracks trusted plain-int `sent_at` rather than broker ingestion
   time, that suppression event time tracks trusted `occurred_at_ms`, and that
   each strategy retained its assigner after `with_idleness()` because the
   assigner was bound last. Exercise chat missing/null/string/float/bool and
   exact +30,000/+30,001 ms cases, confirming fallback uses Kafka record time
   without losing chat and cannot poison the combined watermark. Confirm each
   assignment subtask still corresponds to exactly one topic partition at
   parallelism 4, and record TaskManager Python process count and RSS for the
   two added Python assignment stages. Without this, measurements 1-3 describe
   Kafka record time and prove nothing about the intended contract (research
   §4.1.2, R13-R14, decisions 25-26).

**Do not enable gating until all four pass.**

### B6. Enable gating, then E4

Only after E1, E2a, E2b — including every exact-capacity drill — and all four
parts of E3 have passed, change the checked-in compose value
to `SUPPRESSION_GATING_ENABLED=true` on both Flink blocks and recreate them.
Then capture a real gift-bomb and raid slice and confirm:

- suppression events appear on the topic for exactly the three trigger
  categories, with the right channel and a sane `occurred_at_ms`;
- `suppression_delivery_age_seconds`, computed from
  `max(0, consumer_receipt_ms - occurred_at_ms)`, is small relative to the
  120 s window and well inside `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS = 30`,
  with trusted records classified healthy rather than lagging; over-bound
  future timestamps, if observed, are visible as rejected malformed fields and
  never appear in this distribution. Confirm the source first used Kafka
  record time without rewriting the payload, and that `process_element2`
  rejected the original value with the existing warning;
- suppressed would-have-clipped spikes whose peaks fall in the half-open
  notice-bounded interval produce both required signals and no clip;
- clips outside any window, including pre-notice peaks reported later, are
  unaffected (SC-007).

### B7. E5 — rollback rehearsal

Set `SUPPRESSION_GATING_ENABLED=false` and confirm emission behaviour returns
to pre-007 with no other change. Then confirm the capacity-safe rollback order
is executable, in exactly this sequence:

1. **Gating off** — `SUPPRESSION_GATING_ENABLED=false`. For a
   detection-policy-only incident this is the whole rollback.
2. **For a capacity incident, lower retention to 400 first and reconverge** —
   set `LEAVE_THRESHOLD=400` (`JOIN_THRESHOLD` is already 400) and wait for the
   monitored set to fall to 400 channels and 800 subscriptions. This restores a
   ~100-slot cushion with no code change and leaves dual coverage intact while
   fragmentation, a stranded below-cap `full_at`, a foreign subscription, or a
   failed delete is diagnosed.
3. **Unwind the transport with thresholds at 400/400** — revert the
   two-subscription revision while `JOIN_THRESHOLD`/`LEAVE_THRESHOLD` are
   `400`/`400`. The single-subscription revision needs 400 of 900 slots, so
   every instant of this step is inside capacity.
4. **Wait for convergence** — continue only once `channel.chat.notification`
   subscriptions no longer appear in the enumeration,
   `eventsub_subscription_count` has fallen to the desired channel count (400,
   not 800), and coverage and desired-set metrics are stable.
5. **Only then restore the single-subscription ramp.**

The governing invariant: **the retention threshold is never above 450 while two
subscriptions per channel are live, and it must be back at 400 before the dual
transport is unwound.** Relaxing the threshold first is the unsafe reverse
order — at two subscriptions per channel a retention threshold above 450
permits more than 900 subscriptions, and unwinding from a 450-channel set hands
the single-subscription revision a set larger than its ramp (research R11a).

The topic may be left in place.

---

## Evidence status

| Evidence | Where it is produced | May be claimed from Part A? |
|---|---|---|
| Pool two-slot correctness and capacity arithmetic, including the 400/450 entry-retention asymmetry, exact 450 × 2 = 900, split-pair reservation, capacity classification, and `full_at` handling | A2 | Yes |
| Bounded auxiliary-refusal hold-off, expiry, and reconnect re-eligibility | A2 | Yes |
| Notice mapping, contract conformance, malformed handling, producer key/payload equality | A2 | Yes |
| Gate arithmetic, notice-bounded overlap, duplicates, both interval boundaries, and fixed future-time trust bound | A3 | Yes |
| Suppression source settings and `suppression_delivery_age_seconds` classification, PyFlink-free | A3 | Yes |
| Gated vs ungated state equivalence (SC-004) | A4 | Yes |
| Sparse idle → active watermark bound, simplified harness model only | A4 | Yes, as the simplified model; **not** as PyFlink runtime behaviour |
| Configuration values, checked-in `SUPPRESSION_GATING_ENABLED=false`, topic/partition settings | A5 | Yes (as file assertions, not as deployment) |
| PyFlink operator/topology wiring against fakes | A3 (`test_clip_detector.py`) | Only when the pinned `apache-flink==1.18.0` is already installed; otherwise it is pending, and it is never a substitute for the A3 pure assertions |
| Assigner-last strategy construction and post-source `assign_timestamps_and_watermarks` attachment on both streams | A3 item 8 | Only as wiring; **not** as proof that the Python assigners execute, that event time follows trusted payload time, or that the two Python stages have an acceptable deployed process/RSS cost |
| **E1** mixed types live, subscription cost | B2 | **No** |
| **E2a** dual coverage at 400 channels / 800 subscriptions with a cushion | B4 | **No** |
| **E2b** relational convergence at every desired ≤450, plus conditional exact capacity after 450 valid incumbents accumulate or are safely seeded: 900 subscriptions, ≤300 per connection, zero free slots, 451st excluded, grow-before-split placement, fragment replacement, capacity classification, and `full_at` re-evaluation (SC-006) | B4b | **No** |
| **E3** two-input watermark/idleness, isolated-notice bound, both streams' exact/type/future fallback behavior without chat loss, proof that assigner-last strategies run post-source with one partition per subtask, and TaskManager Python process-count/RSS impact for the two parallelism-four stages | B5 | **No** |
| **E4** trusted-record delivery age, downstream malformed-future visibility after source timestamp fallback, real-burst confirmation for SC-003, and notice-bounded window-default tuning/adequacy for SC-005 | B6 | **No** |
| **E5** rollback rehearsal in the capacity-safe order | B7 | **No** |

Do not mark any Part B item complete from unit tests, fixtures, replay output,
or reasoning. Report them as pending until the operator produces them on the
configured machine.

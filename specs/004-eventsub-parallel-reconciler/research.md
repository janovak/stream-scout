# Research: EventSub Ingestion with a Parallel Reconciler

**Date**: 2026-08-27. Phase 0 measured 2026-08-28. Numbers are measured unless
the text says "projected". The measured spike data lives in
`specs/003-detector-scale-fanout/research.md` §1. This document does not repeat
it; it records the decisions taken from it.

## Phase 0 — measurement gate (COMPLETE, 2026-08-28)

Two decisions (D3, D4) and one risk (R1) rested on comparisons nobody had made.
Phase 0 made them with throwaway scripts running both transports live against
the top ~500 channels (auth user `48754970`, a separate `user:read:chat` token
that did not touch the running IRC production token). **All four passed. The
T006 gate is met — Phase 2 is unblocked.**

| Task | Result | Feeds |
|---|---|---|
| **T001** | `metadata.message_timestamp − tmi_sent_ts` over 24,473 joined messages (15 channels, 12 min): min 0, **median +1 ms**, p95 1 ms, max 1 ms, mean +0.5 ms, **0 negative**. The envelope timestamp *is* the IRC send time — same Twitch-assigned instant, not "dispatch vs send". | D3, FR-009, **T006** |
| **T002** | EventSub delivery lag `local_recv − envelope_ts` over 59,405 messages (414 channels, 8 min): p50 163, p95 217, p99 257, p99.9 415, p99.99 1,255 ms. **Over 2,000 ms: 1 message = 0.0017%.** Over 1,000 ms: 0.039%. | D4, SC-005 |
| **T003** | 250 creates at concurrency 1 / 5 / 10 / 20 → **zero 429s at every level** (2.6 / 11.5 / 6.5 / 44 subs/s; POST p50 ~330–385 ms). The rate limit is not a concurrency ceiling. | D2 |
| **T003b** | 550 creates, concurrency 15, retry + 10 s backoff on 429: **first 429 after 364 successful creates** (t ≈ 14 s), burst budget ≈ 360–420, **time to 500 enabled = 40.6 s**, time to 550 = 53.2 s, 180 total 429s all retried through. A second cold ramp on an already-drained budget still converged (500 in 125 s, 796 429s). | D2, SC-001 |
| **T004** | 500-channel cold ramp, 12,555 events in the ramp window: **0 × `received event for unknown subscription`, loss 0.00%.** Per-channel first-60 s vs steady (5–6 min) rate ratio 1.08 — opening baseline not depressed. | R1, **FR-014** |

**T005**: this section + D2/D3/D4/R1 below are the Phase 0 commit.
**T006 GATE**: median dispatch-vs-receive offset = **1 ms**, far inside the 2 s
watermark → **PASS. Phase 2 unblocked.**
**FR-014**: ramp loss is 0.00% and the opening baseline is not depressed →
**the warm-up gate is NOT built. Skip T040.**

### Caveats carried forward

- T002 ran at **414 channels, not 500** — 86 creates hit 429 at concurrency 10
  before the harness added backoff. 414 ≈ the 394-channel spike; the lag
  distribution is the relevant output and it is clean. Re-confirm the tail at a
  full, stable 500 during Phase 5 (T044 / T038).
  **Outcome (2026-08-29)**: T044 confirmed the transport at a stable 500 for
  31 minutes, and T038 measured the late rate at 21-23 channels as 0.0030%. The
  tail was **not** re-measured at 500, because that needs Flink in the path.
  See the honesty note at the end of Phase 5.
- T002's lag tail includes scheduling jitter from a single-process measurement
  consumer (one asyncio loop doing receive + bookkeeping). The real Kafka
  producer path is lighter, so 0.0017% over 2 s is an **upper bound**.
- T001's dedicated offset number (±1 ms) is not subject to that jitter — it is
  a difference of two Twitch-supplied timestamps, independent of receive time.
- twitchAPI 4.5.0 registers the notification callback synchronously with the
  create POST returning (`websocket.py` `_subscribe`), so the ramp race the 003
  spike saw does not reproduce here. If the library is upgraded, re-check T004.

## Phase 2 — T028a go/no-go gate for IRC removal (2026-08-28)

Phase 3 deletes IRC outright. R5 accepted that there is no intermediate
fallback — `git revert` of the branch is the fallback — so this gate is the
evidence taken before the thing we would fall back to is deleted.

EventSub replaced IRC in production at **16:43:40Z**. The soak ran to
**18:43:40Z** on the post-code-review build, sampling every minute.

| Condition | Status |
|---|---|
| **T006** event-time gate | **PASS** (Phase 0): median dispatch-vs-send offset 1 ms over 24,473 joined messages |
| **T002** delivery-lag tail under 2 s | **PASS at the operating point, NOT re-confirmed at 500.** See below |
| **≥ 2 h live traffic, `eventsub_subscription_count` stable** | **PASS**: 92 min at 19–21 channels, 91 samples, `subs == desired` on every one |
| **T021** schema test | **PASS**: green in `test_stream_monitoring.py`, and confirmed live (below) |

### What the soak measured

- **Subscription stability**: 91 one-minute samples, `eventsub_subscription_count`
  equal to `ZCARD chat:desired` in **all 91**. Deviation 0%, against the SC-004
  budget of ±1%. The count moved 19 → 20 → 21 with the desired set and never
  lagged it by more than one sample interval.
- **Failures**: `subscription_create_failures_total` stayed empty for the whole
  soak — no 429, no refusal, no error. `eventsub_refused_at` is still NULL for
  every row, so nothing was wrongly marked.
- **Reconcile cost**: 1,383 passes; 1,017 of the first 1,020 under 0.5 s, the
  rest under 1 s apart from the 3.4 s cold start. Mean 113 ms.
- **Throughput**: 231,095 messages into `chat-messages` over the soak,
  2,465/min average (2,468–2,756 across the three 30-minute marks).
- **Resources**: 50.8 MiB RSS, 20 file descriptors, 17 threads, flat. No leak
  signature from the retire path.

### The reconnect path was exercised for real

At **16:52:42Z** the socket missed its keepalive and the library reconnected,
re-subscribing all 18 channels with **new subscription ids**. This is the path
the post-review fixes address (`_live_subscription_ids`, `_forget_unrecognised`),
and it is the one no unit test can fully stand in for.

Checked directly against Twitch afterwards by walking the subscription pages:
**20 enabled, all on one session — exactly matching `eventsub_subscription_count`
20 and `eventsub_connection_occupancy{connection="0"} 20`.** Nothing leaked,
nothing duplicated, and the reconciler kept tracking the desired set across it
(18 → 19 → 20).

Incidental: `get_eventsub_subscriptions().total` reported 20 correctly here.
That does not soften D6 — the spike caught it reporting 300 while the pages held
396, and 20 subscriptions is far too few to reproduce that. Page counting stays.

### Detection is unchanged on EventSub data (FR-008, D3 in production)

The strongest available evidence that the schema and the event-time semantics
survived the cutover is that the Flink job never noticed it:

| | Clips | Rate | Mean intensity |
|---|---|---|---|
| 3 h of IRC before cutover | 137 | ~45.7/h | 5.63 |
| First hour on EventSub | 40 | ~40/h | 5.84 |

Anomalies fire with sane event time (`peaked at … 5s ago`) across 17
broadcasters. This is a live version of the comparison T039 plans to make by
replay, and it is a stronger one: the same job, the same tuning, only the
transport changed.

### R1 needs one qualification

One `received event for unknown subscription` was logged at 16:52:48Z, inside
the resubscribe window after the reconnect. Phase 0 T004 measured **0** of these
across a 500-channel cold ramp (12,555 events) and recorded ramp loss as 0.00%.

That conclusion is not contradicted, but "0.00%" should be read as "below the
resolution of that measurement", not "impossible". The race is real and narrow:
Twitch activates a subscription server-side before the create POST's response
reaches us and the callback is registered, so an event arriving in that window
has nowhere to go. The cost is one chat message, which is immaterial against a
baseline built over five minutes. **R1 stays closed; no warm-up gate.**

### What is NOT met: the 500-channel tail

`T002` measured the delivery-lag tail at **414** channels, not 500 (Phase 0
caveat, carried above). Production runs at `JOIN_THRESHOLD` 15 /
`LEAVE_THRESHOLD` 30 — 19 to 21 channels — so this soak re-confirmed the tail at
**~20 channels** (p50 151 / p95 205 / max 288 ms, sampled over 249 messages
against a live pool), not at 500.

Raising production to 500 is a capacity change, and `research.md` Out of Scope
already records why that bites: a 500-channel set detects far more anomalies than
the clip budget can act on. **The operator's decision (2026-08-28) is to leave
production at 15/30 and keep the 414→500 re-check where `tasks.md` already puts
it — Phase 5, T038 and T044.**

### Verdict

**Three of the four conditions are met outright. The fourth is met at the
operating point and deferred at 500 by decision, not by oversight.**

For Phase 3 — deleting IRC — the risk R5 names is "EventSub misbehaves in
production". At the scale production actually runs, that risk is now evidenced
against: two hours of exact subscription tracking, a real reconnect survived
with Twitch and the service in agreement, zero failures, and unchanged detection
output. **Phase 3 is unblocked at the current operating point.** It does not
carry a claim about 500 channels; that claim is Phase 5's to make, and the
414→500 gap is the one thing standing between this gate and an unconditional
pass.

## Phase 4 — watermark move (COMPLETE, 2026-08-29)

**T036**: `WATERMARK_OUT_OF_ORDERNESS_SECONDS` 1 → **2** in
`services/flink-job/spike_detector.py`, with the comment block rewritten to
cite T002 rather than KNOWN_ISSUES Issue 4. `clip_detector_job.py` and
`tools/replay.py` import the same constant, so all three moved together.
`WATERMARK_IDLENESS_SECONDS` was not touched and is still 10.

One stale literal was corrected alongside it: `clip_detector_job.py`'s
"a message for second now_seconds+1..+5" comment still carried the pre-2026-08-27
value of 5. It now names the constant instead of a number.

**Deployed 2026-08-29 02:52 UTC** (job `586d5750`). Application Mode: starting
the `flink-jobmanager` container *is* starting the job, so a restart reruns it.
No state to migrate, because checkpointing is off.

**Do not treat "restart picks up the edit" as a rule.** Those four job files are
bind-mounted **individually** (`docker-compose.yml`, the `flink-jobmanager` and
`flink-taskmanager` `volumes:` blocks), and a single-file bind mount follows the
inode — the same trap recorded under "Deployment trap found while verifying"
below, which had left the stream-monitoring container running pre-Phase-3 code.
It happened to work here because the edit was made in place. Any edit that
replaces the inode — `git checkout`, `sed -i`, an editor that writes and renames
— leaves the containers on the old constant while the host file reads the new
one, and `docker compose restart` will not fix it.

**So the deploy was established by checking, not by assuming**: read from inside
the running container, `WATERMARK_OUT_OF_ORDERNESS_SECONDS = 2` and
`WATERMARK_IDLENESS_SECONDS = 10`. Do that after any change to these files.

**Effect on the watermark, measured.**
`max_over_time(flink_taskmanager_job_task_operator_watermarkLag[20m])` reads
**2,356 / 2,515 / 2,804 ms** across the reporting subtasks — the out-of-orderness
term plus processing, with the idleness timeout not firing. This is the +1 s
cost the decision accepted, visible directly.

**Effect on the detection delay, measured.** 44 detections over 90 minutes:
peak-to-detection **p50 8.2 s, p90 12.0 s, p95 12.0 s, max 17.2 s, 0% over
30 s**; margin-before-peak p50 16.8 s, p10 13.0 s, min 7.8 s. The +1 s cost is
real but invisible against the two reductions that landed before it — the
5 → 1 move and the partition-count fix that stopped the idleness timeout from
firing routinely. For reference the last measurement at the 5 s value, over 554
detections, was p50 14.1 / p90 18.1 / p95 20.1 / max 28.9 s. **44 detections is
a small sample and understates the tail**; the median is the trustworthy part.
KNOWN_ISSUES.md Issue 4 carries the full comparison.

**T037**: `KNOWN_ISSUES.md` Issue 4 "Post-deploy validation" now carries the
bookkeeping for both the 5 → 1 and 1 → 2 moves. The delay figures there were
measured at 5 s; the current value subtracts 3 s from each. That section had
never recorded the 5 → 1 step either, so both gaps are closed together.

### T038 [SC-005] — the metric named in the task does not exist

`tasks.md` says to take the residual late-drop rate from Flink's
`numLateRecordsDropped` on the chat-messages source. **That metric is not
emitted by this job, and not because it is zero.** Flink reports it from
windowing operators (and CEP). `clip_detector_job.py` uses a
`KeyedProcessFunction` with per-second event-time timers and no window
operator. Confirmed against the running job's REST API: `/jobs/<id>/vertices/
<id>/metrics` lists no late-record metric on either vertex, and Prometheus has
no such series to query.

So **Flink drops nothing here** — and what happens instead is worse than
losing the record, not better. A late record reaches `process_element`, which
increments its bucket and **re-registers that bucket's timer unconditionally**
(`clip_detector_job.py:704`). Flink does not compare a newly-registered
event-time timer against the current watermark, so a timer for a second the
watermark has already passed fires on the very next `advanceWatermark`. The
late record therefore does not lose its evaluation; it forces the
already-evaluated second to be **re-evaluated backwards in time**.

That is the `hold_regressed` path: when a hold is open and the re-fired second
sits behind the hold's own recorded peak, `evaluate()` passes the hold through
unmeasured, and `clip_detector_job.py` logs a warning and increments
`hold_regressed_total` (KNOWN_ISSUES.md Issue 3, and the replay mechanism
Issue 4's "Change B" was written against).

So the quantity SC-005 bounds here is *the fraction of records that arrive
after their own bucket's timer fired* — records that cause an extra
out-of-order re-evaluation, not records that are lost. Either way it is
delivery lag beyond the watermark tolerance, and either way the budget is the
right one to hold; but the failure it buys off is a regressed hold, and that
links straight to two issues that are still open.

Measured directly on the live topic, the same way Issue 4 measured
out-of-orderness — per partition, in offset order, against a running maximum of
`sent_at`. This is normally an **upper bound**: Flink's watermark is per split
and the operator watermark is the minimum across splits, so it sits at or below
any one partition's and a record counted late here may still be in time for the
job.

**With one exception, which `with_idleness` creates.** A split marked idle
leaves the minimum, so the operator watermark can run *ahead* of that split's
own. `WATERMARK_IDLENESS_SECONDS` is 10 and a live broadcaster's partition can
go quiet on its own, so the case is real rather than hypothetical. Inside such
a window a record this method calls on-time can be late in the job, and the
headroom below is derived from a bound that does not hold there. Nothing in
this sample lands in that window as far as the `hold_regressed` evidence below
can tell, but the bound is not unconditional and should not be quoted as if it
were.

**And the constant that gap depends on is itself unmeasured.**
`WATERMARK_IDLENESS_SECONDS` is 10 because the KNOWN_ISSUES Issue 4 fix needed
something far below 60, not because anyone measured the quantity that bounds
it. Spec 004 did not change the value: it is outside this feature's scope, it
is working (5+ hours on the deployed job with zero `hold_regressed` events),
and raising it is the direction that reopens Issue 4. But the old justification
in its comment was wrong, and removing it did not supply a right one, so the
constant now carries an explicit "unmeasured, and here is what would measure
it" note.

**A split is a partition, not a broadcaster**, and getting that backwards
inverts the conclusion. `with_idleness` acts per source split; `chat-messages`
has 4 partitions and the producer keys on `broadcaster_id`, so each partition
carries roughly 5 broadcasters at the current operating point. A split goes
idle only when *every* broadcaster on it is silent for the full timeout at
once — a far shorter gap than any one broadcaster's, and shorter still as the
monitored set grows. Measuring a single broadcaster's inter-message gaps would
suggest 10 s is much too low and argue for raising it back toward 30–60 s,
which is exactly the watermark freeze Issue 4 was opened for. Measure
per-partition gaps.

Recorded here so SC-005's headroom is not read as covering a case it does not.

**Getting the test right took two corrections**, both found in code review, and
both in the direction of flattering the result. Bucket `b`'s timer is at
`b*1000` and fires when the watermark (`max_sent_at - OOO - 1`) reaches it, so
the bucket has already fired when `previous >= b*1000 + OOO + 1`, where
`previous` is the running maximum over records seen *before* this one.

- The first version tested `behind > OOO_MS` and dropped the record's offset
  within its own second, undercounting lateness by up to a full second.
- The second included the offset but clamped `previous - sent_at` to 0 for a
  record that is itself a new maximum, where the true value is negative. That
  over-counts, and only at bounds below 1 s — at 1 s and 2 s a clamped record
  would need `rem >= 1001`, which is impossible.

The figures below use the final form, comparing `previous` against the bucket
start directly.

**Three 600 s windows on the deployed 2 s watermark**, 66,154 records total.
Two later corrections to the script — the negative clamp above, and an
equal-timestamp record being miscounted as an inversion — changed only the
sub-second row and the inversion statistics. The 1 s and 2 s counts are
computed identically in all three windows and are directly comparable.

| Bound | Window 1 (27,413) | Window 2 (21,502) | Window 3 (17,239) |
|---|---|---|---|
| 500 ms | 51.21%¹ | 42.63% | 37.51% |
| 1,000 ms | 0.620% | 0.595% | 0.197% |
| **2,000 ms** | **2 (0.0073%)** | **0** | **0** |
| 5,000 ms | 0 | 0 | 0 |

¹ Window 1's 500 ms figure is inflated by the clamp; its 1 s and 2 s figures
are not — a clamped record needs `rem >= 1001` to trip those, which is
impossible.

Worst inversion 998–1,041 ms across the three. Inversion p99 100 ms, p99.9
966 ms in the fully corrected window — percentiles over the 1,472 real
inversions, not over every record. (Counting equal-millisecond records as
inversions had put that figure at 4,540 and diluted the percentiles with
zeros.)

**SC-005 budget is 0.1%. Measured at 2 s: 2 late records in 66,154 =
0.0030% — PASS**, roughly 33× under budget. Two of the three windows saw none
at all, so the honest phrasing is "a few per hundred thousand", not "zero".

**The 1 s column is the finding, and it reproduces in every window.** 0.197%,
0.595%, 0.620% — **two to six times over the SC-005 budget**, never under it.
At the previous value this pipeline does not meet SC-005 on live EventSub
traffic at all. D4 argued the 1 → 2 move bought margin against an untested
channel count; the real reason is that 1 s fails outright, which is a stronger
reason than the one the decision was written on.

The 500 ms row is not a defect and not an artefact: at a sub-second tolerance a
bucket's timer fires before the second it covers has finished arriving, so a
large fraction of records land after it. It is kept because it shows the shape
of the function, and because it is why the offset term cannot be dropped.

**The observable consequence agrees.** A late record only damages anything when
it re-fires a second beneath an *open* hold, which is the `hold_regressed`
path. Over the first 80 minutes of the deployed job (`179f85c0`) there were
**zero** `hold_regressed` warnings and `hold_regressed_total` has no series at
all — no increments. At 0.0030%, two late records in 66,154, the odds of one
landing under an open hold are small, and none did.

**Channel count: this was measured at 21-23 broadcasters, not 500.** The
500-channel measurement is carried by T044 below, which exercised the
reconciler and the sockets at 500 but not Flink — see the honesty note at the
end of Phase 5.

### T039 [US3] — replay comparison, pre- and post-cutover

`corpus/dev-slice.jsonl` (IRC, 2026-08-15, 264,867 records, 23 broadcasters,
1.17 h) against a post-cutover capture drained from the live `chat-messages`
topic (EventSub, 2026-08-29, 195,110 records, 30 broadcasters, 3.00 h). Both
replayed through `tools/replay.py` with production's detector config.

| | Pre (IRC) | Post (EventSub) | Ratio |
|---|---|---|---|
| Spikes | 56 | 43 | — |
| Per hour | 48.0 | 14.3 | 0.30× |
| Per broadcaster-hour | 2.09 | 0.48 | 0.23× |
| **Per 100k messages** | **21.14** | **22.04** | **1.042×** |
| Mean intensity | 5.20 | 5.51 | 1.06× |
| Median intensity | 4.58 | 4.79 | 1.05× |
| Messages per spike (mean) | 61.4 | 63.5 | 1.03× |

**The per-hour rates differ; the anomaly character and the volume-normalised
rate do not.** The conclusion rests on the volume-normalised row: **21.14 vs
22.04 spikes per 100k messages, 1.042×**, with every shape measure — intensity
mean, median, and messages per spike — inside 6%.

**The two per-hour rows are not comparable and should not be read as a
finding.** The capture reads an equal record count from each Kafka partition,
so each partition's slice reaches a different distance back in wall-clock time,
and the file's nominal 3.00 h span is not a span every broadcaster is present
for. Dividing the whole file by that span and by all 30 broadcasters
understates density by an unknown factor. Measured per broadcaster over each
broadcaster's *own* observed span, the median is 9,922 messages per
broadcaster-hour pre-cutover against 4,688 post — a **2.1×** density difference,
not the 4.6× a whole-file division suggests, and still not enough on its own to
account for a 0.23× spike rate. The residue is the warm-up gate: a broadcaster
present for only part of the capture spends more of its span below the 240 s of
baseline the detector needs before it can fire at all.

Volume normalisation sidesteps every one of those sampling artefacts, which is
why it is the row the conclusion is drawn from.

The replay is deterministic: two runs over the same capture diff empty, which
is the harness contract this comparison depends on.

This is the reproducible version of the live comparison the Phase 2 gate
already made (40 clips/h at mean intensity 5.84 on EventSub against 45.7/h at
5.63 over the preceding three IRC hours).

**T040**: skipped, 2026-08-28. T004 measured 0.00% ramp loss.

## Phase 5 — verification (COMPLETE, 2026-08-29)

### How the scale-dependent criteria were measured

Production runs at `JOIN_THRESHOLD` 15 / `LEAVE_THRESHOLD` 30 — 19 to 24
channels — and the operator's 2026-08-28 decision is to leave it there. SC-001,
SC-002 and SC-004 ask for 500. The operator chose (2026-08-29) a **synthetic
driver** over raising production: `services/stream-monitoring/phase5/driver.py`,
written in the style of the Phase 0 harnesses but **tracked**, unlike them —
it produced four of the seven success-criteria results below, and evidence
nobody can re-run is weak evidence. Its captured Kafka slices are gitignored
(~63 MB); the scripts are not.

It runs the **real `Reconciler` and the real `EventSubPoolTransport`** against a
500-channel desired set, so the code under measurement is the code that ships.
It leaves out everything downstream of the socket — no Kafka producer, no Flink,
no `ClipCreator`. Received messages are counted and dropped. That is deliberate:
500 channels detect far more anomalies than the clip budget can act on, which is
spec 005's problem, and pulling it in here would damage production for a
verification number.

Two facts about sharing, checked before the run:

- The driver uses `secrets/phase0_tokens.json`, which is **the same Twitch user
  as production** (48754970). Subscriptions still do not collide:
  `EventSubPoolTransport.list()` yields only subscriptions on a session the pool
  itself holds, so neither side sees or drops the other's.
- The create rate limit **is** shared — it is a per-token burst budget of roughly
  360–420 (T003b). A ramp here can 429 a production create landing in the same
  window; production backs off ~10 s and retries and never drops a channel (D2).
  The operator accepted this on 2026-08-29.

Redis is logical **db 15** on the local container, which production does not use
at all: production's `REDIS_URL` points at a different instance entirely.

### Results

| SC | Task | Result | Measured at |
|---|---|---|---|
| **SC-001** | T041 | **PASS**. Cold start to 500: 50% at 7.5 s, 90% at 48.6 s, 95% at 50.1 s, **99% at 51.1 s**, plateau 499/500. Target 60 s, ceiling 120 s | **500 channels** |
| **SC-002** | T042 | **PASS**. Poll write at 500: change-nothing p50 9.975 ms, change-everything (500 of 500 members replaced) p50 10.391 ms — **1.042×**, and an identical **6 Redis commands** per write either way | **500 channels** |
| **SC-003** | T043 | **PASS**. Zero `Bucket channel_join got rate limited`, and zero rate-limit lines of any kind | production, 21–24 channels |
| **SC-004** | T044 | **PASS**. 31 minutes, 26 one-minute samples, subscriptions **499 constant** against a desired 500. Deviation **0.2%** against a ±1% budget, 0 lost-socket events, 181,180 messages received | **500 channels** |
| **SC-005** | T038 | **PASS**. 0.0030% past the 2 s watermark — 2 records in 66,154 over three windows, budget 0.1%. The same samples put 1 s at 0.20–0.62%, two to six times over budget, in every window | 21–23 broadcasters |
| **SC-006** | T045 | **PASS**. Killed mid-ramp at ~265 of 500 with `SIGKILL`; restart converged to **499/500**, 99% at 94 s. Page walk over all **888** subscriptions on the token: **0 broadcasters holding two enabled subscriptions** on this pool's sessions | **500 channels** |
| **SC-007** | T044a | **PASS**. All five FR-012 metrics present on `/metrics` with HELP/TYPE and live values | production |

**SC-004's single delta is attributed.** The 499-vs-500 gap held constant for
all 26 samples and is one channel that refuses with `subscription missing
proper authorization` on every pass. With the driver's refusal store switched
off, that channel is retried each pass and refuses each pass: the refusal log
accumulates at 11–12 lines per minute against ~12 passes per minute, which is
exactly one channel. SC-004 allows a delta attributable to a logged refusal.

**SC-006 detail — and which check actually carries it.** The driver's own
duplicate count went through `transport.list()`, which by design yields only
subscriptions on a session *this* pool holds. The subscriptions the SIGKILLed
first process left behind sit on a session that died with it, so that count
could never have seen them: `duplicates == 0` was true by construction for
exactly the cross-restart case SC-006 names. That was found in review, and the
driver now enumerates through Twitch directly instead.

The check now walks Twitch's own pages with a **USER** token — the library
defaults to an app token, which cannot see websocket subscriptions at all and
would have returned an empty list — and scopes by session, because production
shares this token and its channels are a subset of the top 500, so counting its
subscriptions alongside ours would have reported ~21 false duplicates.

**Be precise about what that proves.** "No duplicate subscriptions" has two
readings. Taken as *no broadcaster holds two subscriptions of any kind*, it is
false by definition straight after a `SIGKILL`: the dead process's
subscriptions linger until Twitch reaps them. Taken as *no broadcaster holds
two subscriptions that can actually deliver*, it is the property worth having
— two live sockets feeding one broadcaster into the pipeline would double-count
and corrupt detection. This checks the second, which means it **cannot fail on
a cross-restart pair**, because the stranded half is by definition not
deliverable. What it can catch is the pool creating two live subscriptions for
one broadcaster within a run, which is the routing bug that would actually
hurt. The excluded population is now reported rather than assumed:
`not_enabled` counts the stranded orphans, and
`broadcasters_multi_enabled_anywhere` counts broadcasters with two or more
enabled rows across every session — a number production alone makes non-zero,
and which is expected rather than a failure.

**Re-run with the corrected check, 2026-08-29**: `SIGKILL` mid-ramp at ~265 of
500, restart converged to **499/500** with 99% at 94 s. The walk saw **888
subscriptions** on the token — 499 on this pool's sessions, 22 on production's,
and the remainder orphans stranded on the session that died with the killed
process — and found **zero broadcasters holding two enabled subscriptions on
this pool's sessions**. A result that could have failed, over a population that
includes the orphans the earlier check could not see.

The production half was checked the same way: container `SIGKILL`ed and
restarted, converged to 21 of 21, 21 enabled on one session across 21 distinct
broadcasters, no duplicate.

The 500-channel restart is slower than the 51 s cold start (74 s on the first
run, 94 s on this one) because the killed ramp had already spent part of the
create budget — the behaviour T003b measured, not a regression.

**T044a detail.** `subscription_create_failures_total` carries HELP and TYPE but
no series while nothing has failed, which is this exporter's
register-on-first-increment behaviour, not a placeholder. It was observed
populating as `{reason="refused"} 1.0` during the driver runs.

**T046**: `sent_at` is an epoch-millisecond int in every one of 21,502 live
records — 0 null, 0 non-int. Skew against the ingestion `timestamp`: min 100,
p50 165, p95 232, p99 268, max 1,256 ms. **0 records outside the 2 s
tolerance**, and 0 in the earlier 27,413-record window either.

**T047**: `test_stream_monitoring.py` — 136 passed, 8 skipped (the pre-existing
Postgres self-heal skips, which need a `twitch` role the host does not have).

**T048**: `test_replay.py` and `test_spike_detector.py` — 83 passed; whole
flink-job suite 106 passed, 4 skipped. **One test needed a change.**
`test_replay.py` fed `1002 * 1000 + 1` to push the watermark past bucket 1001.
That literal is `1001 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1` evaluated at
the old 1 s, and every other feed in the file already writes that symbolically.
It now does too. The assertions were not touched, and that expression is
identical to the old literal at the old value.

One knock-on worth naming: a *different* feed in the same test, already
symbolic, sits `WATERMARK_OUT_OF_ORDERNESS_MS + 1` past second 1000, so it
moved from bucket 1001 to bucket 1002 with the constant. The test still passes
and still proves what it was written to prove, but its comment described the
1 s behaviour and has been corrected.

### Two incidental observations

- One `received event for unknown subscription` was logged across the whole
  T045 run, inside the resubscribe window. This is the R1 race, and it matches
  the qualification already recorded under the Phase 2 gate: real, narrow, and
  worth one chat message. R1 stays closed.
- The library logged `EventSubSubscriptionError: websocket session has already
  disconnected` twice during a 500-channel ramp, from twitchAPI's own internal
  `_resubscribe`. The reconciler recovered without help: the sustain that
  followed held 499 for all 26 samples with `lost_events` 0.

### What was NOT measured at 500, stated plainly

**SC-005 is the one criterion still taken at the production operating point**
(21-23 broadcasters).
The driver deliberately has no Kafka producer and no Flink, so it cannot
produce a 500-channel late-drop number. Getting one would mean pushing roughly
25× the current traffic through a parallelism-4 job and letting `ClipCreator`
loose on a monitored set far larger than the clip budget can serve — the
change spec.md defers to spec 005.

What the 414 → 500 gap now looks like:

- T002 measured the delivery-lag distribution at **414** channels and found one
  message in 59,405 past 2 s (0.0017%).
- T038 measures the resulting late rate at **21-23** channels: 0.0030%.
- T044 confirms the transport itself is stable at **500** for 31 minutes, with
  the sockets carrying 181,180 real messages — so the 500-channel claim that is
  missing is narrowly about *Flink's* residual drop rate, not about whether the
  reconciler or the pool hold up at 500.

The lag tail did not grow between the 394-channel spike (p50 154 / p95 220 ms)
and 414 channels (p50 163 / p95 217 ms), and the 24-channel production figure
(p50 165 / p95 232 ms) sits on the same line. Nothing in three measurements at
three scales suggests the tail grows with channel count. **That is an argument,
not a measurement, and it is recorded here as one.**

### A self-inflicted incident worth recording

The first version of the T039 capture script drained every `chat-messages`
partition from its log start — roughly 4 million records — instead of bounding
the read. Pulling them at once drove the host load average to **123**, broke
Docker's DNS resolution for the `kafka` hostname
(`java.net.UnknownHostException: kafka`), timed out the Flink TaskManager's
heartbeat, and **failed the running detector job** (2026-08-29 03:20 UTC, job
`586d5750`). `NoRestartBackoffTimeStrategy` meant no in-job recovery; the
container's restart policy brought it back as job `179f85c0` at 03:21:51, about
a minute of lost detection.

Two things came out of it:

- The capture script now starts each partition a bounded number of records back
  from its high watermark. The rerun took 195,110 records at a load average of
  1.4.
- A 900 s late-record sample that overlapped the incident is **discarded**. It
  showed `timestamp - sent_at` skew at p95 309 s and max 319 s, which is the
  producer itself being starved of CPU, not a transport property. Worth noting
  that even in that window the SC-005 quantity held: 0% past 1 s and 2 s, max
  inversion 988 ms. Inversions depend on the relative order of `sent_at`, and a
  uniform ingestion delay does not create them.

### Deployment trap found while verifying

Production was running **stale service code**. `docker-compose.yml` bind-mounts
`stream_monitoring_service.py` as a single file, and a single-file bind mount
follows the **inode**. `git checkout` replaces the file rather than editing it,
so the container kept executing the Phase-2-era version — with the IRC client
still in it — for the whole time Phase 3 sat merged. `docker compose restart`
does not fix this; mounts resolve at container creation.
`docker compose up -d --force-recreate stream-monitoring` does, and was used to
deploy Phase 3 before T043 and T045 were checked, so both were verified against
the code that is actually running. The other three bind-mounted modules matched
already, because Phase 3 did not change them. This belongs in `OPERATIONS.md`
(T055).

## Decisions

### D1 — Transport: websocket, not webhook

- **Decision**: EventSub over websockets (`EventSubWebsocket`).
- **Rationale**: no public ingress, no TLS certificate, no challenge
  handshake. The library handles reconnect. The spike ran 394 subscriptions
  across 2 sockets with no broadcaster consent and `total_cost` 0.
- **Alternatives considered**: webhook transport has a higher ceiling and
  server-side persistence across restarts, but needs a public HTTPS endpoint
  and a challenge handshake. Kept in spec Out of Scope as the fallback.
- **Corrected 2026-08-29 — the websocket ceiling is 900 channels, and this
  decision had the number wrong.** The bullet above used to say the pool
  becomes unwieldy "far past 500 channels (about 7 sockets at 2000)". There is
  no 7-socket configuration: Twitch documents **"a maximum of 3 WebSockets
  connections with enabled subscriptions"** per client-id/user-id pair, at 300
  enabled subscriptions each
  (`dev.twitch.tv/docs/eventsub/handling-websocket-events`, checked
  2026-08-29). So websocket tops out at **3 × 300 = 900 channels**, and 2000
  is not reachable on this transport at all.
  Nothing measured in Phase 0 or Phase 5 contradicts this — the spike used 2
  sockets for 394 and Phase 5 used 2 for 500, both inside the limit — which is
  why it went unnoticed: every number this feature was verified at sits below
  the cap. The consequence is only for the ramp beyond 900. **Webhook is
  therefore not a "revisit if it gets unwieldy" option but the required
  transport past 900 channels**, and the pool now refuses to open a fourth
  socket rather than letting Twitch refuse each subscription with wording the
  classifier does not recognise.

### D2 — Reconciler concurrency: 10, with mandatory 429 backoff-and-retry

- **Decision**: bounded concurrency, default **10** (`RECONCILE_CONCURRENCY`,
  env-configurable). On a 429, back off (fixed ~10 s, or honour the
  `Ratelimit-Reset` header if the library exposes it) and **retry the failed
  channels** — never drop them. The backoff/retry loop is load-bearing, not a
  safety net.
- **Rationale (measured, T003 / T003b, 2026-08-28)**: the limit is a
  **per-token request budget**, not a concurrency ceiling. 250 creates drew
  **zero 429s at concurrency 1, 5, 10 and 20**. A larger burst hits the budget:
  the first 429 landed after **364 successful creates** (~14 s at concurrency
  15). Burst budget ≈ 360–420 creates. So concurrency can be anywhere in
  10–20 with no throttling risk *within* a burst; what matters past ~400
  channels is the retry loop. With concurrency 15 + 10 s backoff, a cold start
  reached **500 subscriptions in 40.6 s** and 550 in 53.2 s — inside the SC-001
  60 s target and well under the 120 s ceiling. Even a ramp starting on an
  already-drained budget converged (500 in 125 s, 796 429s, all retried).
- **Why keep 10 rather than raise it**: 10 already clears SC-001 with margin;
  15–20 shave ~10 s off cold start but buy nothing operationally and give the
  reconciler more in-flight work to unwind on a mid-ramp restart. 10 is the
  conservative default; the env var is there if a future channel count needs it.
- **Outcome at the shipped default (T041, 2026-08-29)**. Read the two cold-start
  numbers in this decision as the pair they are. **40.6 s is the T003b
  throwaway harness at concurrency 15.** **51.1 s (99% of 500) is the shipped
  `Reconciler` at the default concurrency 10**, and it is the number SC-001 is
  judged on. The 10.5 s between them is the "~10 s" the bullet above predicted
  for 15–20, now measured rather than argued, so the trade the default was
  chosen on holds: 10 lands 8.9 s inside the 60 s target and 69 s inside the
  120 s ceiling. Quote 51.1 s whenever the subject is what this feature ships;
  40.6 s only ever described the harness.
- **Alternatives considered**: unbounded fan-out (mirrors the `ClipCreator`
  thread bug this project is moving away from; and a burst >420 just 429s
  anyway); staying sequential (2.1/s — no better than IRC, 500 in ~240 s);
  a fixed requests-per-second limiter instead of concurrency + backoff (more
  code, and the measured burst-then-throttle shape is handled fine by
  concurrency + reactive backoff).

### D3 — `sent_at` source: envelope `metadata.message_timestamp` — GATE PASSED

- **Decision**: `sent_at` comes from the EventSub envelope's
  `metadata.message_timestamp`, converted to epoch milliseconds.
  `ChannelChatMessageData` has no timestamp field, so there is no other
  per-message option. **T001 gate met** (2026-08-28): median offset **+1 ms**,
  max 1 ms, 0 negative, over 24,473 messages joined by message UUID. Cutover
  proceeds.
- **What T001 actually showed**: `metadata.message_timestamp` and IRC
  `tmi-sent-ts` are **the same value** to the millisecond — not "dispatch time
  vs send time" as feared, but the one Twitch-assigned instant carried on both
  transports. There is no offset to correct and no calibration constant to
  maintain. `SentAtTimestampAssigner` sees the identical event-time input it
  sees today; the corpus-derived tuning stays valid.
- **Rationale**: `SentAtTimestampAssigner` drives Flink's event time from
  `sent_at`. A silent shift in that quantity would move every bucket boundary
  and invalidate the corpus-derived tuning without failing loudly.
- **Alternatives considered**: subtract a measured static offset (adds a
  calibration constant that drifts if Twitch changes dispatch behaviour);
  use the ingestion `timestamp` as event time (abandons Twitch's clock, which
  Plan 06 Phase 2 deliberately adopted); block the feature and pursue webhook
  (only if T001 shows a material, uncorrectable offset).
- **Clarified 2026-08-27**: this is the chosen rule (spec Clarifications).
- **Offset direction (pre-measurement reasoning, now moot)**: dispatch time was
  expected to be at or after send time, giving a one-signed offset. T001 showed
  the two timestamps are identical, so there is no offset in either direction.
  The "wide or unstable spread" failure signal did not appear: the spread is
  0–1 ms.

### D4 — Watermark tolerance: 2 s

- **Decision**: `WATERMARK_OUT_OF_ORDERNESS_SECONDS` moves from 1 to 2, in
  `services/flink-job/spike_detector.py`. `clip_detector_job.py` and
  `tools/replay.py` read the same constant, so all three move together.
- **Rationale (T002, 2026-08-28)**: EventSub delivery lag over 59,405 messages
  at 414 channels held p50 163 / p95 217 / p99 257 ms — flat against the
  394-channel spike (154 / 220). The tail did **not** keep growing: p99.99 was
  1,255 ms and exactly **one message (0.0017%) exceeded 2,000 ms** (a lone
  4.4 s outlier, most likely a GC pause in the measurement consumer). A 2 s
  tolerance therefore drops ~0.0017% of records — ~60× under the SC-005 0.1%
  budget. The cost is ~1 s added to the peak-to-clip-request delay floor.
- **Alternatives considered**: keep 1 s and accept a measured drop rate
  (the operator chose the watermark move instead — spec Clarifications); at 1 s
  the T002 data shows ~0.039% would drop — still under budget but with no
  margin for the untested 414→500 gap. Raise to 5 s (the pre-2026-08-27 value,
  a round number with no measurement behind it — over-corrects and slows every
  detection).
  **Superseded 2026-08-29 — read the Acceptance bullet below before this one.**
  The "1 s is under budget" reading came from T002's delivery-lag figure, which
  omits the record's offset within its own second and so understates lateness.
  Measured properly, 1 s is late on ~0.60% of records, six times over the
  SC-005 budget, in two independent windows. **1 s is not an available option**;
  keeping it would have failed SC-005. The bullet is left in place because it
  records what was believed when the decision was taken.
- **Acceptance — settled 2026-08-29, and not the way this line first said.**
  The planned check was Flink's `numLateRecordsDropped` at a stable 500
  channels. **That metric does not exist for this job**: Flink emits it from
  windowing operators, and `clip_detector_job.py` uses a `KeyedProcessFunction`
  with per-second timers and no window operator, so nothing ever drops a late
  record. The equivalent quantity — records arriving after their own bucket's
  timer fired — was measured directly on the live topic instead:
  **0.0030% past 2 s — 2 records in 66,154 across three windows** — against
  the 0.1% budget. The same measurement puts the *previous* 1 s value at
  0.20–0.62%, over budget in every window, so the move to 2 s was necessary
  for SC-005, not merely prudent. The channel count is 21–23, not 500; the 500-channel part of the claim is carried by T044's
  transport measurement and is argued, not measured. See Phase 4 T038 and the
  honesty note at the end of Phase 5.

### D5 — Refusal cache and `allows_clipping`: shared 7-day re-check

- **Decision**: add `eventsub_refused_at TIMESTAMPTZ` and
  `clipping_disabled_at TIMESTAMPTZ` to the `streamers` table. A channel with a
  non-null mark is skipped. A mark older than 7 days is retried once; success
  clears it, a fresh refusal resets the timestamp.
- **Rationale**: ~1.5% of channels refuse with `subscription missing proper
  authorization` (6 of 400). Retrying every cycle wastes POSTs; skipping
  forever leaves a channel dark after it fixes its settings. The existing
  `allows_clipping` skip has the same "forever" problem, so both get the same
  self-heal.
- **Alternatives considered**: in-memory set rebuilt each restart (not durable;
  a long-lived process never revisits); permanent skip like `allows_clipping`
  today (rejected by the operator — spec Clarifications); a shorter re-check
  interval such as daily (more self-healing, more wasted calls against
  permanently unauthorized channels).
- **Clarified 2026-08-27**: both flags, 7-day interval (spec Clarifications,
  FR-007 and FR-013).

### D6 — Connection pool routing: rendezvous hashing

- **Decision**: route a channel to a connection by a hash that keeps it on the
  same connection across reconciles. Track
  per-connection occupancy against the 300 cap. Grow the pool when the desired
  set needs more than `connections * 300` slots.
- **Rationale**: on socket death only that connection's ~300 subscriptions need
  recreating, not a full reshuffle. `get_eventsub_subscriptions().total` is
  unreliable (reported 300 while pages yielded 396) — count pages, do not trust
  `total`.
- **Alternatives considered**: fill-first packing (a socket death forces a
  rebalance across the whole pool); fixed 2-socket pool (breaks silently above
  600 channels).
- **Corrected during implementation (T018, 2026-08-28)**: this decision was
  first written as "consistent hash", and `data-model.md` wrote the rule as
  `hash(id) % len(connections)`. **Modulo is not consistent hashing, and it
  defeats the decision.** Growing the pool changes the divisor and moves nearly
  every channel, which is the reshuffle D6 exists to prevent. The
  implementation uses **rendezvous hashing** — score each connection against
  the broadcaster id, take the highest — so growth moves only the channels that
  score higher on the new connection. Connection ids come from a monotonic
  counter, so retiring one does not renumber the survivors. The digest is
  `blake2b` rather than the built-in `hash()`, because Python salts `hash()` of
  a string per process and would otherwise reshuffle every channel on restart.
- **Narrowed in the Phase 6 code review (2026-08-29)**: the placement is stable
  within a process, not across restarts. `route()` scores only the connections
  that are already OPEN, and the pool grows only when those are full, so a cold
  start fills connection 0 to the cap before connection 1 exists and arrival
  order therefore contributes to where a channel lands. Making placement
  restart-stable would mean routing against sockets that do not exist yet —
  opening three for ten channels, against FR-006's "start with no connections"
  and Twitch's habit of closing a session that has no subscription within ten
  seconds. It would also buy nothing: a websocket session dies with the
  process, so a restart re-creates every subscription wherever it lands. The
  two properties D6 is actually for — growth never moves a working channel, and
  a socket death costs only that socket's channels — hold as written.

## Risks

| ID | Risk | Mitigation | Phase 0 task |
|---|---|---|---|
| R1 | Events dropped during the subscribe ramp — `received event for unknown subscription` | **CLOSED (T004, 2026-08-28)**: 0 dropped events across a 500-channel cold ramp (12,555 events in the window), opening baseline not depressed (first-60 s / steady ratio 1.08). twitchAPI 4.5.0 registers the callback synchronously with the create POST. No warm-up gate — T040 skipped. Re-check only if the library is upgraded | T004 ✓ |
| R2 | D3 shifts event time silently and corrupts detection | **CLOSED (T001, 2026-08-28)**: the two timestamps are identical (median offset +1 ms, 0 negative, over 24,473 messages). No event-time shift. T006 gate passed | T001 ✓ |
| R3 | Concurrency triggers 429s not seen sequentially | **Measured (T003/T003b)**: 429s are budget-driven, not concurrency-driven — none at concurrency ≤20 for 250 creates; first 429 after ~364 creates in a larger burst. Mitigation is the D2 backoff-and-retry loop, which converged a 500-channel cold start in 40.6 s **in the T003b harness at concurrency 15** — the shipped `Reconciler` at the default concurrency 10 does it in 51.1 s (T041), and that is the figure SC-001 is judged on — and recovered even from a drained budget. Concurrency 10 default, configurable | T003 ✓ |
| R4 | A socket death drops up to 300 channels at once | Rendezvous-hash routing plus fast reconcile. Alert on a subscription-count drop (FR-012) | — |
| R5 | Removing IRC leaves no fallback if EventSub misbehaves in production | Deliberate. The operator accepted no intermediate compatibility. `git revert` of the branch is the fallback | — |

## Deployment and token notes

- **Token scope**: the production token carries `chat:read` and `clips:edit`
  only — checked on disk 2026-08-28, no `user:read:chat`. Phase 0 seeded a
  **separate** `secrets/phase0_tokens.json` (superset `chat:read` +
  `clips:edit` + `user:read:chat`) via `seed_twitch_tokens.py` device flow and
  never touched the production file; prod kept running on IRC throughout, token
  unaffected. Phase 2 T017 still needs to add `AuthScope.USER_READ_CHAT` to
  `REQUIRED_SCOPES` and re-seed the production token file.
- **New modules**: `reconciler.py` and `eventsub_pool.py` need a `COPY` line in
  `services/stream-monitoring/Dockerfile` and a bind-mount entry in
  `docker-compose.yml`. Neither is picked up automatically.
- **Lingering subscriptions**: after a socket closes, its subscriptions linger
  as `websocket_disconnected` and `DELETE` on them returns "not found". Twitch
  garbage-collects them. Restart reconciliation must tolerate stale entries and
  not treat a failed `DELETE` as an error.

## Out of scope (carried from spec, recorded so it is not re-litigated)

- `ClipCreator`'s unbounded thread spawn and the missing clip budget. Clip
  creation is capped per account, so a larger monitored set detects far more
  anomalies than can be acted on. That needs anomaly ranking against a scarce
  budget — a design change. Candidate for spec 005.
- The detector state redesign from spec 003, which measurement deflated.
- Kafka partition and Flink parallelism re-provisioning.

### Post-merge: the 2026-08-30 channel-count ramp

Production was ramped 15/30 → 50/100 → 150/300 → 300/500 → 480/500 with a
~25 min soak per step (`OPERATIONS.md` "Ramping the monitored channel count"
has the full record and the metrics table). Result: **the system stayed clean
through ~485 real channels.** Subscriptions tracked the desired set on every
sample, TaskManager RSS held flat at ~2.6 GB against the 6 GB cap, thread count
held at ~135, and clip creation never returned a rate limit.

**The clip ceiling this section predicts was not reached.** The ~2.2
detections-per-broadcaster-hour figure in §1 came from the top ~72 channels;
the measured rate across ~485 was ~0.3–0.5, because the rank 150–500 band is far
quieter. At ~485 channels that is ~150 clip attempts per hour, well under any
Twitch per-account limit. Spec 005 is still the right shape — it just does not
block a ramp to the transport's own 900-channel cap. Its trigger is a
`rate_limited` reason on `clips_created_failed_total`, which this ramp never
produced. Production was left at 150/300 (~290 channels); going higher is proven
safe and is the operator's call.

One rough edge worth carrying: every cold start throws a burst of
`transient_session` create failures (11–71, non-deterministic) as the first
websocket session reconnects under load. All recover on the next pass. A commit
on 2026-08-30 classified them apart from real failures and moved the logging
from ERROR to WARNING.

**A later attempt at 800/1000 (~800 channels) did not converge and was rolled
back.** The transport's 900-connection cap is not what binds first. The poller
does a remote Postgres upsert and a Kafka lifecycle publish per channel, so at
~800 channels one `poll_top_streams` ran past 100 s and APScheduler
(`max_instances=1`) missed the next poll by 104 s — the FR-003 "poll never
blocks" guarantee breaking under the per-channel remote write. Separately, 800
creates from cold is ~2x Twitch's ~400 burst budget, so the reconciler spends
200 s+ per pass grinding through 429 backoff. **The safe ceiling for the current
design is ~500 channels.** Raising it needs the poll's per-channel write batched
or moved out of the poll loop — a design change, candidate for its own spec
alongside 005. `OPERATIONS.md` "Ramping the monitored channel count" carries the
detail.

# Implementation Plan: Detector State Cost at Fan-Out Scale

**Branch**: `003-detector-scale-fanout` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/003-detector-scale-fanout/spec.md`

## Summary

`AnomalyDetector.on_timer` reads its whole bucket map from keyed state once per
broadcaster per second. Replace that per-entry map with a single stored value,
so the per-second cost becomes one read and one write. Keep the detection math
byte-identical.

## Technical Context

**Language/Version**: Python 3.10 (PyFlink job), Python 3.11 (services)
**Primary Dependencies**: PyFlink, `spike_detector` (pure), `tools/replay.py`
**Storage**: Flink keyed state (operator), Postgres (clips)
**Testing**: pytest — `test_spike_detector.py`, `test_replay.py`, `test_clip_detector.py`
**Target Platform**: Flink standalone cluster in Docker, 1 TaskManager, 2048 MB, 4 slots
**Project Type**: single (stream-processing job)
**Performance Goals**: sustain 2000 keyed broadcasters without watermark lag
**Constraints**: 2048 MB TaskManager; `FLINK_PARALLELISM=4`; 4 Kafka partitions
**Scale/Scope**: 30 broadcasters today, 2000 target

## Constitution Check

| Principle | Status |
|---|---|
| Kafka for all inter-service messaging | Unchanged |
| Postgres exclusively for persistence | Unchanged |
| PyFlink for stream processing | Unchanged |
| Prometheus metrics | Unchanged; add state-cost metric |
| No data loss in the pipeline | Must hold — see Risk R3 |
| Filter bot commands | Unchanged (`CommandFilter` untouched) |
| Python virtual environments | Use `services/flink-job/.venv` |

No violation. This feature changes an operator's internal state representation
only.

## ⚠️ T003 resolved — this plan is now the FALLBACK, not the first move

The gate has been run (see `research.md`, "T003 RESOLVED"). **The premise was
wrong.** `MapState.items()` is a cached, batched read, not ~305 round trips.

The real problem is an **LRU capacity cliff**: PyFlink's state read cache
defaults to `python.state.cache-size = 1000`. At 30 broadcasters every key stays
cached and `items()` is nearly free. At 2000 keys the LRU thrashes, and every
call becomes a batched fetch plus ~305 entry decodes.

**Because the cliff is a config value, the first move is a config change, not
this redesign.**

### Option 1 — raise the cache ceiling (DO THIS FIRST)

Set, in `flink-conf.yaml`:

- `python.state.cache-size` — above the target broadcaster count
- `python.map-state.read-cache-size` — sized to hold 305-entry maps
- `python.map-state.write-cache-size` — to match the write path

Zero code change. Zero detection risk. FR-002 is satisfied trivially, because
nothing about the detector changes. The cost is Python-process memory:
2000 x 305 = ~610,000 cached entries.

**Open question — the memory budget.** `taskmanager.memory.process.size` is
2048 MB, on a machine that also runs Kafka, Postgres, Redis, Flink, and the
Grafana/Loki/Prometheus stack. Whether Option 1 fits, or whether the
TaskManager can grow, is a resource decision for the operator.

### Option 2 — the ring buffer (build only if Option 1 cannot fit)

The design below. It reduces both cached object count per key and per-entry
decode cost. It is strictly more work and strictly more risk than Option 1, and
it should not be built speculatively.

**Revised gate**: measure Option 1 under load first. Proceed to Option 2 only
if memory makes Option 1 impossible.

## Design (provisional, pending Phase 0)

### Current state layout

```
message_counts   MapState<long, int>    ~305 entries, one per retained second
hold             ValueState<string>     HoldState as JSON
last_fire_second ValueState<long>
```

### Proposed state layout

```
window           ValueState<string>     the whole retained window, encoded
hold             ValueState<string>     unchanged
last_fire_second ValueState<long>       unchanged
```

`window` holds a fixed-length ring buffer of `retained_seconds` counts, plus
the event-time second of the newest slot. Encoding must be cheap — a compact
string or array, **not** JSON of a dict, because JSON encode/decode of 305
entries per broadcaster-second at 2000 keys is itself a real cost.

### Why the math does not change

`evaluate()` keeps its exact signature, its two-pass statistics, and its whole
test suite. The adapter rebuilds an ordinary Python dict from the ring buffer
and passes it in, exactly as today:

```python
all_counts = self._window_to_dict()      # local memory, microseconds
decision  = evaluate(counts_as_of_now, now_seconds, hold, last_fire, config)
```

Building a 305-entry dict in local memory costs microseconds. The 305 state
reads it replaces do not. **That asymmetry is the entire feature.**

This also satisfies FR-004 for free: the precision decision documented in
`_mean_and_sample_stdev` stays untouched, because that function is untouched.

### Consequences that must be handled

**C1 — Late writes.** `process_element` does `message_counts.put(bucket, n+1)`
for any bucket, including one already inside the baseline. The ring buffer must
accept the same update. This means `process_element` now does a read-modify-write
of `window` instead of a single map `put`. That is a cost *increase* on the
hot path — one state access per message rather than one map put. Phase 0 must
measure whether this trade is favourable at the observed message rate
(~1200-1800 msg/s projected across 2000 keys; see `research.md`).

**C2 — Buckets outside the ring.** A message can arrive for a bucket older than
the ring covers, or newer than the newest slot. Today `MapState` accepts both
and `evaluate()` filters them. The ring buffer must define this explicitly:
advance the ring for a newer bucket (zeroing skipped slots), and **drop** a
bucket older than `retained_seconds`, which `evaluate()` would have evicted
anyway.

**This touches a constitution MUST — "No data loss in the event pipeline."**
Dropping a bucket is only safe if the current code would have discarded the
same data. That must be *proven* case by case, not asserted. Task **T015a**
carries this as a merge gate.

**C3 — TTL.** `_state_ttl()` currently relies on `MapState` per-entry TTL with
`OnCreateAndWrite`, so each bucket's clock starts at its own write. A single
`ValueState` has **one** TTL clock for the whole window, refreshed on every
write. This is a real semantic change. It is probably benign — an active key
keeps writing, and an idle key stops and expires — but the reasoning in
`_state_ttl()` about not deleting a bucket the baseline still needs must be
re-derived for the new layout, not carried over.

**C4 — Eviction and `expired_buckets`.** `evaluate()` returns
`expired_buckets`, and the adapter removes each from `MapState`. With a ring
buffer, eviction is implicit: advancing the ring overwrites old slots. The
adapter must still consume `expired_buckets` so `evaluate()` is unchanged, but
the removal becomes a no-op or a slot-zeroing. Sorted order (FR-005) is
preserved because the ring is ordered by construction.

**C5 — Timer chaining.** `on_timer` re-arms only `if all_counts:`. The ring
buffer is fixed-length and always "present", so emptiness must be defined as
"all slots zero", not "the structure exists". Getting this wrong makes an idle
key re-arm a timer forever — a per-key leak at 2000 keys.

**C6 — `replay.py` parity.** FR-008 requires the harness to stay a faithful
mirror. If the operator's storage changes but `replay.py` keeps a dict, replay
still proves the *math* is equal but no longer proves the *storage* is
equivalent. Decide explicitly: either mirror the ring buffer in `replay.py`, or
document that replay covers math only and cover storage with unit tests.

## Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Phase 0 disproves the premise; work targets nothing | Phase 0 is a hard gate before any code change |
| R2 | C1 makes the hot path worse than it fixes | Measure both paths in Phase 0, not just the timer |
| R3 | Ring-buffer edge case silently drops messages (constitution: no data loss) | Corpus replay must be byte-identical on both files |
| R4 | TTL change (C3) leaks state at 2000 keys | Explicit idle-key expiry test |
| R5 | Encoding cost replaces state cost | Benchmark the encoding; reject JSON-of-dict |

## Project Structure

### Documentation (this feature)

```
specs/003-detector-scale-fanout/
├── spec.md
├── research.md
├── plan.md
└── tasks.md
```

### Source Code

```
services/flink-job/
├── clip_detector_job.py       # AnomalyDetector — the only operator changed
├── spike_detector.py          # evaluate() — signature and math UNCHANGED
├── tools/replay.py            # mirror; see C6
├── tools/measure_corpus.py    # existing measurement tooling
├── test_spike_detector.py     # must pass unmodified
└── test_replay.py             # must pass unmodified
```

**Structure Decision**: Single project. The change is contained to
`AnomalyDetector`'s state handling in `clip_detector_job.py`, plus whatever C6
decides for `replay.py`. `spike_detector.py` is deliberately untouched, which is
what makes the equivalence argument tractable.

## Complexity Tracking

The one genuine complexity is C1: moving from a map `put` per message to a
read-modify-write per message. It trades hot-path cost for timer-path saving.
That trade is only justified if Phase 0 shows the timer path dominates. If the
message rate is high and the per-key second rate is low, the trade could be
neutral or negative.

An alternative, if C1 proves costly: keep `MapState` for writes but maintain a
**separate** running-aggregate `ValueState` updated on the same write, so the
timer path never scans the map. This costs more state writes per message but
removes the scan entirely. Hold this in reserve; do not build it speculatively.

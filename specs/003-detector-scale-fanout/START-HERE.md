# START HERE — implementation handoff

You are picking up feature 003. **You do not need to read the conversation that
produced it.** Everything needed is in this directory. Sonnet-class is fine for
this work; the hard design calls are already made and written down.

## The one-paragraph version

StreamScout watches ~30 Twitch broadcasters and we want 2000. The detector
(`AnomalyDetector` in `services/flink-job/clip_detector_job.py`) was assumed to
be the bottleneck. **Investigation showed it probably is not.** The likelier
constraint is TaskManager heap, which is a config change (PR #40). The job now
is to *measure* rather than to rewrite. A ring-buffer rewrite of the detector is
specified as a fallback, and you should build it only if measurement demands it.

## Do these in order

1. **T005a1 — DONE.** TaskManager raised 2048m to 6144m. PR #40.
2. **T005b1 — the real work, and it does not exist yet.** Build a load rig: a
   synthetic producer writing chat messages for N distinct `broadcaster_id`s
   into the `chat-messages` Kafka topic at a configurable rate.
3. **T005c** — run it at 2000 keys. Watch: watermark lag, JVM heap, Python
   worker memory, PyFlink state cache hit rate.
4. **T005d** — decide. If the config change alone holds, close Phases 1b and 2
   as unnecessary and go to Phase 3. If not, `plan.md` has the fallback design.

## Traps that will cost you a day if you miss them

- **`tools/replay.py` cannot be the load rig.** It is a pure-Python mirror of
  the detector. It proves the *math* is equivalent. It never touches Flink
  state, the PyFlink Java boundary, or the LRU read cache, so it cannot measure
  any of the things this feature cares about. You need real messages through
  real Kafka into the real job.

- **`spike_detector.py` must not change.** The whole equivalence argument rests
  on it being untouched. If your change seems to require editing it, something
  upstream is wrong — stop and re-read `plan.md`.

- **Do not "optimise" `_mean_and_sample_stdev` into a sum-of-squares form.** It
  is deliberately two-pass, and its docstring says why. `research.md` records
  the rejected proposal in full so nobody re-proposes it.

- **`taskmanager.memory.process.size` lives in two files** —
  `services/flink-job/flink-conf.yaml` and the `FLINK_PROPERTIES` block in
  `docker-compose.yml`. Edit both or they drift silently.

- **`chat-messages` is pinned at 4 partitions** to match `FLINK_PARALLELISM=4`.
  Kafka cannot shrink a partition count in place. If your load rig wants more
  parallelism, the topic must be deleted and recreated.

- **Checkpointing is off** (`flink-conf.yaml`, deliberate). State does not
  survive a restart; the job rebuilds from Kafka. Do not write tests that
  assume state restoration.

## What "done" looks like

`spec.md` Success Criteria, all measurable:

- SC-001 / 001a / **001b** — the **total** measured cost must fall. A win on
  the timer path with a worse total fails the feature.
- SC-002 — corpus replay produces byte-identical anomalies before and after.
  Both files in `~/stream-scout-corpus/`.
- SC-003 — `test_spike_detector.py` and `test_replay.py` pass with **no
  assertion edited**.
- SC-004 / SC-005 — 2000 keys, 10 minutes, watermark holds, heap in budget.

## Read in this order

1. `spec.md` — what and why, and the Out of Scope list, which matters
2. `research.md` — the measurements, **and three refuted hypotheses.** Read the
   refutations; they exist to stop the work being re-proposed
3. `plan.md` — the fallback design and its six open consequences (C1-C6)
4. `tasks.md` — the checklist. Tick items as you go; it is the durable record

## Environment

- Job venv: `services/flink-job/.venv` (Python 3.10, PyFlink 1.18)
- Constitution requires a virtualenv for any package install
- Corpus: `~/stream-scout-corpus/chat-corpus.jsonl` (635 MB) and
  `chat-corpus-2026-08-17.jsonl` (542 MB)
- Stack: `docker compose up -d` from the repo root

## Not in this feature

Real, deliberately excluded, each large enough to sink 003. Candidates for 004:

- `ClipCreator` spawns an unbounded `threading.Thread` per anomaly against a
  per-account clip limit. Needs anomaly *ranking* against a scarce budget — a
  design change, not a limiter.
- EventSub delivery lag measured at max 1243 ms, which breaches the 1 s
  `WATERMARK_OUT_OF_ORDERNESS` set in commit `4ce10e0`.
- Migrating ingestion from IRC to EventSub. `research.md` §1 has the full spike
  data and is ready to carry forward.

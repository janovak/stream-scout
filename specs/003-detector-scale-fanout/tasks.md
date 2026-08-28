# Tasks: Detector State Cost at Fan-Out Scale

**Input**: Design documents from `/specs/003-detector-scale-fanout/`
**Prerequisites**: spec.md, research.md, plan.md

**Tests**: Test tasks ARE required here. FR-002 makes behavioural equivalence
the core contract, so verification is the feature, not an optional extra.

**Format**: `[ID] [P?] [Story] Description` — `[P]` means parallelisable.

## Path Conventions

Job code lives in `services/flink-job/`. Use `services/flink-job/.venv`
(constitution: always use a virtual environment).

---

## Phase 0: Measurement Gate ⚠️ BLOCKING

**Purpose**: Prove or disprove the premise before changing any code.

**⚠️ CRITICAL**: `plan.md` marks this a hard gate. If T003 contradicts the
premise, STOP, record the finding in `research.md`, and re-plan. Do not
proceed into Phase 2 on assumption.

- [ ] T001 Bring up `services/flink-job/.venv` and confirm `test_spike_detector.py`, `test_replay.py`, `test_clip_detector.py` all pass on `main` as the baseline
- [ ] T002 Capture a baseline anomaly stream: replay both `~/stream-scout-corpus/chat-corpus.jsonl` and `chat-corpus-2026-08-17.jsonl` through `tools/replay.py` on unmodified code; store outputs as the reference for T020
- [x] T003 **[GATE]** ~~Determine whether PyFlink's `MapState.items()` is one bulk read or ~305 per-entry reads.~~ **DONE 2026-08-27 — premise REFUTED.** It is a cached, batched read. The real issue is an LRU capacity cliff at `python.state.cache-size` (default 1000). See `research.md` "T003 RESOLVED"
- [x] T005a **[GATE, conditional]** ~~If refuted, re-plan.~~ **DONE** — `plan.md` now leads with Option 1 (config), demoting the ring buffer to a fallback. Refuted hypothesis kept in `research.md`, not deleted
- [ ] T004 Measure per-broadcaster-second `on_timer` cost and per-message `process_element` cost at current scale, as the baseline for judging Option 1
- [ ] T004a **[Option 1]** Measure Python-process memory at 2000 keys with raised cache sizes. Decides whether Option 1 fits the 2048 MB TaskManager, and therefore whether Option 2 is needed at all
- [ ] T005 Record T004/T004a results in `research.md`

**Checkpoint**: Option 1 measured. Option 2 built only if Option 1 cannot fit.

---

## Phase 1a: Option 1 — Config-only fix (TRY THIS FIRST)

**Purpose**: Remove the LRU cliff without touching code. If this holds at 2000
keys, Phase 1b and Phase 2 are unnecessary.

- [ ] T005a1 **[DO FIRST]** Raise `taskmanager.memory.process.size` from 2048m to 4-6 GB in `flink-conf.yaml` and `docker-compose.yml`. The TaskManager is at ~73% of its cap at only 30 broadcasters, and the box has 5.2 GiB free. This is the likeliest real constraint, and it costs nothing but config
- [ ] T005b Only if measurement shows the LRU matters: add `python.state.cache-size`, `python.map-state.read-cache-size`, `python.map-state.write-cache-size` to `flink-conf.yaml`. Note per `research.md` that at parallelism 4, 2000 broadcasters is ~500 keys per subtask, which already fits the default 1000
- [ ] T005b1 **[BLOCKER — does not exist yet]** Build the 2000-key load rig: a synthetic producer that writes chat messages for N distinct `broadcaster_id`s into the `chat-messages` Kafka topic at a configurable rate. NOTE: `tools/replay.py` cannot do this — it is a pure-Python mirror that never exercises Flink state, the PyFlink boundary, or the LRU cache. It proves math equivalence only. Every runtime measurement in this feature (T005c, T022, T022a, T023) depends on this rig
- [ ] T005c Run the 2000-key load test using the T005b1 rig. Confirm the watermark holds and JVM heap stays inside the raised budget. Record cache hit rate to confirm or refute the LRU concern
- [ ] T005d **[DECISION]** If T005a1 alone holds: close Phase 1b and Phase 2 as not needed, and go to Phase 3 verification. If not, record why, then continue

**Checkpoint**: Either the feature is done with a config change, or Option 2 is
justified with evidence.

---

## Phase 1b: Design Decisions (only if Option 1 failed)

**Purpose**: Close the six open consequences in `plan.md` before writing code.

- [ ] T006 Decide the `window` encoding; benchmark candidates and reject JSON-of-dict per Risk R5
- [ ] T007 [P] Resolve C2 — define behaviour for buckets older or newer than the ring, and prove equivalence to today's `MapState` + `evaluate()` filtering
- [ ] T008 [P] Resolve C3 — re-derive the TTL argument for a single-clock `ValueState`; confirm no bucket the baseline needs can expire early
- [ ] T009 [P] Resolve C4 — define how `expired_buckets` is consumed when eviction is implicit, preserving sorted order (FR-005)
- [ ] T010 [P] Resolve C5 — define "empty" for a fixed-length ring so idle keys stop re-arming timers
- [ ] T011 Resolve C6 — decide whether `replay.py` mirrors the ring buffer or covers math only; if math only, state what covers storage
- [x] T012 ~~Rubber-duck the design before implementing.~~ **DONE 2026-08-27.** The duck pass resolved T003 by reading PyFlink's `state_impl.py`, refuted the original premise, and produced Option 1. C1-C6 below apply only to Option 2, which is now a fallback
- [ ] T012a If Phase 1b is reached, re-duck C1-C6 against the *measured* Option 1 failure, not the original assumption

**Checkpoint**: Design agreed. Implementation can start.

---

## Phase 2: Implementation — Reduce state cost, preserve behaviour (US1, US2, US3)

**Goal**: Timer path down to 1-2 state accesses per broadcaster-second, total
cost measurably lower, identical output.

**Independent Test**: Corpus replay diff is empty; measured total cost drops.

### Tests first

- [ ] T013 [US2] Write a characterisation test that pins current `on_timer` behaviour across the Edge Cases in `spec.md` (late bucket, future bucket, watermark jump, key restart, eviction tie). Must explicitly cover all four FR-006 behaviours by name: peak-hold, cooldown, warm-up gate, hold-regression guard
- [ ] T014 [P] [US3] Write an idle-key expiry test covering Risk R4
- [ ] T015 [P] [US2] Write a unit test for the ring buffer in isolation — advance, wrap, late write, out-of-range write. Must assert FR-003 explicitly: an absent bucket reads as a count of zero, never as missing
- [ ] T015a [P] [US2] **[FR-002 / constitution gate]** Prove C2 loses no message the current code would have counted. Enumerate every bucket-age case, and assert equivalence to today's `MapState` + `evaluate()` filtering. The constitution forbids data loss in the pipeline, so this is a gate, not a unit test
- [ ] T015b [P] [US2] Assert FR-004 mechanically: `spike_detector.py` is byte-identical to `main` at the end of Phase 2. Any diff invalidates the equivalence argument

### Implementation

- [ ] T016 [US1] **[FR-001]** Add the `window` ValueState and the ring-buffer helpers to `AnomalyDetector` in `services/flink-job/clip_detector_job.py`
- [ ] T017 [US1] Rewrite `process_element` to update the ring instead of `message_counts.put` (see C1)
- [ ] T018 [US1] Rewrite `on_timer` to rebuild the counts dict from the ring; leave the `evaluate()` call, the hold handling, the cooldown, and the emit path untouched
- [ ] T019 [US1] Remove the `message_counts` MapState and its descriptor once nothing reads it

**⚠️ `spike_detector.py` must not change in this phase.** If it seems to need a
change, that is a signal the equivalence argument broke — stop and re-check.

---

## Phase 3: Verification

**Purpose**: Prove FR-002. This is the load-bearing evidence for the feature.

- [ ] T020 **Corpus replay diff** — replay both corpus files through the new code; diff against the T002 reference. Anomalies must be identical in broadcaster, second, count, mean, std, and intensity (SC-002)
- [ ] T021 Run `test_spike_detector.py` and `test_replay.py` unmodified; every test must pass with no assertion edited (SC-003)
- [ ] T022 Confirm **timer-path** state accesses per broadcaster-second are at most 2 (SC-001), using the T003/T004 method
- [ ] T022a Confirm **message-path** accesses are at most one per message (SC-001a), and that measured **total** cost is below the current total at the message rates in `research.md` (SC-001b). A timer-path win with a worse total fails this feature
- [ ] T023 Run a 2000-synthetic-key load test for 10 minutes; confirm the watermark does not fall behind (SC-004) and heap stays inside 2048 MB (SC-005)
- [ ] T024 Confirm idle keys expire and per-key state stays bounded (FR-007)

**Checkpoint**: All success criteria in `spec.md` are met with evidence.

---

## Phase 4: Review

**Purpose**: The review process agreed with the user.

- [ ] T025 Run `/speckit.analyze` — cross-artifact consistency across spec.md, plan.md, tasks.md. Read-only; produces a findings report
- [ ] T026 Address any CRITICAL or HIGH findings from T025 before merging
- [ ] T027 Run `/speckit.checklist` for a requirements-quality pass, as `002` did
- [ ] T028 Run `/code-review high` on the diff — targets what replay cannot see: TTL interaction, state migration on restart, error paths
- [ ] T029 Address code-review findings
- [ ] T030 Update `KNOWN_ISSUES.md` if any issue is resolved or created (note: it is currently untracked in git)

---

## Phase 5: Handoff

- [ ] T031 Update `research.md` with final measured numbers, replacing projections
- [ ] T032 Record in `spec.md` Out of Scope which follow-on features are now unblocked (EventSub migration, clip budget and ranking, Kafka re-provisioning)
- [ ] T033 Open the next feature for the EventSub transport migration, carrying `research.md` §1 forward

---

## Dependencies

- **Phase 0 blocks everything.** T003 is a gate, not a checkbox. T005a is the
  exit if the gate fails.
- T012 (duck) blocks Phase 2.
- T013-T015b (tests and gates) come before T016-T019 (implementation).
- T015a is a constitution gate — it blocks merge, not just implementation.
- T020 depends on T002 having captured the reference.
- T022a depends on T004 having established the baseline total.
- Phase 4 depends on a complete Phase 3.

## Parallel Opportunities

- T007-T010 are independent design questions — resolvable together.
- T014, T015, T015a, T015b are independent and can run together.
- T025 and T028 are different review tools and can run in either order.

## Notes on scope discipline

`spec.md` Out of Scope lists the EventSub migration, the `ClipCreator` thread
and clip-budget problem, the Kafka partition coupling, and the delivery-lag
tail that breaches the 1-second watermark. All are real. None belong in this
feature. Resist pulling them in — each is large enough to sink this one.

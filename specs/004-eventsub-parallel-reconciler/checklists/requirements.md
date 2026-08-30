# Requirements Quality Checklist: EventSub Ingestion with a Parallel Reconciler

**Purpose**: Unit-test the requirements in `spec.md` for completeness, clarity,
consistency, measurability, and coverage before implementation begins.
**Created**: 2026-08-27
**Feature**: [spec.md](../spec.md)
**Focus areas**: data integrity / event-time semantics; Phase 0 measurement
gating; migration and recovery; reconciler convergence.
**Depth**: release gate (this change touches a live ingestion path and event
time). **Audience**: implementer and PR reviewer.

**Status**: run 2026-08-27, **re-run 2026-08-29 against the final spec**
(Phase 6, T050a). Resolved items are checked with the edit that closed them.
The four items the first pass left open — CHK003, CHK013, CHK015, CHK030 — are
**all closed now**: three by the implementation answering the question the spec
had left black-box, one by the measurement the gate was waiting on. **35 of 35
closed.** One item, CHK017, was re-opened and re-closed on new evidence. See
Notes.

## Requirement Completeness

- [x] CHK001 Is the reconcile cadence or trigger specified — timer interval, poll-driven, or event-driven? [Gap, Spec §FR-002/§Overview] → `plan.md` Technical Context "Reconcile cadence"
- [x] CHK002 Are the reconciler's startup ordering requirements relative to the poller defined? [Gap] → FR-005 "any starting state" + `plan.md` process model; the reconciler tolerates an empty desired set at boot
- [x] CHK003 Is the initial connection-pool size and the growth trigger stated as a requirement, not only in `data-model.md`? [Gap, Spec §FR-006] → **closed 2026-08-29**: FR-006 now states it — the pool starts empty, grows on demand when every open connection is at the cap, and growth must not move a channel already placed. That last clause is the requirement rendezvous hashing exists to meet, and leaving it in `data-model.md` was how the doc came to describe modulo routing instead (see the D6 correction)
- [x] CHK004 Is behaviour specified when the desired set exceeds total pool capacity before a new connection is ready? [Gap, Spec §FR-006] → Edge Cases
- [x] CHK005 Is an upper bound on the chat-coverage gap during cutover stated, or "any gap is acceptable" made explicit? [Gap] → Assumptions & Dependencies
- [x] CHK006 Are the Prometheus metric names and units specified? [Clarity, Spec §FR-012] → names pinned in SC-007 and tasks T015; units are impl detail by decision
- [x] CHK007 Is the concurrency limit's default value and tuning procedure captured? [Clarity, Spec §FR-004] → `research.md` D2, tasks T003/T012
- [x] CHK008 Is the `emotes` field's required content defined? [Coverage, Spec §FR-008] → `contracts/chat-messages.schema.md` (kept empty)
- [x] CHK009 Are requirements defined for how the operator judges EventSub healthy enough for Phase 3 IRC removal? [Gap] → tasks T028a gate
- [x] CHK010 Is the `user:read:chat` scope change stated in the spec, not only `plan.md`? [Dependency, Spec Gap] → Assumptions & Dependencies

## Requirement Clarity

- [x] CHK011 Is "large enough to distort detection" (FR-014) quantified? [Ambiguity, Spec §FR-014] → ~1% of first-window messages, or a baseline-mean shift beyond detector noise
- [x] CHK012 Is "settled" / "warm" defined for the warm-up gate? [Ambiguity, Spec §FR-014] → "`enabled` and one message received"
- [x] CHK013 Is the T001 gate precise about which statistic is compared and how offset direction is handled? [Clarity, Spec §FR-009] → **closed 2026-08-29 by measurement**: FR-009 named the median, the gate ran, and T001 found the two timestamps identical (median +1 ms, max 1 ms, 0 negative, 24,473 joined messages). There is no offset, so there is no direction to handle. `research.md` D3 keeps the pre-measurement reasoning marked moot
- [x] CHK014 Is "converge from any starting state" bounded? [Clarity, Spec §FR-005] → "diff in one cycle; cold start inside the SC-001 bound"
- [x] CHK015 Is "the schema matches what the Flink job consumes today" defined precisely? [Clarity, Spec §US3 AS1] → **closed 2026-08-29**: US3 AS1 gives the spec-level rule (same keys, same value types, `sent_at` int-or-null) and points at `contracts/chat-messages.schema.md`, which holds the field table. The contract is the definition now rather than a second opinion: its invariant 4 first named an EventSub-vs-IRC equivalence test, and that test was correctly removed with IRC in Phase 3 (T034) once its reference implementation was gone. `TestEventSubMessageMapping` asserts against the contract instead, and T046 confirmed it on 21,502 live records — `sent_at` an epoch-ms int in every one
- [x] CHK016 Is "no unexplained drift" (SC-004) given an objective definition? [Measurability, Spec §SC-004] → "±1%, every delta attributable"
- [x] CHK017 Is the late-event drop measurement method named? [Measurability, Spec §SC-005] → named on 2026-08-27 as Flink `numLateRecordsDropped`; **that answer was wrong and was corrected 2026-08-29**. The metric does not exist for this job — Flink emits it from window operators and `clip_detector_job.py` is a `KeyedProcessFunction` with per-second timers, so no record is ever dropped. SC-005 now names the quantity that was actually measured: records arriving after the timer for their own bucket has fired, taken per partition on the live topic (`research.md` T038)

## Requirement Consistency

- [x] CHK018 Do FR-007 and FR-013 agree on who clears the mark and on what event? [Consistency] → `data-model.md` "Skip and re-check rule" is the single source; both FRs point at the 7-day rule
- [x] CHK019 Is the 2 s watermark tolerance used consistently between FR-009 and FR-010? [Consistency] → yes
- [x] CHK020 Is "500 channels" used consistently? [Consistency] → near-term target in the Overview. **Answer refined 2026-08-29**: the first pass wrote "every SC measured at 500", which the verification did not bear out. SC-001, SC-002, SC-004 and SC-006 were measured at 500; SC-003 and SC-007 are production checks where channel count is not the variable; **SC-005 is at 21-23 broadcasters and says so**. The Assumptions section now states which of the three is which
- [x] CHK021 Do the spec Edge Cases and the `research.md` risk list describe the same failure set? [Consistency] → the 429 case (R3) was missing from Edge Cases; added
- [x] CHK022 Does FR-001 stay consistent with every other requirement (no implied fallback transport)? [Consistency] → yes; `git revert` is the only fallback, stated in Assumptions

## Acceptance Criteria Quality

- [x] CHK023 Does every functional requirement have a matching Success Criterion or acceptance scenario? [Acceptance Criteria, Gap] → FR-011 via tasks T011; FR-012 via new SC-007; FR-013 via new US4 scenario 3
- [x] CHK024 Is there an acceptance criterion for FR-002 distinct from SC-002? [Coverage] → SC-002 (poll duration flat) is the accepted observable proxy
- [x] CHK025 Is FR-014's conditional phrased so it can be judged done or not-applicable? [Ambiguity] → yes, once T004 runs; tasks T040 records the decision

## Scenario & Edge Case Coverage

- [x] CHK026 Are recovery requirements defined for Redis or Postgres unavailable during a reconcile? [Gap, Recovery] → NFR-002, tasks T014c
- [x] CHK027 Is behaviour specified when `metadata.message_timestamp` is missing or malformed? [Gap, Edge Case] → Edge Cases + contract (`sent_at` null, assigner falls back). **Implemented 2026-08-29 in the Phase 6 code review**: it was specified in both places but not in the code — `to_epoch_ms` raised on an unreadable value and `map_chat_message` read the attribute directly, and an envelope without the field has no such attribute at all, so the exception reached `_on_eventsub_message` and the whole chat message was dropped. Both now yield `sent_at` null and publish (`TestEventSubMessageMapping`)
- [x] CHK028 Is behaviour defined when subscription enumeration fails mid-pagination at start-up? [Gap, Edge Case] → NFR-003, tasks T014b
- [x] CHK029 Is subscription-churn behaviour defined for a channel oscillating across the JOIN/LEAVE band? [Coverage, Spec §FR-011] → the hysteresis band is the mitigation; FR-011 preserved
- [x] CHK030 Is behaviour defined when a channel is in the desired set but its `broadcaster_id` cannot be resolved? [Gap, Edge Case] → **closed 2026-08-29 in code, as the first pass intended**: the reconciler no longer skips silently. It logs a warning with the count and a five-login sample, then skips those logins rather than guessing (`reconciler.py`, `reconcile_once` — the `missing_ids` branch). The case should not arise at all — Phase 1 added `chat:desired:ids` and the poller writes it in the same MULTI/EXEC as `chat:desired`, so the set and the map cannot disagree — which is why this stays a loud log and not a requirement
- [x] CHK031 Are the reconciler's own resource-use limits at 500+ channels stated? [Gap, Non-Functional] → NFR-001, tasks T014d
- [x] CHK032 Are rollback requirements defined for a half-applied `streamers` migration? [Gap, Recovery] → `data-model.md`: one `BEGIN…COMMIT` transaction

## Dependencies & Assumptions

- [x] CHK033 Is the "re-seeding the token does not invalidate the running token" assumption in the spec? [Assumption] → Assumptions & Dependencies
- [x] CHK034 Is the "300-per-connection and 0-cost hold beyond 394" assumption flagged? [Assumption, Spec §SC-004] → Assumptions & Dependencies; T002 re-checks
- [x] CHK035 Is the PR #41 dependency stated in `spec.md`? [Dependency, Spec Gap] → Assumptions & Dependencies

## Notes

- **Resolved in the first pass (2026-08-27)**: 31 of 35 items, via edits to
  `spec.md` (Assumptions section, NFR-001..003, SC-004/005/007, FR-005/014, US4
  scenario, 4 new edge cases), `plan.md` (cadence, process model), `tasks.md`
  (T014b–d, T028a, T038, T044a), `data-model.md` (transactional migration), and
  `research.md` (offset-direction reasoning).
- **Closed in the Phase 6 re-run (2026-08-29)**: the remaining four. CHK013 was
  answered by the T001 measurement the gate was waiting on. CHK030 was answered
  in code, which is where the first pass said it belonged. CHK015 was answered
  by the contract file becoming the single definition once IRC left. **CHK003
  was the one that should not have been deferred**: leaving pool growth out of
  FR-006 is how `data-model.md` came to specify modulo routing, which would
  have reshuffled the whole pool on growth and defeated D6. The implementation
  got it right independently; the requirement now says so.
- **One item re-opened**: CHK017's original answer named a Flink metric that
  does not exist for this job. A checked box is not evidence, and this one held
  a wrong answer for two days until T038 went looking for the metric. Both the
  item and SC-005 now name the quantity that was measured.
- This checklist tests the requirements text, not the implementation. It is not
  a QA plan — that is Phase 5 in `tasks.md`.

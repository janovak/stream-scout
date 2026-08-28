# Requirements Quality Checklist: EventSub Ingestion with a Parallel Reconciler

**Purpose**: Unit-test the requirements in `spec.md` for completeness, clarity,
consistency, measurability, and coverage before implementation begins.
**Created**: 2026-08-27
**Feature**: [spec.md](../spec.md)
**Focus areas**: data integrity / event-time semantics; Phase 0 measurement
gating; migration and recovery; reconciler convergence.
**Depth**: release gate (this change touches a live ingestion path and event
time). **Audience**: implementer and PR reviewer.

**Status**: run once on 2026-08-27; resolved items are checked with the edit
that closed them. Four minor items stay open by decision (CHK003, CHK013,
CHK015, CHK030 — see Notes).

## Requirement Completeness

- [x] CHK001 Is the reconcile cadence or trigger specified — timer interval, poll-driven, or event-driven? [Gap, Spec §FR-002/§Overview] → `plan.md` Technical Context "Reconcile cadence"
- [x] CHK002 Are the reconciler's startup ordering requirements relative to the poller defined? [Gap] → FR-005 "any starting state" + `plan.md` process model; the reconciler tolerates an empty desired set at boot
- [ ] CHK003 Is the initial connection-pool size and the growth trigger stated as a requirement, not only in `data-model.md`? [Gap, Spec §FR-006] → left in `data-model.md`; FR-006 + the new "desired set larger than the pool" edge case cover the behaviour
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
- [ ] CHK013 Is the T001 gate precise about which statistic is compared and how offset direction is handled? [Clarity, Spec §FR-009] → FR-009 names the median; direction reasoning added to `research.md` D3, not the spec
- [x] CHK014 Is "converge from any starting state" bounded? [Clarity, Spec §FR-005] → "diff in one cycle; cold start inside the SC-001 bound"
- [ ] CHK015 Is "the schema matches what the Flink job consumes today" defined precisely? [Clarity, Spec §US3 AS1] → tightened to "same keys and value types, `sent_at` int-or-null"; full field table stays in the contract, not the spec
- [x] CHK016 Is "no unexplained drift" (SC-004) given an objective definition? [Measurability, Spec §SC-004] → "±1%, every delta attributable"
- [x] CHK017 Is the late-event drop measurement method named? [Measurability, Spec §SC-005] → Flink `numLateRecordsDropped` on the `chat-messages` source

## Requirement Consistency

- [x] CHK018 Do FR-007 and FR-013 agree on who clears the mark and on what event? [Consistency] → `data-model.md` "Skip and re-check rule" is the single source; both FRs point at the 7-day rule
- [x] CHK019 Is the 2 s watermark tolerance used consistently between FR-009 and FR-010? [Consistency] → yes
- [x] CHK020 Is "500 channels" used consistently? [Consistency] → near-term target in the Overview; every SC measured at 500; Assumptions flags "measured to 394"
- [x] CHK021 Do the spec Edge Cases and the `research.md` risk list describe the same failure set? [Consistency] → the 429 case (R3) was missing from Edge Cases; added
- [x] CHK022 Does FR-001 stay consistent with every other requirement (no implied fallback transport)? [Consistency] → yes; `git revert` is the only fallback, stated in Assumptions

## Acceptance Criteria Quality

- [x] CHK023 Does every functional requirement have a matching Success Criterion or acceptance scenario? [Acceptance Criteria, Gap] → FR-011 via tasks T011; FR-012 via new SC-007; FR-013 via new US4 scenario 3
- [x] CHK024 Is there an acceptance criterion for FR-002 distinct from SC-002? [Coverage] → SC-002 (poll duration flat) is the accepted observable proxy
- [x] CHK025 Is FR-014's conditional phrased so it can be judged done or not-applicable? [Ambiguity] → yes, once T004 runs; tasks T040 records the decision

## Scenario & Edge Case Coverage

- [x] CHK026 Are recovery requirements defined for Redis or Postgres unavailable during a reconcile? [Gap, Recovery] → NFR-002, tasks T014c
- [x] CHK027 Is behaviour specified when `metadata.message_timestamp` is missing or malformed? [Gap, Edge Case] → Edge Cases + contract (`sent_at` null, assigner falls back)
- [x] CHK028 Is behaviour defined when subscription enumeration fails mid-pagination at start-up? [Gap, Edge Case] → NFR-003, tasks T014b
- [x] CHK029 Is subscription-churn behaviour defined for a channel oscillating across the JOIN/LEAVE band? [Coverage, Spec §FR-011] → the hysteresis band is the mitigation; FR-011 preserved
- [ ] CHK030 Is behaviour defined when a channel is in the desired set but its `broadcaster_id` cannot be resolved? [Gap, Edge Case] → open; current code skips silently, acceptable for now, worth an explicit line during implementation
- [x] CHK031 Are the reconciler's own resource-use limits at 500+ channels stated? [Gap, Non-Functional] → NFR-001, tasks T014d
- [x] CHK032 Are rollback requirements defined for a half-applied `streamers` migration? [Gap, Recovery] → `data-model.md`: one `BEGIN…COMMIT` transaction

## Dependencies & Assumptions

- [x] CHK033 Is the "re-seeding the token does not invalidate the running token" assumption in the spec? [Assumption] → Assumptions & Dependencies
- [x] CHK034 Is the "300-per-connection and 0-cost hold beyond 394" assumption flagged? [Assumption, Spec §SC-004] → Assumptions & Dependencies; T002 re-checks
- [x] CHK035 Is the PR #41 dependency stated in `spec.md`? [Dependency, Spec Gap] → Assumptions & Dependencies

## Notes

- **Resolved in this pass**: 31 of 35 items, via edits to `spec.md`
  (Assumptions section, NFR-001..003, SC-004/005/007, FR-005/014, US4 scenario,
  4 new edge cases), `plan.md` (cadence, process model), `tasks.md` (T014b–d,
  T028a, T038, T044a), `data-model.md` (transactional migration), and
  `research.md` (offset-direction reasoning).
- **Left open by decision**:
  - CHK003 — pool sizing stays in `data-model.md`; the spec keeps it black-box.
  - CHK013 / CHK015 — the spec references `contracts/chat-messages.schema.md`
    rather than restating the field-level detail.
  - CHK030 — a channel with an unresolvable `broadcaster_id` is skipped; make
    that explicit in code, not the spec.
- This checklist tests the requirements text, not the implementation. It is not
  a QA plan — that is Phase 5 in `tasks.md`.

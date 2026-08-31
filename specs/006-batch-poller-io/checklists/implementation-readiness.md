# Implementation Readiness Checklist: Batched Poller Datastore I/O

**Purpose**: Formal reviewer gate for the clarity, completeness, consistency, measurability, and executability of the Stage 4 requirements and task decomposition
**Created**: 2026-08-31
**Feature**: [spec.md](../spec.md)
**Plan**: [plan.md](../plan.md)
**Tasks**: [tasks.md](../tasks.md)
**Contract**: [poller-batch-contract.md](../contracts/poller-batch-contract.md)
**Depth**: Formal implementation-readiness gate
**Audience**: Stage 4 implementer and pull-request reviewer

**Note**: These items evaluate the written requirements and tasks, not whether future code passes its tests.

## Requirement-to-Task Traceability

- [x] CHK001 Does the task inventory give every FR-001 through FR-022 at least one specific implementation or evidence task rather than a catch-all mapping? [Completeness, Tasks §Requirement Traceability]
- [x] CHK002 Does each NFR-001 through NFR-005 map to implementation, validation, or explicit unchanged-configuration work with an objective completion condition? [Completeness, Tasks §Requirement Traceability]
- [x] CHK003 Does each SC-001 through SC-010 map to tasks that produce the exact evidence named by the criterion? [Measurability, Tasks §Success-Criteria Evidence]
- [x] CHK004 Is every setup, foundational, story, and polish task traceable to a requirement, story acceptance scenario, contract row, quickstart command, or completion gate? [Traceability, Tasks §Requirement Traceability, §Contract Failure Matrix, §Quickstart Command Readiness]
- [x] CHK005 Are story labels and story-phase boundaries consistent with the four user stories while leaving setup, foundational, and polish tasks unlabeled? [Consistency, Spec §User Scenarios & Testing; Tasks §Phase 1-7]
- [x] CHK006 Are excluded changes to dependencies, Python, schemas, scheduler policy, EventSub policy/capacity, Flink, feature 005, Redis layouts, and production thresholds absent from task descriptions and covered by a final scope gate? [Scope, Spec §Out of Scope; Tasks T095]

## Task Atomicity, Paths, and Execution Order

- [x] CHK007 Is each task small enough for one focused implementation turn and explicit about the exact repository file it changes or validates? [Clarity, Tasks T001-T096]
- [x] CHK008 Does every behavior-changing increment place a failing test task before the corresponding implementation task? [Ordering, Tasks §Within Each User Story]
- [x] CHK009 Are tasks that share `stream_monitoring_service.py` or `test_stream_monitoring.py` serialized rather than falsely marked parallel? [Consistency, Tasks §Genuine Parallel Opportunities]
- [x] CHK010 Do all `[P]` markers identify different files and settled prerequisites, with concrete concurrent partners documented? [Clarity, Tasks T056-T057, T088; Tasks §Genuine Parallel Opportunities]
- [x] CHK011 Is technical dependency order explicit among adapter foundations, metadata atomicity, lifecycle orchestration, scale evidence, observability, and completion review? [Consistency, Tasks §Dependencies and Execution Order]
- [x] CHK012 Does every user-story phase state a goal, independent test, and checkpoint sufficient to judge the increment without rereading the whole plan? [Completeness, Tasks §Phase 3-6]

## Failure and Recovery Requirement Coverage

- [x] CHK013 Does the failure matrix map every contract failure point to both precise test work and the production/helper work that enforces it? [Coverage, Contract §Failure; Tasks §Contract Failure Matrix]
- [x] CHK014 Are refresh failure before application, acknowledgement loss after application, and acknowledged element-level `ResponseError` specified as distinct test-adapter and poll scenarios? [Coverage, Contract §Test Adapter Contract; Tasks T003-T004, T044-T046]
- [x] CHK015 Do the tasks enumerate, for each fatal phase, the lifecycle, desired-intent, reconciler-notification, and success signals that must be absent? [Completeness, Spec §FR-013-014; Tasks T030, T041-T047]
- [x] CHK016 Is metadata-only failure consistently specified as whole-batch rollback plus healthy state/intent continuation and a distinct `metadata_failed` outcome? [Consistency, Spec §FR-011-012; Tasks T016-T021, T050-T053]
- [x] CHK017 Is desired-publication failure distinguished from refresh failure, including non-rollback of prior lifecycle output and re-read of whichever atomic intent is visible? [Clarity, Spec §FR-014; Tasks T047, T053]
- [x] CHK018 Is missing, non-numeric, or non-positive previous identity specified to suppress only the corrupt offline event while preserving other valid work? [Coverage, Spec §FR-008; Tasks T037, T040]
- [x] CHK019 Are next-poll recovery requirements explicit for poison metadata, indeterminate desired publication, partial/unknown refresh application, and keys expiring during a failed interval? [Recovery, Spec §Edge Cases, §FR-019-020; Tasks T025, T045, T047-T048]
- [x] CHK020 Are empty/empty and departures-only requirements explicit about every omitted and retained datastore phase, including empty desired publication? [Edge Case, Spec §FR-015; Tasks T013, T033, T049, T060]

## Datastore Dispatch and Metadata Measurability

- [x] CHK021 Is a remote interaction defined at the client dispatch boundary rather than as a helper invocation, row, queued command, or server-side command? [Clarity, Spec §Performance Measurement Profile; Tasks T008-T010]
- [x] CHK022 Are the ordered unique current-plus-departed key union, one `MGET`, empty omission, and response-length validation all specified in executable tasks? [Completeness, Spec §FR-002-003; Tasks T028-T032]
- [x] CHK023 Are non-transactional refresh mode, ranking-order `SETEX`, existing TTL/ID values, one pipeline execution, per-command responses, and `raise_on_error=True` all unambiguous? [Clarity, Contract §Online-State Contract; Tasks T002-T007, T033, T039]
- [x] CHK024 Is one-statement metadata persistence protected from hidden `execute_values()` paging by explicit `page_size=len(rows)` and actual `cursor.execute()` counting at 50, 500, and 900? [Measurability, Spec §FR-003-004; Tasks T010, T014-T015, T019, T058-T059]
- [x] CHK025 Are final-occurrence deduplication, database-clock timestamps, one transaction completion, rollback, pool reuse/discard, contextual failure visibility, and no per-row fallback specified together without contradictory escape paths? [Consistency, Spec §FR-009-012; Tasks T014-T021]
- [x] CHK026 Does real-Postgres evidence cover more than 500 inputs, insert/update/login changes, common advancing timestamps, poison rollback, connection reuse, and corrected next-poll retry while excluding fixture SQL from counts? [Coverage, Spec §FR-019, §SC-006; Tasks T022-T026]

## Lifecycle Equivalence and Ordering

- [x] CHK027 Are normalization and duplicate rules complete for ranking order, login-keyed desired semantics, final-ID metadata semantics, and repeated-login refresh order? [Completeness, Data Model §Ranked Channel; Tasks T018, T027, T031, T034]
- [x] CHK028 Is first/best-rank lifecycle evaluation for repeated logins specified independently from final ranking-order key value semantics? [Clarity, Plan §Snapshot once then refresh once; Tasks T034, T038-T039]
- [x] CHK029 Do the tasks cover entry-band new, outside-band new, stable, departed-present, departed-expired, real-ID, and corrupt-ID lifecycle scenarios with exact expected output cardinality? [Coverage, Spec §FR-006-008, §FR-020; Tasks T035-T037]
- [x] CHK030 Is it unambiguous that all lifecycle candidates derive from the immutable pre-refresh snapshot and publish only after acknowledged refresh success? [Consistency, Spec §FR-002, §FR-013; Tasks T032, T038-T040]

## Observability and Performance Evidence

- [x] CHK031 Are total outcomes, phase outcomes, phase names, and metadata failure-streak semantics finite, enumerated, and protected from exception/login label cardinality? [Measurability, Contract §Completion and Telemetry Contract; Tasks T021, T080-T085]
- [x] CHK032 Is exactly one final outcome and total-duration observation assigned to one completion path with timing from immediately before ranking retrieval through notification or failure? [Clarity, Spec §FR-017, §Performance Measurement Profile; Tasks T053, T080, T084]
- [x] CHK033 Are deterministic A/B fixture disjointness, exact post-filter eligibility, page size, representative disabled proportion, live page calibration, and ranking budget all specified reproducibly? [Measurability, Spec §Performance Measurement Profile; Tasks T054-T064]
- [x] CHK034 Are warm-ups, 20 completed samples, nearest-rank p95, whole-poll failure replacement, stable/turnover state setup, and overlap gates defined with one interpretation? [Acceptance Criteria, Spec §SC-001-002; Tasks T055, T065-T067]
- [x] CHK035 Are post-convergence start, composed pass callbacks, direct monotonic timestamps, adjacent-gap limits, 30-minute duration, and all scheduler event classes specified for steady-state evidence? [Measurability, Spec §NFR-002, §SC-004; Tasks T068-T070, T078]
- [x] CHK036 Are cold-subscription preconditions, zero initial subscriptions, required real backoff, accepted-window progress, poll schedulability, unchanged transport delegation, and the absence of a new convergence deadline consistent? [Consistency, Spec §FR-022, §SC-010; Tasks T071-T075, T079]

## Environment, Configuration, Rollback, and Completion Gates

- [x] CHK037 Is the boundary between local code/unit/driver-calculation gates and separate-machine real datastore, duration, steady-state, and Twitch-backoff runs explicit for every affected command? [Boundary, Tasks §Local Merge Gate, §External Acceptance Gate]
- [x] CHK038 Are production 150/300/120 values protected by an automated guard while all other unchanged dependency/schema/scheduler/EventSub/Flink boundaries remain explicit completion criteria? [Scope, Spec §FR-021, §NFR-005; Tasks T086, T095]
- [x] CHK039 Are validation-only modules explicitly kept out of production Docker copies and Compose bind mounts, with an automated source guard? [Boundary, Plan §Project Structure; Tasks T087]
- [x] CHK040 Are unchanged-value deployment, evidence collection, any later channel ramp, and one-revision rollback specified as separate review decisions with no hidden migration or configuration reversal? [Completeness, Spec §US4, §SC-009; Tasks T088-T091, T096]

## Notes

- Mark an item complete only after re-reading its cited artifacts.
- Any failed item blocks Stage 4 until the cited artifact is corrected without weakening the underlying requirement.

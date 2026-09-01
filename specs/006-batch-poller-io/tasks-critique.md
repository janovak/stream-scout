# Stage 3 Task Critique: Batched Poller Datastore I/O

**Date**: 2026-08-31
**Scope**: Task generation, cross-artifact analysis, implementation-readiness checklist, and independent rubber-duck review
**Final verdict**: **GO for implementation**

## `/speckit.analyze` Metrics

| Metric | Initial | Final |
|---|---:|---:|
| Functional + non-functional requirements | 27 | 27 |
| Implementation tasks | 96 | 96 |
| Requirements with task coverage | 27/27 (100%) | 27/27 (100%) |
| Unmapped tasks | 0 | 0 |
| Constitution conflicts | 0 | 0 |
| Ambiguities | 2 | 0 |
| Duplications | 2 | 0 |
| CRITICAL findings | 0 | 0 |
| HIGH findings | 2 | 0 |
| MEDIUM findings | 3 | 0 |
| LOW findings | 1 | 0 |

The final read-only pass was rerun after both analysis remediation and accepted
rubber-duck edits. It found no contradiction, uncovered requirement, unmapped
task, invalid dependency, false parallel marker, scope leak, or unmeasurable
acceptance gate.

## Initial Analysis Findings

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| A-001 | HIGH | The real-Postgres recovery task said next invocation rather than the required next poll path. | **Accepted**: T025 now invokes a poison poll followed by a corrected `poll_top_streams()` retry. |
| A-002 | HIGH | Driver tasks told operators to use isolated state but did not require target preflight. | **Accepted**: T055/T057/T061 now test and enforce explicit isolated datastore targets. |
| A-003 | MEDIUM | Metadata preparation ownership overlapped the metadata and orchestration phases. | **Accepted**: T018 owns the batch helper; T031 owns its one-call poll integration. |
| A-004 | MEDIUM | T021 and T083 both appeared to own the metadata failure gauge. | **Accepted**: T021 owns the gauge; T083 explicitly reuses it. |
| A-005 | MEDIUM | Outcome selection/emission and the total timing boundary were split ambiguously. | **Accepted**: T053 selects one outcome; T084 is the sole timed emission path with exact start/end boundaries. |
| A-006 | LOW | T092 and T094 duplicated the full local test gate. | **Accepted**: T092 is the focused US4 selector gate; T094 remains the final full suite. |

No analysis finding was deliberately left open.

## Independent Rubber-Duck Findings

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| F1 | HIGH | A counting cursor lacking `connection.encoding` and bytes `mogrify()` could force tests to patch `execute_values()` and hide internal paging. | **Accepted**: T010/T014/T058-T059 require a real-helper-compatible cursor and prohibit helper patching in count gates. |
| F2 | HIGH | The poll builder could retain a mocked metadata path and make zero dispatches look scale-independent. | **Accepted**: T012 installs the counting pool and forbids batch/snapshot/refresh mocks; T058-T059 require positive counts of exactly one. |
| F3 | MEDIUM | Real-Postgres coverage did not protect non-batch `streamers` columns. | **Accepted**: T023 now preserves `first_seen_at`, clipping, and EventSub refusal fields while updating login/time. |
| F4 | MEDIUM | Script-style Phase 5 modules lacked a specified pytest/direct-CLI import seam. | **Accepted**: T054-T055 test both modes and T057 implements both without changing production packaging. |
| F5 | MEDIUM | One task bundled all fatal and non-fatal poll gates. | **Accepted**: T051-T053 now separate ranking/read gates, snapshot/refresh gates, and final desired/metadata outcome handling. |
| F6 | LOW | The reviewer proposed a seventh desired-read phase metric. | **Rejected**: `desired_read` is bounded failed-phase log context, not a required phase-metric label; FR-017 and the contract intentionally require six phase metrics. |
| F7 | LOW | Existing poll tests were not explicitly protected as the pre-batch lifecycle baseline. | **Accepted**: T027 preserves the golden assertions and names the sole FR-008 correction. |
| F8 | LOW | Repeated-login online-event identity was not pinned to the first/best occurrence. | **Accepted**: `data-model.md`, the contract, and T034 now specify the first/best occurrence's ID while refresh retains ranking-order last-write behavior. |
| F9 | LOW | The plan's source inventory omitted `test_desired_set_store.py`, where adapter-contract tasks live. | **Accepted**: the plan now lists that test file and its role. |

## Final Coverage and Readiness

- **Requirements**: FR-001 through FR-022 and NFR-001 through NFR-005 all
  have concrete implementation, validation, or unchanged-boundary tasks.
- **Success criteria**: SC-001 through SC-010 all map to evidence-producing
  tasks.
- **Failure contract**: all 10 modeled failure rows map to tests and enforcing
  implementation tasks, including before-application failure,
  acknowledgement loss, and element-level Redis errors.
- **Command readiness**: all 10 quickstart sections map to code, tests,
  documentation, or external execution tasks.
- **Task quality**: 96 unique sequential tasks; story labels are valid; the
  only `[P]` tasks are T056, T057, and T088, each with a documented,
  independent-file concurrency opportunity.
- **Checklist**:
  `checklists\implementation-readiness.md` passes 40/40 formal requirements
  quality checks with 100% traceability.
- **Scope**: Stage 3 changes documentation only. No service, dependency,
  Python image, schema, scheduler, EventSub, Flink, Redis layout, feature 005,
  Docker value, or production-threshold change is included.

## Deferred External Evidence

Implementation must create every fixture, driver, proxy, opt-in test, and
operator procedure locally. Execution of real-Postgres (T026), datastore
operation counts (T076), calibrated poll profiles (T077), 30-minute
steady-state runs (T078), isolated 900-channel cold backoff (T079), unchanged
production deployment evidence (T090), and rollback evidence (T091) remains
explicitly deferred to the user's separate production-equivalent machine.
Deferral does not convert any external acceptance gate into a local pass.

## Verdict

**GO**. The task graph is implementation-ready, constitution-compliant,
fully traceable, failure-complete, measurably testable, and bounded to the
settled feature scope. No CRITICAL, HIGH, MEDIUM, or LOW analysis finding
remains open; the sole rejected rubber-duck suggestion is documented above.

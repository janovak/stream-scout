# Requirements Quality Checklist: Batched Poller Datastore I/O

**Purpose**: Evaluate whether the performance, lifecycle, failure, convergence,
and rollout requirements are complete, precise, consistent, and measurable
before technical planning
**Created**: 2026-08-30
**Feature**: [spec.md](../spec.md)
**Focus**: Scale and performance; lifecycle equivalence; partial failure and
recovery; bounded network interactions; steady-state versus cold-start
convergence; rollout and rollback boundaries
**Depth**: Stage 1 release gate
**Audience**: Specification author, planner, and PR reviewer

**Note**: This checklist evaluates the requirements text. It does not test the
future implementation.

## Performance and Scale Requirement Completeness

- [x] CHK001 Are distinct poll-duration targets documented for 500 and 900
  ranked channels? [Completeness, Spec §NFR-001, §SC-001-002]
- [x] CHK002 Is the measured poll interval bounded from a stated start event to
  a stated end event? [Clarity, Spec §Performance Measurement Profile]
- [x] CHK003 Are percentile, sample count, warm-up, and ranking-change profiles
  specified so the duration targets have one objective interpretation?
  [Measurability, Spec §SC-001-002, §Performance Measurement Profile]
- [x] CHK004 Are representative remote-latency and topology assumptions
  documented, together with a requirement to record the observed latency
  during measurement? [Assumption, Spec §Performance Measurement Profile]
- [x] CHK005 Are both stable rankings and complete channel turnover covered at
  each performance scale point? [Coverage, Spec §US1, §SC-001-002]
- [x] CHK006 Is the relationship among the 10-second target, 120-second poll
  interval, and 180-second online-state expiration stated clearly enough to
  judge starvation risk? [Clarity, Spec §Overview, §NFR-001-002]
- [x] CHK007 Are upstream throttling, injected failures, process startup, and
  subscription convergence explicitly included in or excluded from latency
  measurement? [Boundary, Spec §Performance Measurement Profile]

## Bounded Interaction Requirement Quality

- [x] CHK008 Does the specification place explicit per-phase ceilings on
  online-state reads, online-state refreshes, and metadata write transactions?
  [Measurability, Spec §FR-003]
- [x] CHK009 Is channel-count independence required at 50, 500, and 900
  channels rather than inferred from one same-size comparison?
  [Completeness, Spec §FR-004, §SC-003]
- [x] CHK010 Is a client-visible request/response cycle distinguished from
  commands or rows processed inside an acknowledged batch? [Clarity, Spec
  §FR-004, §Performance Measurement Profile]
- [x] CHK011 Does the specification explicitly allow payload bytes, local CPU
  iteration, datastore-side work, and ranking pagination to remain
  proportional to channel count? [Scope, Spec §Overview, §FR-004]
- [x] CHK012 Are the already-bounded desired-set and clipping-eligibility
  interactions separated from the interactions this feature must batch?
  [Consistency, Spec §FR-003, §Assumptions & Dependencies]
- [x] CHK013 Are empty-phase requirements precise about which datastore
  interactions are omitted and which remain necessary for departures and
  empty desired intent? [Edge Case, Spec §FR-015, §Edge Cases]

## Lifecycle and Desired-Set Requirement Quality

- [x] CHK014 Are empty, partial, stable, and completely changed rankings all
  covered by desired-set and hysteresis requirements? [Coverage, Spec §FR-001,
  §US2]
- [x] CHK015 Is the pre-refresh snapshot defined across the union of current
  and departed logins after desired membership and departures are known?
  [Clarity, Spec §FR-002]
- [x] CHK016 Is the numeric direction of the entry threshold unambiguous for
  `online` event eligibility? [Clarity, Spec §US2 AS1, §FR-006]
- [x] CHK017 Is the no-event behavior for a new key outside the entry band
  documented independently from its required state refresh?
  [Completeness, Spec §FR-007]
- [x] CHK018 Are both conditions for `offline` output documented: departure
  from desired intent and absence from the pre-refresh online-state view?
  [Clarity, Spec §FR-008]
- [x] CHK019 Is the source of the real broadcaster ID for a departed login
  explicit? [Data Integrity, Spec §FR-008]
- [x] CHK020 Are lifecycle equivalence claims scoped honestly when a remote
  refresh outcome is indeterminate because acknowledgement is lost?
  [Boundary, Spec §Edge Cases, §NFR-004]

## Metadata Integrity and Recovery Requirement Quality

- [x] CHK021 Does successful metadata persistence cover every unique observed
  identity, inserts, updates, login changes, and `last_seen_at` advancement?
  [Completeness, Spec §FR-009]
- [x] CHK022 Is duplicate-identity resolution deterministic and tied to the
  final state produced by prior sequential processing? [Clarity, Spec §FR-010]
- [x] CHK023 Are malformed input and remote persistence failure both defined as
  whole-batch rollback cases? [Recovery, Spec §FR-011]
- [x] CHK024 Are logging context, clean connection return, next-poll retry, and
  the prohibition on metadata-success signaling all documented?
  [Completeness, Spec §FR-011-012]
- [x] CHK025 Is the decision to continue other healthy poll phases after a
  metadata failure stated without implying that metadata succeeded?
  [Consistency, Spec §FR-012, §US3 AS4]
- [x] CHK026 Is real-Postgres coverage required at a row count high enough to
  expose accidental internal paging, as well as duplicate and rollback SQL
  behavior? [Coverage, Spec §FR-019, §SC-006]

## Online-State and Intent Failure Requirement Quality

- [x] CHK027 Are snapshot-read failure and refresh-batch failure both covered
  rather than collapsed into a generic datastore error? [Completeness, Spec
  §FR-013, §Edge Cases]
- [x] CHK028 Does the specification state that failed online-state refresh
  prevents desired-intent publication, lifecycle output, reconciler
  notification, and poll-success signaling? [Recovery, Spec §FR-013]
- [x] CHK029 Is behavior after an indeterminate refresh outcome explicit about
  avoiding speculative events and using next-poll observed state?
  [Clarity, Spec §Edge Cases]
- [x] CHK030 Is desired-intent publication failure distinguished from
  online-state failure, including known-versus-indeterminate outcomes,
  next-poll replacement, and reconciler-notification suppression?
  [Completeness, Spec §FR-014]
- [x] CHK031 Are lifecycle side effects that precede desired-intent publication
  failure explicitly described as non-transactional and not rolled back?
  [Consistency, Spec §FR-014, §Edge Cases]
- [x] CHK032 Do all remote failure requirements prohibit success-shaped state
  while retaining visible error and retry semantics? [Consistency, Spec
  §FR-011-014, §NFR-004]

## Steady-State, Cold-Start, and Capacity Boundaries

- [x] CHK033 Is reconciler schedulability measurable during separate
  steady-state 500- and 900-channel runs? [Measurability, Spec §NFR-002,
  §SC-004]
- [x] CHK034 Is the reconciler last-success advancement window stated and
  separated from the poll-duration metric? [Clarity, Spec §NFR-002]
- [x] CHK035 Is the cold-start exception explicit that existing external retry
  and backoff may exceed 120 seconds and freeze last-success while one pass is
  active? [Boundary, Spec §Scope Decision, §Assumptions & Dependencies]
- [x] CHK036 Does the specification avoid promising a new hard 900-channel
  cold-convergence deadline while still requiring eventual progress without
  poller starvation? [Scope, Spec §FR-022]
- [x] CHK037 Are subscription-create rate-limit policy and transport capacity
  changes explicitly out of scope? [Scope, Spec §Out of Scope]
- [x] CHK038 Is 900 documented as an exact no-headroom transport ceiling rather
  than a generally safe operational target? [Assumption, Spec §Scope Decision,
  §Assumptions & Dependencies]

## Rollout, Rollback, Observability, and Traceability

- [x] CHK039 Are total poll duration and the named phase durations required for
  both successful and failed polls? [Observability, Spec §FR-017, §US4]
- [x] CHK040 Is a failed phase required to remain distinguishable from a
  successful poll in telemetry and logs? [Clarity, Spec §FR-017]
- [x] CHK041 Are production entry/retention thresholds and the clipping fetch
  buffer explicitly frozen for this feature's deployment? [Boundary, Spec
  §FR-021, §SC-009]
- [x] CHK042 Is a later channel ramp separated from code deployment as its own
  operational decision? [Scope, Spec §FR-021]
- [x] CHK043 Are rollback requirements compatible with no schema, dependency,
  or threshold change? [Recovery, Spec §US4 AS3, §SC-009]
- [x] CHK044 Are dependency upgrades, async-client migration, executor
  relocation, reconciler policy changes, transport changes, and feature 005
  work consistently excluded? [Consistency, Spec §NFR-003-005, §Out of Scope]
- [x] CHK045 Do automated validation requirements correct the prior same-size
  blind spot by comparing 50 versus 500 channels and covering stable versus
  complete-turnover inputs? [Traceability, Spec §FR-018]
- [x] CHK046 Do lifecycle, real-Postgres, operation-count, and failure
  validation requirements collectively cover every critical functional and
  non-functional outcome? [Coverage, Spec §FR-018-020, §SC-003-007]
- [x] CHK047 Does every success criterion trace to at least one user scenario
  or normative requirement without introducing new scope? [Traceability, Spec
  §US1-US4, §FR-001-022, §NFR-001-005, §SC-001-010]
- [x] CHK048 Does the measurement profile require the observed eligible count
  to equal each nominal scale point and distinguish test-only scale overrides
  from frozen production configuration? [Clarity, Spec §Performance
  Measurement Profile, §FR-021]
- [x] CHK049 Is complete ranking turnover made reproducible without claiming
  that a live ranking naturally turns over, while retaining representative
  pagination delay? [Assumption, Spec §Performance Measurement Profile]
- [x] CHK050 Are post-convergence steady-state start conditions and maximum
  last-success gaps specified tightly enough to fail the currently observed
  starvation behavior? [Measurability, Spec §US1 AS4, §NFR-002, §SC-004]
- [x] CHK051 Are element-level errors inside an acknowledged online-state batch
  classified explicitly as phase failure? [Recovery, Spec §FR-013, §SC-007]
- [x] CHK052 Is lifecycle behavior after online state expires during a failed
  refresh interval documented, including possible correlated `online`
  re-emission? [Edge Case, Spec §Edge Cases, §FR-020]
- [x] CHK053 Is missing prior broadcaster identity handled without contradicting
  the requirement that offline events carry real IDs? [Data Integrity, Spec
  §US2 AS6, §FR-008]
- [x] CHK054 Is the testable polling obligation during a rate-limited
  900-channel cold start covered separately from unbounded convergence time?
  [Coverage, Spec §US1 AS5, §FR-022, §SC-010]
- [x] CHK055 Is the source and timestamp resolution for reconciler pass-gap
  measurement explicit, without relying on a production scrape interval as
  fine as the acceptance bound? [Measurability, Spec §FR-017, §Performance
  Measurement Profile]
- [x] CHK056 Is remote operation counting located at the datastore
  client/connection dispatch boundary so internal statement paging cannot hide
  behind one helper invocation? [Clarity, Spec §Performance Measurement
  Profile]
- [x] CHK057 Are persistent poison-record consequences documented, including
  all-row staleness, repeated retry, alertable failure streak, and no per-row
  fallback? [Recovery, Spec §US3 AS5, §FR-011]
- [x] CHK058 Is the intended missing-ID behavior carved out from the otherwise
  strict before/after lifecycle equivalence criterion? [Consistency, Spec
  §FR-008, §SC-005]
- [x] CHK059 Is "cold start" in the 900-channel acceptance scenario defined as
  cold subscription state after service and datastore initialization?
  [Clarity, Spec §SC-010, §Performance Measurement Profile]
- [x] CHK060 Do operation-count requirements explicitly cover both fully empty
  polls and empty-current polls that still have departures? [Coverage, Spec
  §FR-015, §FR-018]
- [x] CHK061 Is the ranking-pagination share of each end-to-end timing budget
  required to be recorded from raw count, page count, and same-environment
  per-page latency? [Measurability, Spec §Performance Measurement Profile]
- [x] CHK062 Is lifecycle-state setup for stable and complete-turnover
  performance profiles stated so event volume is not left accidental?
  [Clarity, Spec §Performance Measurement Profile]
- [x] CHK063 Is the one-statement metadata constraint explicitly tied to
  channel-count-independent network dispatches rather than constant
  datastore-side work? [Consistency, Spec §FR-003-004]
- [x] CHK064 Are cold-start obligations expressed as measurable poll
  scheduling and accepted-window progress while full convergence remains
  conditional on unchanged external policy? [Scope, Spec §Scope Decision,
  §FR-022, §SC-010]
- [x] CHK065 Are previous desired-set read failures specified separately from
  online-state snapshot and refresh failures? [Completeness, Spec §FR-013,
  §SC-007]
- [x] CHK066 Does desired-publication failure avoid falsely promising that the
  old version survived an indeterminate acknowledgement, while retaining
  atomic replacement and retry semantics? [Clarity, Spec §FR-014, §Edge Cases]

## Notes

- Quality pass completed 2026-08-30: 66 of 66 items are satisfied. CHK008 and
  CHK026 initially exposed that "one transaction" did not prohibit multiple
  channel-count-dependent statements; FR-003 now requires one bulk statement
  regardless of row count. CHK006 prompted an explicit explanation of the
  timing margin relative to the scheduler interval and online-state expiration.
- Independent rubber-duck findings F1-F2 and F4-F7/F9-F10 were accepted:
  reconciler cadence, exact measurement inputs, percentile rules, element-level
  failures, failed-refresh expiry, missing IDs, 900-count coverage, and
  cold-start polling now have explicit requirements. F3 was accepted in part:
  the operator-supplied baseline is now identified and reconciled with the
  earlier ramp record; a new relative-improvement gate was not added because
  the absolute latency and operation-count gates define success. F8 was
  rejected: equal counts at 50/500/900 are intentional because fixed-size
  paging would still make network cycles depend on channel count and is the
  regression this feature must prevent.
- A closure review found that the revised pass-gap and operation-count gates
  still lacked measurement-boundary detail. FR-017 and the Performance
  Measurement Profile now require direct per-pass timestamps and datastore
  connection-boundary counts. It also prompted explicit poison-record
  staleness, cold-subscription-state, empty-operation, ranking-budget, and
  accepted-window progress requirements. Its suggestion to relax the
  one-statement metadata constraint was rejected for the same reason as F8;
  FR-003 now states that the constraint prevents channel-count-dependent
  dispatches, not proportional server row work.
- Items were checked only after the cited specification text was re-read.

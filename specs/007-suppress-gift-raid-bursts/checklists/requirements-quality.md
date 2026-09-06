# Feature 007 Requirements Quality Checklist

**Purpose**: Formal PR and release-review gate for the clarity, completeness,
consistency, measurability, and scenario coverage of Feature 007 requirements
in the roadmap and spec-kit artifacts.
**Created**: 2026-09-04
**Feature**: [Suppress Gift and Raid Chat Bursts](../spec.md)
**Review audience**: Feature author and merge reviewer
**Review timing**: Before PR approval and again before release sign-off

**Evaluation rule**: `[x]` means the current written artifacts specify the
requirement clearly and consistently. `[ ]` identifies an unresolved
requirements-quality defect; its `Gap` note is part of the release gate.

## Requirement Completeness - Capacity and Units

- [x] CHK001 Is the account limit re-derived explicitly as 3 sessions x 300 enabled subscriptions = 900 subscriptions for the existing client-id/user-id pair? [Completeness, Spec §Overview, NFR-001; Research §1.1, §1.4]
- [x] CHK002 Are monitored channels and individual subscriptions defined as different units, with two independently tracked coverage subscriptions required per monitored channel? [Clarity, Spec §FR-001, §FR-002, §FR-015; Data Model §1.1-§1.4]
- [x] CHK003 Is the ceiling arithmetic complete and internally consistent: 450 channels x 2 subscriptions = 900 subscriptions, i.e. exactly the account limit with 0 guaranteed free slots, and is it explained as the halved analogue of the pre-007 800/900 ramp? [Consistency, Spec §Overview, §FR-013-§FR-015, §SC-006; Research §1.4, §6; Autonomous Decisions §27]
- [x] CHK004 Is the per-session packing consequence specified as at most 150 co-located channel pairs per 300-subscription session, with pair splitting across sessions defined as a supported placement — including the atomic one-slot-per-connection reservation when no single connection can hold the pair and the retention of the successful half after a post-reservation failure? [Coverage, Data Model §4, I25; Research §3 R2, R5; Autonomous Decisions §28; Tasks T061-T062]
- [x] CHK005 Are the entry threshold of 400, the retention-and-maximum threshold of 450, the fresh-versus-incumbent asymmetry at rank 401-450, and the 451st-channel exclusion stated objectively? [Measurability, Spec §FR-013, §SC-006, §Edge Cases, US3 scenarios 3-4; Data Model §4, I24; Research §6]
- [x] CHK006 Are capacity-reporting requirements explicit about connection occupancy and total occupancy using subscription units while desired-set and coverage signals use channel units, and is connection-full state — including full below the 300 cap — reportable? [Clarity, Spec §FR-015; Data Model §1.4, I4-I5, I27; Plan §Design at a glance]
- [x] CHK007 Are reconnect, adoption, reservation, and mid-ramp capacity requirements written so that exact capacity is engineered rather than assumed: reconnect and adoption normally consume no new slot, and the exceptional zero-slack cases are enumerated and mitigated instead of absorbed by a reserve? [Completeness, Spec §FR-014, §NFR-001, §Assumptions; Data Model §4, I25-I27; Research §1.4, §3, R5, R15-R18; Autonomous Decisions §27-§28]
- [x] CHK008 Is the disposition of the desired-set churn signal defined — advisory bounded-label telemetry with no numeric release gate, no observation window, and no power to block enabling gating — and is the entry/retention band's effect on churn explained? [Measurability, Spec §NFR-007, §SC-011; Research §6, D10a; Data Model I20; Tasks T064, §Deferred Deployed Evidence]

## Scenario Coverage - Dual-Subscription Lifecycle

- [x] CHK009 Is the full channel-coverage state model defined, including `absent`, `chat_only`, `notification_only`, `complete`, and `degraded_chat_only`, without inferring either slot from its sibling? [Completeness, Spec §FR-002, §NFR-003; Data Model §1.2-§1.3]
- [x] CHK010 Are create requirements type-aware for an absent channel and both partial states, including creating only missing types, preserving surviving subscription identity, and independently recording the session used for each create? [Coverage, Data Model §5.1, I2-I3; Tasks T010, T018]
- [x] CHK011 Are list and ordinary adoption requirements defined as two type-filtered enumerations joined by channel, with enabled/live-session criteria and explicit reporting for both partial directions? [Clarity, Data Model §1.3, I1; Research §3; Tasks T011, T019]
- [x] CHK012 Is enumeration failure behavior specified for either type walk so an incomplete view cannot authorize destructive reconciliation drops? [Exception Coverage, Research §3 R1; Tasks T011, T019]
- [x] CHK013 Are 409-conflict requirements type-aware for both chat and notification, matching coverage type, broadcaster, and a live session held by the pool before adoption? [Coverage, Data Model §5.1; Research §3; Tasks T012, T020]
- [x] CHK014 Are channel-deletion requirements complete for deleting both types independently, treating already-absent subscriptions as success, following reconnect-rotated IDs, and retaining retryable state after a one-sided failure? [Recovery Coverage, Data Model §5.2, I3; Tasks T013, T021]
- [x] CHK015 Are revocation requirements explicit that only the identified coverage type is removed, its sibling remains, one lost subscription is reported, and the resulting partial state is repairable even after ID rotation? [Recovery Coverage, Data Model §5.4, I3; Tasks T014, T022]
- [x] CHK016 Are reconnect and retirement requirements type-aware for independent session staleness and ID rotation, clearing every slot on a dead connection without removing a split sibling held elsewhere? [Recovery Coverage, Data Model §5.5; Tasks T015, T023]
- [x] CHK017 Are notification-refusal requirements distinguished from chat-refusal requirements, including fail-open retention of chat, bounded retry behavior with a stated retry period and reconnect-forced re-eligibility, visible auxiliary degradation, and clearing degradation on successful re-adoption? [Exception Coverage, Spec §FR-001, §NFR-003; Autonomous Decisions §7 (superseded), §17; Data Model §1.3, §5.4.1, I17; Tasks T016, T024]
- [x] CHK018 Is the `degraded_chat_only` exception consistent with the unconditional dual-coverage invariant and the definition of a channel that has completed convergence? [Conflict resolved, Spec §FR-001, §NFR-003, §SC-001, §Edge Cases, US3 scenario 9; Data Model §1.3, §5.4.1, I1, I17; Autonomous Decisions §17]

## Requirement Clarity - Sparse Input, Event Time, and Lateness

- [x] CHK019 Is the two-input event-time hazard stated explicitly — the operator watermark is the minimum of both inputs, so either an untrusted future chat timestamp or a sparse suppression input can stall chat-time evaluation — **and** is the idle-to-active re-entry case covered, with the bounded hold; and is idleness located per assignment subtask under the partitions = source parallelism = assignment parallelism = 4 one-to-one condition, with mismatch/rescale and the two added Python stages' deployed process/RSS impact reserved for E3 revalidation? [Clarity, Research §4.1, §4.1.1, §4.1.2, R3, R10, R12-R14; Plan §Summary; Data Model I15, I16, I22-I23; Autonomous Decisions §18, §25-§26; Tasks T045, T050; Quickstart §A4, §B5]
- [x] CHK020 Are trustworthy event-time requirements defined symmetrically for chat and suppression — including exact bounded → idleness → assigner-last construction and post-source attachment — with fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`; suppression's two-layer fallback/rejection; and chat accepting only plain-int/non-bool `sent_at` through +30,000 ms while missing/null/string/float/bool/+30,001 ms uses Kafka record time without rewriting, rejecting, or dropping chat? [Consistency, Spec §FR-003, §FR-017; Contract §1.1-§1.1.1; Research §4.1.2, §4.3, R12-R14; Data Model I21-I23; Autonomous Decisions §23-§26]
- [x] CHK021 Are idleness and partition requirements quantified as a 5-second suppression idleness timeout below chat's 10 seconds, with both real strategies built bounded out-of-orderness → idleness → assigner last and attached post-source, and four partitions matching source and assignment parallelism in a form assertable without claiming deployed execution? [Measurability, Research §4.1 D4, §4.1.2 R13-R14, §4.7 D16; Contract §1, §1.1.1; Plan §Structure Decision; Tasks T029, T031, T038, T045]
- [x] CHK022 Are startup and restart requirements explicit about latest offsets, empty suppression state, no replay of old notices, and fail-open operation until a later notice establishes state? [Recovery Coverage, Data Model §5.6; Research §4.1 D4; Contract §1]
- [x] CHK023 Are late-notice semantics complete: no waiting, retraction, or retroactive change, with later decisions gated only when their peaks lie inside the established notice-bounded interval, so a pre-notice peak reported after delivery remains eligible? [Coverage, Spec §FR-007, §FR-018, User Story 1 scenarios 2 and 6; Contract §4.1 rules 7-8; Data Model §3.2; Autonomous Decision §22]
- [x] CHK024 Are both interval boundaries and the compared instant unambiguous: the spike peak second is used, `suppress_from_ms <= peak_ms < suppress_until_ms`, a pre-notice peak is eligible, the notice instant is included, and the deadline is excluded? [Clarity, Spec §FR-006-§FR-007, §SC-003; Data Model §3.1-§3.2; Research D5; Tasks T030, T040, T044]

## Requirement Consistency - Output-Only Detector Gating

- [x] CHK025 Are message-count, rolling-baseline, bucket-retention, and expired-bucket requirements defined as identical between gated and ungated processing? [Consistency, Spec §FR-008, §SC-004; Data Model §3.3 I11; Tasks T049]
- [x] CHK026 Are peak-hold requirements explicit about identical open, extend, peak, close, and expiry trajectories even when an otherwise qualifying output is suppressed? [Completeness, Spec §FR-008, §SC-004; Research §4.4 D6; Tasks T049]
- [x] CHK027 Are cooldown and `last_fire_second` semantics resolved explicitly so a suppressed would-have-clipped decision updates them exactly as the ungated decision would? [Clarity, Data Model §3.3; Research §4.4 D6; Autonomous Decisions §10]
- [x] CHK028 Are timer requirements complete: suppression records register no timer, chat-side chain timers remain unchanged, and watermark-driven evaluation continues on the same schedule? [Coverage, Contract §4.1 rule 7; Data Model §3.3; Research §4.2, §4.5]
- [x] CHK029 Is the permitted difference between paired runs narrowly defined as clip output plus the required suppression metric and structured log, with every other keyed-state write unchanged? [Measurability, Spec §SC-004; Data Model I11, I14; Quickstart §A4]
- [x] CHK030 Are anomaly-counting and channel-isolation requirements consistent with output-only gating, including continued anomaly counting and no notice changing another broadcaster's eligibility or state? [Consistency, Spec §NFR-002, §NFR-006; Plan §Design at a glance; Research R8]

## Non-Functional Quality - Contract, Failure, and Observability

- [x] CHK031 Are fail-open/no-data-loss requirements complete for suppression failures and malformed or over-future chat `sent_at`, with suppression records remaining visible for downstream rejection while chat falls back only for event-time assignment and is never rewritten, rejected, or dropped? [Coverage, Spec §FR-003, §FR-011, §FR-017-§FR-018; Contract §1.1, §1.1.1, §3.6, §4; Data Model I9-I12, I21, I23]
- [x] CHK032 Are healthy coverage, both partial states, auxiliary refusal, delivery lag, ignored notices, malformed input, rejected records, capacity refusal — classified distinctly from provider refusal and transient faults, and without arming a transient growth backoff or writing the durable refusal cache — connection-full state including full below the 300 cap, and each suppressed spike required to be separately attributable and distinguishable through appropriately bounded metrics/logs, with broadcaster attribution retained where NFR-006 requires it? [Completeness, Spec §NFR-001, §NFR-004-§NFR-006, US3 scenarios 7-8; Plan §Design at a glance; Data Model I26-I27; Autonomous Decisions §28; Tasks T025, T033, T039-T042, T061-T063]
- [x] CHK033 Is "lagging suppression delivery" quantified from trusted records using `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`, with the fixed +30-second future boundary checked first, accepted negative-skew clamp/log behavior, over-bound rejection before observation/state, `suppression_delivery_age_seconds`, lag threshold, optional `received_at_ms` diagnostic-only semantics, and silence distinguished as idle/unknown? [Ambiguity resolved, Spec §FR-017, §NFR-005, §SC-010; Research §4.3, §4.6, D13; Contract §2.2, §4.1 rules 4-5; Data Model I19, I21; Autonomous Decisions §20, §23; Tasks T039, T042, T047]
- [x] CHK034 Is the producer/consumer contract versioned and evolution-safe, with producer/consumer deployment order for incompatible versions, one-hour coexistence, and rules for optional versus required changes? [Completeness, Contract §2.1, §4.2, §6; Research D8]
- [x] CHK035 Is every ordering constraint explicit: bounded out-of-orderness → idleness → Python assigner last, post-source attachment on both streams, source-time trust before watermark generation without payload mutation/drop, and then suppression-only decode plus original-value rejection before observation/state; and does the producer alone still own key/payload agreement? [Ambiguity resolved, Contract §1.1, §1.1.1, §3.5, §4.0, §4.1 rules 1-5, §5.1, §5.9, §5.11; Research D4, D13, D15, §4.1.2; Data Model I18, I21-I23; Tasks T033, T038, T041-T042, T045]
- [x] CHK036 Are identity, time, trigger, and exclusion requirements complete: required trustworthy channel/time fields, keying by broadcaster, exactly three trigger categories, all other categories excluded, diagnostic-only `viewer_count`, and no producer-computed deadline/window? [Completeness, Spec §FR-003-§FR-006, §FR-017; Contract §1-§3; Research D7-D9]

## Dependencies and Assumptions - Release and Scope Gates

- [x] CHK037 Is forward deployment ordering consistent about reducing the ramp on the current single-subscription revision, then deploying dual coverage with gating off and the retention threshold still 400, then E1 and E2a, then the account-wide foreign-subscription sweep, then the ramp to the checked-in 400/450 and E2b, and about when E1 may block progression? [Conflict resolved, Plan §Rollout and rollback; Quickstart §B0-§B4b; Research §8 R6, R9, R15, §9 E1, E2a, E2b, D12; Autonomous Decisions §16, §21, §27]
- [x] CHK038 Is rollback ordering capacity-safe and unambiguous about the retention threshold value in force before auxiliary subscriptions are unwound — lowered to 400 and reconverged on a capacity incident, never above 450 while dual coverage is live, and back at 400 before the transport is unwound? [Conflict resolved, Plan §Rollback order, capacity-safe by construction; Autonomous Decisions §15 (superseded), §21, §27; Quickstart §B7; Research R11a; Tasks T065]
- [x] CHK039 Are local/offline claims sharply separated from deployed E1-E5 evidence, with each deployed gate, actor, order, and prohibition on satisfying it from fixtures, replay, static assertions, or reasoning stated explicitly, is exact-capacity evidence (E2b) required to be read as relational equality rather than an approximate subscription total, and is conditional PyFlink evidence scoped so it is never treated as covered by the always-run pure suites? [Assumption, Quickstart §A3, §A6, §Part B, §Evidence status; Research §9; Tasks T038, T047, T056, T067, §Deferred Deployed Evidence]
- [x] CHK040 Are scope boundaries consistent across artifacts: no second application identity, token reseed or scope expansion, dependency or persistence migration, separate raid subscription, partial rollout, viewer-derived duration, chat-schema change, ranking-policy change, heartbeat or synthetic-record protocol, hidden code-level hysteresis band, pair compaction/migration between connections, monitored maximum above 450 or fourth connection, or weakening of acceptance due to local constraints? [Consistency, Spec §FR-004, §FR-016, §NFR-007, §Out of Scope; Plan §Technical Context, §Deployment wiring, §Complexity Tracking; Tasks T054, §Notes]

## Notes

- Treat every unchecked item and its `Gap` note as unresolved until the owning
  requirement artifacts are amended and the author and merge reviewer agree
  the question can be checked.
- Checked items assess only the current writing; they make no claim about
  implementation or deployed behavior. In particular, CHK008 now assesses that
  the churn signal's *advisory* disposition is specified; CHK003-CHK007 assess
  that the amended capacity model and its exact-capacity engineering are
  *specified*, not that any of it is implemented. E1, E2a, E2b, E3, E4, and E5
  all remain pending deployed evidence, and amendment tasks T059-T068 remain
  unchecked.
- Remediation pass, 2026-09-04: CHK008, CHK018, CHK033, CHK035, CHK037, and
  CHK038 moved from unresolved to checked after their owning artifacts were
  amended — respectively by NFR-007/SC-011, the bounded auxiliary-refusal
  exception in FR-001/NFR-003/SC-001, the three-state delivery classification in
  NFR-005/SC-010, the producer-side key/payload invariant with payload-only
  consumer validation, the separation of the preliminary ramp-down from E1's
  gate, and the capacity-safe rollback order. CHK017, CHK019, CHK021, CHK023,
  CHK031, CHK032, CHK039, and CHK040 were strengthened in the same pass;
  CHK019 now also owns the idle-to-active watermark re-entry question. All 40
  items pass.
- Implementation-review correction, 2026-09-04: CHK020, CHK023, CHK024,
  CHK031, CHK033, and CHK035 were strengthened for the notice-bounded
  half-open interval and fixed future-time trust boundary. The checklist
  remains exactly 40 checked items; this records artifact quality only and
  does not complete any pending Flink task or deployed evidence E1-E5.
- Final code-review correction, 2026-09-04: CHK020, CHK031, and CHK035 were
  strengthened so the same fixed trust boundary protects source watermark
  assignment before the unchanged original payload is rejected downstream.
  The checklist remains exactly 40/40 checked; T058 and deployed evidence
  E1-E5 remain pending.
- Final code-review correction, 2026-09-05: CHK019, CHK020, CHK021, and CHK035
  were strengthened for autonomous decision 25 — the Python timestamp assigners
  must be attached with `assign_timestamps_and_watermarks()` after
  `from_source` on both the chat and suppression streams, and idleness is now
  generated per assignment subtask under an explicit
  partitions = source parallelism = assignment parallelism = 4 one-to-one
  invariant. The checklist remains exactly 40/40 checked; this records artifact
  quality only. No Flink task is completed by it, the amended T038/T041/T045/
  T050 keep their existing marks with the outstanding work carried by T058, and
  deployed evidence E1-E5 remains pending.
- Final code-review hardening, 2026-09-05: the same existing items now capture
  decision 26's bounded → idleness → assigner-last binding invariant, strict
  plain-int/non-bool chat timestamp rule, symmetric +30-second source bound,
  chat no-data-loss fallback, and deployed-only process/RSS evidence for the
  two parallelism-four Python stages. The checklist remains exactly 40/40;
  T038/T041/T045 retain their existing marks, T058 remains open, and E1-E5
  remain pending.
- Approved capacity amendment, 2026-09-05: CHK003, CHK004, CHK005, CHK006,
  CHK007, CHK008, CHK018, CHK032, CHK037, CHK038, CHK039, and CHK040 were
  rewritten for autonomous decisions 27 and 28 — entry 400 with
  retention-and-maximum 450, exact 900-of-900 subscription capacity with no
  guaranteed reserve, co-location-first placement with an atomic
  one-slot-per-connection split fallback, a distinct capacity classification
  with no transient growth backoff, `full_at` cleared/re-evaluated with
  below-cap full state exposed, the removal of the numeric churn release gate,
  the staged rollout through E2a and E2b, and the amended rollback order. The
  checklist remains exactly **40/40 checked**: each item was re-read against
  the rewritten spec, plan, research, data-model, quickstart, decision log, and
  runbook, and the retired 400/400 and 100-slot-reserve numbers no longer
  appear as active requirements anywhere in them. This records requirements
  quality only. It completes no task: T059-T068 are unchecked, T058 remains
  open, and deployed evidence E1, E2a, E2b, E3, E4, and E5 remains pending.

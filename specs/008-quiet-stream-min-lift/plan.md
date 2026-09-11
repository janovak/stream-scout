# Quiet-Stream Minimum-Lift Plan

**Branch**: `008-quiet-stream-min-lift`
**Decision**: Lightweight plan; a full feature specification is not warranted.

## Why this is a lightweight change

The defect is confined to one predicate in the existing pure detector. It
adds no API, persistent data, Flink state, dependency, topic, or user-facing
surface. The production risk is still meaningful because the predicate decides
whether to create a clip, so configuration, observability, rollout, and rollback
are part of the implementation.

## Problem

The detector currently opens an elevated period when:

```text
(window_mean - baseline_mean) / baseline_std >= k
```

A quiet or nearly metronomic channel can have a very small positive
`baseline_std`. One message then produces an outsized score, and a two-message
window can clear the shipped `k=4.0` threshold despite representing too little
absolute activity to justify a clip. A zero standard deviation is already
unmeasurable; this change covers small positive deviations.

## Decision

Keep the existing z-score trigger and require a minimum absolute increase over
the baseline expectation:

```text
expected_window_messages = baseline_mean * window_seconds
excess_messages = window_message_count - expected_window_messages

elevated =
    intensity >= k
    and (
        minimum-lift gating is disabled
        or excess_messages >= min_excess_messages
    )
```

Use `min_excess_messages=2.0`. The inclusive boundary means a window must
contain at least two messages more than its baseline expectation. Under the
shipped geometry, a near-silent two-message window is just below that boundary
because its baseline expectation is positive, while three messages clear it.

The intensity calculation and stored clip intensity remain unchanged. The
minimum lift is a second eligibility gate, not a replacement score.

## Implementation

1. Add `min_excess_messages` and `min_excess_gating_enabled` to
   `DetectorConfig`, including strict environment parsing and startup
   validation.
2. Apply the minimum-lift predicate in `spike_detector.evaluate()` without
   changing bucket ranges, baseline statistics, cooldown, hold, peak selection,
   or suppression behavior.
3. Report a diagnostic whenever a per-second reading passes the z-score gate
   but fails minimum lift.
4. Expose the diagnostic through
   `anomaly_min_lift_candidates_total{broadcaster_id,mode}`, where `mode` is
   bounded to `shadow` or `enforced`, and an INFO log containing the count,
   expected count, excess, intensity, configured threshold, event-time second,
   and whether the candidate would open a new hold under the shadow policy.
5. Add the threshold to both Flink compose blocks with enforcement enabled,
   matching the code default. Setting the gate to `false` remains available
   for optional shadow validation and rollback.
6. Log the submission-side settings at job startup. The runbook verifies the
   TaskManager environment separately because the worker re-reads
   `DetectorConfig` in `AnomalyDetector.open()`.
7. Update `tools/measure_corpus.py` and `tools/analyze_corpus.py` so corpus
   reconstruction models the shipped minimum-lift policy. Passing
   `--min-excess-messages 0` reproduces the pre-Feature-008 Plan 06 tables.
8. Document offline comparison, shadow rollout, enforcement, and rollback in
   `OPERATIONS.md`.

## Invariants

- A reading must still clear `DETECTION_STD_DEV_THRESHOLD`; minimum lift cannot
  create a new anomaly.
- A blocked reading does not open or extend a hold.
- If a blocked reading follows an open valid period, it ends that period under
  the existing hold behavior and reports the prior valid peak.
- `Spike.intensity`, emitted JSON, database rows, cooldown timing, and
  gift/raid suppression remain unchanged.
- Shadow mode changes no detector output or keyed state.
- The metric counts candidate **seconds**, not counterfactual clips. One chat
  event can remain in the five-second window for multiple evaluations.

## Automated validation

Add focused tests proving:

- a two-message near-silent window can pass 4 sigma under the shipped geometry
  but is blocked by the lift gate;
- the inclusive `2.0` boundary passes;
- a genuine multi-message spike still opens a hold;
- disabling the gate preserves the pre-change decision;
- a blocked reading correctly ends an existing valid hold;
- invalid, negative, non-finite, and non-boolean configuration fails at
  construction/startup;
- both Flink compose blocks carry identical checked-in values;
- the operator emits one attributable shadow/enforced metric and diagnostic
  log for each candidate second, including hold and cooldown context; and
- the corpus reconstruction and real replay agree with the same configured
  minimum lift.

Run the existing pure detector/replay tests and the conditional operator unit
tests. Do not start Kafka, Flink, Redis, Postgres, Twitch clients, or application
services locally.

## Production validation

### P0: Offline counterfactual

Before deploying, replay the available captured corpus twice with every
production detector setting held constant and only
`DETECTION_MIN_EXCESS_GATING_ENABLED` changed from `false` to `true`. Filter to
`SPIKE` lines and diff the sorted outputs. This gives the exact output delta,
including hold and cooldown interactions, that per-second shadow telemetry
cannot reconstruct.

Record every removed and added event with its broadcaster, peak time, count,
mean, and intensity. Inspect the corresponding clip or surrounding corpus
messages. Enforcement is blocked if it removes a desirable highlight or if an
added event cannot be explained by removing an earlier low-lift hold/cooldown.
The corpus characterizes the change; it does not predict current production
volume because the channel mix may be stale.

If the corpus is unavailable, record that fact. P1 is then the available
pre-deployment validation path: temporarily override the checked-in enabled
setting to collect live shadow evidence before restoring enforcement.

### P1: Optional shadow validation

The checked-in deployment skips this phase and starts with P2 enforcement.
When live shadow evidence is wanted before enforcement, temporarily override
both Flink environments with:

```text
DETECTION_MIN_EXCESS_MESSAGES=2.0
DETECTION_MIN_EXCESS_GATING_ENABLED=false
```

Inspect both containers' environments and require those exact values to agree.
The TaskManager value is authoritative because `AnomalyDetector.open()` reads
the worker environment; the JobManager startup log proves submission-side
configuration only. Existing clip decisions must remain unchanged.

Exclude the first five minutes after the force-recreate while baselines rebuild.
Then run an uninterrupted shadow soak of at least 24 hours covering a
representative peak period. Require all four `clip-detector` Prometheus targets
to be up and at least one candidate in either the offline replay or live Loki
logs. During a no-restart soak, use the raw counter:

```promql
sum by (broadcaster_id) (
  anomaly_min_lift_candidates_total{mode="shadow"}
)
```

Use each candidate log's `would_open` field to separate new-hold candidates
from readings that could only end an existing period or were already in
cooldown. The detector computes this after discarding an over-age hold and
with its own cooldown predicate. From shadow `ANOMALY DETECTED` logs, compute
`count - mean * 5`; the two-decimal logged mean introduces at most 0.025
message of error. Use a conservative computed boundary of `<=2.025` so
rounding cannot remove a real sub-2.0 case from manual review. Review the
resulting clips and proceed only when the live cases match the low-volume
behavior accepted in P0 and no desirable highlight is in the removed set.

Record the shadow-period anomaly and successful-clip counters by broadcaster
as diagnostic context for P2. They are not a fabricated percentage gate.

### P2: Deploy enforcement

The checked-in `DETECTION_MIN_EXCESS_GATING_ENABLED=true` ships this phase by
default. If P1 was selected, restore `true` in both Flink environments.
Force-recreate both Flink containers, require both environments to report
`true`, and treat the TaskManager value as authoritative. Exclude the first
five minutes while baselines rebuild, then verify:

- the metric advances under `mode="enforced"`;
- a candidate logged with `would_open=true` does not create an anomaly with
  that candidate's peak second;
- a candidate may end and emit a valid hold that opened earlier, as designed;
- active multi-message reactions still produce anomalies and clips;
- changed outputs retain the low-volume shape measured by P0/P1; and
- Flink restarts, errors, watermark health, and clip-creation failures remain
  at their pre-change levels.

Roll back immediately if manual review finds a desirable blocked highlight, if
the gate emits a candidate it should have blocked, or if detector health
regresses. Keep enforcement through at least one representative busy period
before calling the rollout complete.

### Rollback

Set `DETECTION_MIN_EXCESS_GATING_ENABLED=false` in both Flink environments and
force-recreate both Flink containers. Confirm both environments report `false`,
with the TaskManager value authoritative. Allow five minutes for baseline
rebuild before judging clip recovery; a later candidate must carry
`mode="shadow"`. No data, Kafka, or state rollback is required.

## Review

Review the final diff specifically for boundary arithmetic, shadow-mode state
equivalence, hold termination, configuration parity between JobManager and
TaskManager, metric cardinality, and consistency between code and the
production procedure. Resolve every high-confidence finding before handoff.

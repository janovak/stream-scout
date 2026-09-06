# Autonomous Decision Log: Suppress Gift and Raid Chat Bursts

This log records material choices considered during Feature 007. Decisions 1-4
were directly accepted before autonomous execution was enabled. Decision 5 was
selected autonomously after the user instructed the agent not to ask further
questions.

Decisions 17-21 were taken during a later remediation pass over the artifacts.
Decisions 22-23 are implementation-review corrections that clarify earlier
window and timestamp decisions before their code changes landed.
Decisions 24-25 are final code-review corrections to event-time handling,
taken before their code changes landed. Decision 26 is final code-review
hardening after making the Python assigners effective exposed the remaining
builder-order and chat-watermark risks.
Decisions 27-28 are the approved capacity amendment of 2026-09-05. The user
approved `JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=450` and asked that every
cascade from the earlier 400/400 model be revisited rather than patched.
Decision 27 records the capacity model itself; decision 28 records the
exact-capacity engineering without which that model must not be deployed.

Where a remediation decision replaces an earlier one, the earlier entry is
marked **superseded** and left in place with its original text rather than being
rewritten, so the reasoning that led to the replacement stays readable.

## 1. Suppression windows

**Question**: How long should gift and raid suppression last, and how should
those durations be determined?

| Option | Description |
|--------|-------------|
| A | Operator-configurable windows defaulting to 120 seconds for gifts and 180 seconds for raids, with no viewer-count scaling. |
| B | Fixed windows of 120 seconds for gifts and 180 seconds for raids. |
| C | Operator-configurable windows defaulting to 180 seconds for both gifts and raids. |
| D | Gift windows remain fixed while raid windows vary by viewer-count tier. |

**Selection**: Option A — directly accepted.

**Rationale**: Separate defaults reflect the expected burst lengths while
remaining tunable from operational evidence. Avoiding viewer scaling keeps
behavior deterministic and prevents audience size from becoming a policy
dependency.

**Stage**: Clarification.

**Impact**: Defines suppression-window requirements, overlap behavior, replay
expectations, and future configuration work.

## 2. Trigger notices and overlapping hype

**Question**: Which notification types should trigger suppression, and what
should happen when genuine hype overlaps a suppression window?

| Option | Description |
|--------|-------------|
| A | Trigger only on `community_sub_gift`, `sub_gift`, and `raid`; exclude `unraid`, plain `sub`, and `resub`; accept overlapping genuine hype as a false negative. |
| B | Use the same trigger set but attempt to preserve genuine hype that overlaps a suppression window. |
| C | Also trigger suppression for plain `sub` and `resub` notifications. |

**Selection**: Option A — directly accepted.

**Rationale**: The narrow trigger set targets the known low-value bursts.
Accepting overlap false negatives keeps suppression deterministic and avoids
adding a second, competing spike-classification policy.

**Stage**: Clarification.

**Impact**: Fixes the supported notice set, exclusion cases, overlap acceptance
tests, and feature scope.

## 3. Missing or delayed suppression signals

**Question**: How should clip detection behave when notification coverage is
missing or lagging, or suppression delivery is delayed?

| Option | Description |
|--------|-------------|
| A | Fail open, expose the degraded condition, and never retract an emitted clip. |
| B | Fail closed by blocking clip emission while suppression health is uncertain. |
| C | Wait up to 30 seconds for suppression data, then fail open. |

**Selection**: Option A — directly accepted.

**Rationale**: Fail-open behavior preserves existing clip availability and
avoids turning an auxiliary signal failure into a clipping outage. Explicit
degradation signals retain operator visibility without retroactive mutation.

**Stage**: Clarification.

**Impact**: Defines failure behavior, late-arrival semantics, observability,
and degraded-path acceptance criteria.

## 4. Suppressed-spike visibility

**Question**: What operator visibility should a suppressed
would-have-clipped spike produce?

| Option | Description |
|--------|-------------|
| A | Emit both an operator-visible metric and a structured log. |
| B | Emit an operator-visible metric only. |
| C | Emit a structured log only. |
| D | Emit no operator signal. |

**Selection**: Option A — directly accepted.

**Rationale**: A metric supports aggregate monitoring and tuning, while a
structured log supports channel-level diagnosis. Together they make
suppression measurable without creating a clip.

**Stage**: Clarification.

**Impact**: Adds per-decision observability requirements, acceptance criteria,
and operator-facing diagnostic expectations.

## 5. Monitored-set capacity

> **Superseded by decision 27.** The original text is preserved below exactly
> as written. What does not stand is option A's firm 400-channel maximum, its
> equal 400/400 thresholds, and the 100-subscription reserve it justified.
> Decision 27 replaces them with entry 400 / retention-and-maximum 450 and an
> exact 900-subscription ceiling with no guaranteed reserve. The reasoning that
> is *kept* is option A's refusal to redesign session capacity inside this
> feature: connections, per-session caps, and the 900 total are unchanged.

**Question**: What capacity ceiling and ramp thresholds should apply after
adding a second subscription per monitored channel?

| Option | Description |
|--------|-------------|
| A | Set a firm 400-channel maximum with `JOIN_THRESHOLD=400` and `LEAVE_THRESHOLD=400`, retaining the existing session-capacity assumptions. |
| B | Use `JOIN_THRESHOLD=350` and `LEAVE_THRESHOLD=400` within the same 400-channel ceiling. |
| C | Revisit connection and session assumptions before reducing monitored-set capacity. |

**Selection**: Option A — selected autonomously after the no-questions
instruction.

**Rationale**: This matches the roadmap's locked capacity decision, preserves
100 of the existing 900 subscription slots for reconnect and adoption safety,
and avoids expanding Feature 007 into a session-capacity redesign.

**Stage**: Clarification.

**Impact**: Sets the runtime ramp thresholds, capacity requirements, 401st
channel refusal behavior, operational guidance, and capacity-focused tests.

## 6. Where the two-subscriptions-per-channel model lives

**Question**: Which component owns the two-subscriptions-per-channel state
model — the EventSub pool, or the reconciler's desired/actual diff?

| Option | Description |
|--------|-------------|
| A | The pool owns it: one slot record per (channel, coverage type), a channel-level coverage view, and a `list()` that reports a channel only when both types are enabled. The reconciler stays channel-keyed and behaviorally unchanged. |
| B | The reconciler owns it: its desired and actual sets become keyed by (channel, type), and the transport interface becomes type-aware end to end. |
| C | A second transport instance dedicated to notifications, with its own connections. |

**Selection**: Option A — selected autonomously.

**Rationale**: The reconciler's channel-keyed diff carries rank ordering, the
per-channel refusal cache, the retry and concurrency budget, and the
channel-level count metric. Option B would rewrite all of that for no
behavioral gain and would make a per-type refusal indistinguishable from a
channel refusal. Option C would need more websocket connections than Twitch
permits for one client-id/user-id pair. Option A confines the change to the
component that already models sessions, slots, occupancy, and staleness.

**Stage**: Planning.

**Impact**: Sets the implementation boundary and the task split; keeps
`reconciler.py` off the change list; makes coverage a pool-owned derived state
with its own channel-counting metric alongside subscription-counting occupancy.

## 7. Auxiliary subscription refused while chat is live

> **Superseded by decision 17.** The original text is preserved below exactly
> as written. Option A's clause "stop retrying that type until re-adoption or
> restart" made the degraded state effectively permanent, which contradicts
> FR-001's dual-coverage requirement. Decision 17 keeps everything else about
> option A and replaces that clause with a bounded retry policy.

**Question**: What should happen when Twitch refuses the
`channel.chat.notification` subscription for one channel while that channel's
chat subscription is live and healthy?

| Option | Description |
|--------|-------------|
| A | Record the channel as auxiliary-refused inside the pool, keep chat, report the channel as covered-but-degraded, and stop retrying that type until re-adoption or restart. |
| B | Propagate the refusal to the reconciler as an ordinary channel refusal. |
| C | Retry the auxiliary subscription on every reconcile pass indefinitely. |

**Selection**: Option A — selected autonomously.

**Rationale**: Option B would write the channel into the durable refusal cache
and skip it for the seven-day recheck window, so an auxiliary-signal refusal
would remove that channel's chat ingestion entirely — data loss caused by a
feature that is only supposed to gate emission. Option C spends create budget
and log volume every pass, forever, at 400 channels. Option A keeps the chat
pipeline intact, keeps the degraded state operationally visible, and matches
the specified fail-open behavior.

**Stage**: Planning.

**Impact**: Adds a degraded coverage state and its metric, changes error
classification in the pool, and adds an explicit acceptance test that a
notification refusal never evicts chat coverage.

## 8. How suppression reaches the detector

**Question**: What transport and operator shape should carry suppression from
the monitoring service to the clip detector?

| Option | Description |
|--------|-------------|
| A | A dedicated versioned Kafka topic keyed by broadcaster id, consumed by a second Kafka source connected to the keyed chat stream through a keyed two-input process function. |
| B | The same dedicated topic, but broadcast to every subtask through broadcast state. |
| C | Sentinel records embedded in the existing chat topic. |
| D | The detector reads suppression from Postgres or Redis at decision time. |

**Selection**: Option A — selected autonomously.

**Rationale**: The signal is per channel, so keying it the same way the chat
stream is keyed gives channel isolation structurally rather than by convention.
Broadcast state fans per-channel data to every subtask and abandons keyed
state. Sentinel records would break the frozen chat schema contract and route a
notice through the command filter and chat mapper. An external lookup would add
I/O to a path that runs once per second per broadcaster and would contradict
the requirement that services communicate over Kafka.

**Stage**: Planning.

**Impact**: Defines the new topic, its keying and partitioning, the contract
file, and the conversion of the detector operator into a keyed two-input
function with a second source.

## 9. Watermarks and offsets for the suppression input

**Question**: How should the suppression stream participate in event time,
given that a two-input operator's watermark is the minimum of its inputs and
this stream is silent for hours?

| Option | Description |
|--------|-------------|
| A | Real event-time watermarks from the notice occurrence time, bounded out-of-orderness matching the chat stream, an idleness timeout strictly shorter than the chat stream's, latest starting offsets, and partition count equal to job parallelism. |
| B | No watermarks on the suppression source. |
| C | Earliest starting offsets so a restart replays recent notices. |
| D | Process-time semantics for the suppression side only. |

**Selection**: Option A — selected autonomously.

**Rationale**: Option B never advances the minimum, which freezes the operator
watermark and stops per-second evaluation for every channel — an outage far
worse than the false positives being removed. Option C feeds hours-old
timestamps into the same minimum at startup and pins event time in the past
until the backlog drains. Option D would mix two clocks in one comparison.
Option A keeps the suppression input from ever being the binding minimum in
steady state and keeps both sides on Twitch's clock.

**Stage**: Planning.

**Impact**: Fixes the source configuration, the topic partition count, a new
idleness constant, restart behavior (state starts empty and fails open), and
the deployed watermark-stall check that gates enabling gating in production.

## 10. Where the suppression gate sits, and what it may change

**Question**: Should suppression be applied inside the detector's evaluation
arithmetic, or as a filter on its output, and may it change cooldown state?

| Option | Description |
|--------|-------------|
| A | An output-only filter after evaluation and after every keyed state write, including the last-fire cooldown update. |
| B | Applied inside the evaluation function, so a suppressed period never opens or closes a hold and never records a fire. |
| C | An output-only filter, but skipping the last-fire cooldown update so the cooldown only starts on real clips. |

**Selection**: Option A — selected autonomously.

**Rationale**: The specification requires that only clip emission and the two
required operator signals differ from an ungated run, and that all
message-derived state be identical. Option B changes hold and expiry
trajectories and would break both that guarantee and the existing pure-module
test suite. Option C diverges from the ungated run for the length of the
cooldown after every suppressed decision, which makes the identical-state
success criterion unprovable. Option A costs one thing — a suppressed decision
starts the cooldown as if a clip had been created — and that window lies inside
a longer suppression window where no clip could have been emitted anyway.

**Stage**: Planning.

**Impact**: Defines the exact gate location, keeps the pure evaluation function
unchanged, and makes the gated-versus-ungated replay equivalence the decisive
offline acceptance evidence.

## 11. Which moment of a spike is tested against the deadline

**Question**: When a decision is reported, is the suppression deadline compared
against the peak second of the spike or the second the decision was reported?

| Option | Description |
|--------|-------------|
| A | The peak second of the reported spike. |
| B | The report second. |
| C | Suppress if either falls inside the window. |

**Selection**: Option A — selected autonomously.

**Rationale**: The peak is the moment chat actually burst, it is the timestamp
recorded with the clip, and it is what "a spike inside the window" means. Under
option B a burst that peaks inside the window but is reported after the
peak-hold cap escapes suppression, which is the exact failure this feature
exists to prevent. Option C additionally suppresses spikes that peaked before
the notice occurred, which no requirement asks for.

**Stage**: Planning.

**Impact**: Fixes the gate predicate, makes suppression independent of
peak-hold timing, and defines the boundary cases the detector tests must cover.

## 12. Where suppression window durations are applied

**Question**: Should the producer compute and publish the suppression deadline,
or should it publish the raw notice and let the consumer apply the window?

| Option | Description |
|--------|-------------|
| A | The producer publishes notice category and occurrence time only; the consumer applies the operator-configured window. |
| B | The producer computes the deadline and publishes it. |

**Selection**: Option A — selected autonomously.

**Rationale**: Option B bakes policy into retained records, so a window change
would leave records on the topic that mean something the configuration no
longer says, and it would split the window constants across two services with
independent deploys. Option A keeps the topic a record of what happened rather
than of what was decided, so retuning is a consumer configuration change and
replay stays meaningful.

**Stage**: Planning.

**Impact**: Determines the contract's field set, places the window
configuration on the detector service, and keeps the topic policy-free and
replay-safe.

## 13. Malformed notices at the producer

**Question**: What should the producer do with a notice that lacks a
trustworthy channel identity or occurrence time?

| Option | Description |
|--------|-------------|
| A | Publish nothing, increment a malformed counter, and log; never substitute another clock. |
| B | Publish with a null occurrence time and let the consumer decide. |
| C | Substitute the ingestion clock for the missing occurrence time. |

**Selection**: Option A — selected autonomously.

**Rationale**: The requirement is explicit that a malformed input must not
create a guessed deadline and must be operationally visible. Options B and C
both end in a fabricated deadline that would suppress real clips. This is a
deliberate asymmetry with the chat path, which publishes a message with a null
send time rather than dropping it: dropping chat would be data loss, whereas
dropping one auxiliary notice degrades to the fail-open behavior the system is
already designed to survive.

**Stage**: Planning.

**Impact**: Sets the producer validation rules in the contract, adds the
malformed-input counter and log required for operational visibility, and
documents why the two paths differ.

## 14. Zero-width hysteresis band at the locked 400/400 thresholds

> **Superseded by decision 27.** The original text is preserved below exactly
> as written. Its entire premise — that join and leave are the same number, so
> no hysteresis band exists — is removed by decision 27's entry 400 /
> retention 450 split, which restores a 50-channel band. Options B and C are
> still rejected for the reason given here: policy must live in configuration
> operators read, never in hidden code behaviour. The churn signal option A
> introduced is kept, but as advisory telemetry rather than as the measurement
> that justifies a zero-width band.

**Question**: The locked capacity decision sets join and leave thresholds to
the same value, which removes the hysteresis band that stops a boundary-rank
channel from leaving and rejoining each poll. How should that be handled?

| Option | Description |
|--------|-------------|
| A | Implement 400/400 exactly as locked, add an explicit monitored-set churn signal so the cost is measured rather than assumed, and record a narrower entry threshold inside the same ceiling as a follow-up that would require a specification change. |
| B | Quietly implement a narrower entry threshold, for example 380/400, to restore a band. |
| C | Special-case hysteresis in code so the band exists regardless of configuration. |

**Selection**: Option A — selected autonomously.

**Rationale**: Options B and C contradict a locked, explicitly clarified
requirement and hide policy from the configuration operators read. The churn
cost is real but bounded — it affects only channels oscillating across the
ceiling rank, and each affected channel simply re-warms its baseline — so
measuring it is a better basis for any future change than pre-emptively
deviating from the specification.

**Stage**: Planning.

**Impact**: Keeps the locked thresholds intact, adds a churn metric and a ramp
observation step, and records the follow-up option without acting on it.

## 15. Rollback lever for emission gating

> **Partially superseded by decision 21.** The original text is preserved below
> exactly as written. The kill switch itself stands. What does not stand is the
> ordering clause "restores the safe ramp before unwinding auxiliary
> subscriptions" and its rationale sentence "Unwinding capacity before
> thresholds would leave the monitored set larger than the capacity model in
> force" — that order is the unsafe one while two subscriptions per channel are
> still live. Decision 21 replaces the ordering and adds the checked-in default
> for the switch.

**Question**: How should an operator disable suppression gating if it misbehaves
in production?

| Option | Description |
|--------|-------------|
| A | An operator-settable switch that disables gating while leaving both subscriptions and the topic in place, plus a documented rollback order that restores the safe ramp before unwinding auxiliary subscriptions. |
| B | Revert the code revision and redeploy. |

**Selection**: Option A — selected autonomously.

**Rationale**: The failure this protects against is clips silently not being
created, and the correct first response is to stop gating within one restart of
one container rather than to run a code rollback under pressure. Keeping the
subscriptions and topic in place while gating is off also preserves the
evidence needed to diagnose the problem. Unwinding capacity before thresholds
would leave the monitored set larger than the capacity model in force, so the
order is fixed rather than left to judgement.

**Stage**: Planning.

**Impact**: Adds one detector configuration switch, defines the deployment
sequence (deploy with gating off, verify coverage and watermarks, then enable),
and fixes the rollback ordering documented for operations.

## 16. Deployment ordering of the capacity reduction

> **Amended by decision 27.** The original text is preserved below exactly as
> written and its selection still stands: the Redis-resident desired set means
> the ramp reduction must converge on the current single-subscription revision
> **before** the two-subscription transport ships. Decision 27 adds a third
> stage after those two. The dual transport is deployed while the retention
> threshold is still **400**, and only after E1 and E2a — and the account-wide
> foreign-subscription sweep — does the retention threshold ramp to **450**.
> The same reasoning drives both: never let the transport observe a desired set
> larger than the capacity model already proven in force.

**Question**: Should the monitored-set reduction to 400 and the
two-subscriptions-per-channel transport be deployed together, or separately?

| Option | Description |
|--------|-------------|
| A | Two steps: reduce the ramp to 400 on the current single-subscription revision, let the monitored set converge, and only then deploy the two-subscription revision. |
| B | One step: deploy the threshold change and the transport change together. |

**Selection**: Option A — selected autonomously.

**Rationale**: The desired set is stored in Redis and survives a restart. On
deploy, the reconciler converges to the set that is already there, while the
poller only rewrites it on its next scheduled tick up to two minutes later.
Under option B the new transport would therefore begin creating two
subscriptions for each of roughly eight hundred previously monitored channels,
against a nine-hundred-subscription ceiling, before the smaller set was
written. Option A makes that impossible: reducing the ceiling on the old code
is always within capacity, and the new transport never observes a set larger
than its capacity model permits.

**Stage**: Planning.

**Impact**: Fixes the deployed validation sequence, adds an explicit
pre-deployment convergence step to the validation procedure, and records the
Redis-resident desired set as the reason the two changes cannot be collapsed
into one deployment.

## 17. Recovery policy for a refused auxiliary subscription

**Question**: Decision 7 kept chat and failed open when Twitch refuses the
`channel.chat.notification` subscription for one channel, but stopped retrying
that type "until re-adoption or restart", which makes the degraded state
effectively permanent and quietly contradicts FR-001. What recovery policy
should replace that clause?

| Option | Description |
|--------|-------------|
| A | Keep the pool-local degraded state permanent, as decision 7 wrote it. |
| B | Bounded retry: suppress repeated notification creates for `AUXILIARY_REFUSAL_RETRY_SECONDS` = 3600 s after a refusal, then make the channel repairable again; force eligibility immediately on websocket reconnect or connection retirement; clear the state on successful creation or adoption. During the hold-off the channel is reported as actual-but-degraded so the reconciler does not hot-loop; after expiry it is partial again and normal create repair resumes. |
| C | Exponential backoff from seconds to hours, with no ceiling. |
| D | Retry every reconcile pass, as decision 7's rejected option C. |

**Selection**: Option B — selected autonomously.

**Rationale**: Option A trades one coverage hole for another: a transient
refusal — a momentary 403, a session-scoped failure, a Twitch-side blip —
becomes a permanent hole that only a container restart repairs, and the
artifacts would then be requiring universal dual coverage while the design
guaranteed a class of channels never regained it. Option D is what decision 7
correctly rejected: at 400 channels it spends create budget and log volume every
pass forever. Option C adds state and tuning surface to solve a problem a flat
hour already solves, and an unbounded ceiling recreates option A's failure at
the tail. Option B keeps every property decision 7 was protecting — chat is
never evicted, no seven-day channel refusal is written, the reconciler does not
hot-loop — and adds only the property it was missing: the state expires. One
hour is short enough that a transient refusal costs at most one hour of
suppression coverage on one channel, and long enough that a genuinely permanent
refusal costs one create attempt per hour rather than one per pass.

**Stage**: Remediation.

**Impact**: Amends FR-001, NFR-003, and SC-001 to state the bounded exception
explicitly; adds `AUXILIARY_REFUSAL_RETRY_SECONDS` (default 3600) to the
`stream-monitoring` configuration; turns `auxiliary_refused` into a
`auxiliary_refused_until_ms` deadline in the coverage model with data-model
invariant I17; folds the expiry, reconnect, and adoption cases into the existing
T016 tests and T024 implementation without adding a task.

## 18. Watermark cost of a sparse source becoming active again

**Question**: Idleness keeps a silent suppression subtask out of the two-input
watermark minimum, but the subtask re-enters that minimum the moment it emits a
record. On a source that is silent for hours and then delivers one isolated
notice, how should the resulting watermark hold be handled?

| Option | Description |
|--------|-------------|
| A | Document and accept it, with a conservative upper bound of `SUPPRESSION_IDLENESS_SECONDS + WATERMARK_OUT_OF_ORDERNESS_SECONDS` before the subtask goes idle again and releases the minimum; test the bound offline in the replay harness's simplified model and measure it deployed as part of E3. |
| B | Emit heartbeat or synthetic records on the topic so the source is never idle. |
| C | Give the suppression side process-time or ingestion-time watermarks so it can never hold event time back. |
| D | Set `SUPPRESSION_IDLENESS_SECONDS` to a very small value so the hold is negligible. |

**Selection**: Option A — selected autonomously.

**Rationale**: The hold is real, bounded, and short: at most the idleness
timeout plus the out-of-orderness bound, after which the subtask is idle again.
That is one to two orders of magnitude smaller than the shortest suppression
window it protects, and it delays per-second evaluation rather than stopping it.
Option B introduces exactly the heartbeat protocol the feature's scope forbids,
with its own failure modes, purely to avoid a delay measured in seconds.
Option C is the two-clocks mistake decision 9 already rejected — it would make
`suppress_from_ms <= peak_second * 1000 < suppress_until_ms` a comparison
between different clocks.
Option D trades the hold for flapping: a very short idleness makes the source
oscillate between idle and active around every record, and it narrows the margin
below the chat stream's 10 s that keeps suppression from being the binding
minimum in the first place. Accepting a bound and proving it is a better answer
than engineering around a cost that is smaller than the thing it protects.

**Stage**: Remediation.

**Impact**: Adds research §4.1.1 and risk R10, data-model invariant I16, and the
offline simplified-model case to the existing T050; makes deployed E3 require
**both** prolonged silence and an isolated notice after silence, so the
re-entry case is measured rather than assumed.

## 19. Release disposition for 400/400 desired-set churn

> **Superseded by decision 27.** The original text is preserved below exactly
> as written. Option B's numeric release gate — 8 membership changes per poll
> averaged over a 24-hour deployed observation, blocking gating when exceeded —
> is **removed**, together with the 24-hour wait it imposed on the rollout. It
> existed to make a zero-width band falsifiable, and decision 27 removes the
> zero-width band instead. What is kept is option D's rejection: the service
> must never widen or narrow the band on its own, and any future change to the
> configured thresholds is a specification change made in the open.
> `desired_set_churn_total` remains, as advisory bounded-label telemetry with
> no numeric release gate attached to it.

**Question**: Decision 14 accepted the zero-width hysteresis band and added a
churn signal so the cost would be measured rather than assumed, but never said
what measurement would be unacceptable. What disposition applies?

| Option | Description |
|--------|-------------|
| A | Keep the signal with no threshold — observe and judge case by case. |
| B | Bound it: entries plus departures attributable to the band, averaged per poll across a 24-hour deployed observation, must not exceed 2% of the 400-channel ceiling (8 membership changes per poll). Exceeding the bound blocks enabling gating and is resolved by a specification change to a narrower join threshold inside the firm 400 ceiling — never by hidden code behaviour. |
| C | Bound it tightly, at well under 1% per poll. |
| D | Have the service widen the band automatically when observed churn is high. |

**Selection**: Option B — selected autonomously.

**Rationale**: Option A leaves the acceptance unfalsifiable; a signal nobody can
fail is documentation, not a gate, and the entire reason decision 14 accepted
the locked thresholds was that the cost would be *measured*. Option D is the
hidden-policy option decision 14 already rejected, in a more sophisticated
disguise: operators would read 400/400 in the configuration and the service
would be enforcing something else. Option C would fail for reasons unrelated to
this feature — boundary-rank movement in the ranking is normal, and a bound that
trips on ordinary churn would block a good release. Two percent of the ceiling
is a rate at which at most eight channels re-warm their baselines per poll,
which is absorbable against 400 monitored channels and still small enough that
crossing it indicates genuine thrash. Routing the failure to a specification
change keeps the locked decision and the configuration honest: if the band must
narrow, that is a product decision, made in the open.

**Stage**: Remediation.

**Impact**: Adds NFR-007 and SC-011 to the specification; adds the 24-hour
observation and its bound to deployed evidence E2 and quickstart B4; extends
T027/T028 to pin the churn accounting locally while leaving the rate itself
explicitly deployed; adds data-model invariant I20 and research decision D14.

## 20. What "lagging suppression delivery" means when the topic is legitimately silent

**Question**: NFR-005 requires lagging delivery to be distinguishable from
healthy delivery, but the suppression topic is silent for hours in normal
operation, and silence is what both a healthy system and a stalled broker path
look like. How is delivery health defined without adding a heartbeat?

| Option | Description |
|--------|-------------|
| A | Treat silence as healthy. |
| B | Treat silence as lagging. |
| C | Define three states from received records using `delivery_age_ms = max(0, consumer_receipt_ms - occurred_at_ms)`, with `consumer_receipt_ms` captured from an injected/current consumer clock at `process_element2` receipt. Against `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS` = 30, age at or below is healthy, above is lagging, and a window with no record is idle/unknown — neither healthy nor lagging. Observe the clamped value in `suppression_delivery_age_seconds`; structured-log negative raw age as clock skew and lagging records as lagging. Keep optional `received_at_ms` diagnostic-only, usable to split Twitch-to-producer from producer-to-consumer latency but never for classification. State plainly that the feature does not claim to detect a stalled delivery path during a period with no real notices. |
| D | Publish a continuously refreshed per-channel delivery-age gauge from `on_timer` so there is always a current value. |

**Selection**: Option C — selected autonomously.

**Rationale**: Options A and B are both false statements about the same
observation: A makes a stalled broker path invisible for as long as it lasts,
and B pages on a perfectly healthy system every quiet hour, which trains
operators to ignore the signal. Option D is the subtler version of A — it must
publish *some* number every second for every key, and during legitimate silence
whatever it publishes is invented, which is exactly the fabrication FR-017 and
decision 13 refuse on the producer side. Option C is the only one that reports
what is actually known: records that arrived, and their age from Twitch
occurrence to actual consumer receipt. Using the consumer receipt clock ensures
consumer-side delay is included even when producer delay was small; optional
`received_at_ms` can explain the components but cannot change the result.
Clamping negative raw age prevents clock skew from creating negative
observations while retaining a structured diagnostic. Silence produces no
samples, and "no samples" is a distinguishable, honest third state. Coverage
status answers the separate question of whether the subscriptions exist, and the
two are read together rather than substituted — complete coverage plus silence
proves nothing about the broker path, and the artifacts now say so instead of
implying otherwise. The residual limitation is stated rather than hidden: an
undetected stall during a genuinely quiet period degrades to the pre-007
behaviour, which is the floor this fail-open design already accepts.

**Stage**: Remediation.

**Impact**: Amends NFR-005 and SC-010; adds `SUPPRESSION_DELIVERY_LAG_WARN_SECONDS`
= 30 to both Flink blocks; replaces the delivery-age gauge with
`suppression_records_consumed_total{lag_class}` plus the per-received-record
`suppression_delivery_age_seconds` observation; adds data-model invariant I19,
contract §4.1 rule 7, and research §4.6/D13; folds the work into T039, T042,
T046, and T053.

## 21. Capacity-safe rollback order and the checked-in gating value

> **Amended by decision 27.** The original text is preserved below exactly as
> written. Both of its principles stand: `SUPPRESSION_GATING_ENABLED=false` is
> the first and often the only rollback step, and capacity is unwound *before*
> the threshold is relaxed, never after. Only the numbers move. With entry 400 /
> retention 450, the sequence gains a step in front of option B's step (b): on a
> capacity incident, lower `LEAVE_THRESHOLD` to **400** first and let the set
> reconverge, which is what returns the deployment to the 800-of-900 shape that
> the rest of the order already assumes. The invariant is restated the same way
> it was derived: the retention threshold is never above **450** while two
> subscriptions per channel are live, and it must be back at **400** before the
> dual transport is unwound. The checked-in `SUPPRESSION_GATING_ENABLED=false`
> and the code-level `true` default are unchanged.

**Question**: Decision 15 fixed the rollback order as "gating off, restore the
safe ramp, then unwind the auxiliary subscriptions". With two subscriptions per
channel still live, raising thresholds first permits more than 800 subscriptions
against a 900 ceiling. What order is actually safe, and what value of the gating
switch should be checked into the repository?

| Option | Description |
|--------|-------------|
| A | Keep decision 15's order and rely on operator judgement about which threshold value is "known good". |
| B | Invert it, and make the invariant explicit: (a) `SUPPRESSION_GATING_ENABLED=false`; (b) while thresholds remain 400/400, revert or unwind the dual-subscription transport; (c) wait until notification subscriptions are gone, total subscription count is approximately the desired channel count (~400), and coverage/desired metrics are stable; (d) only then raise thresholds back toward the single-subscription ramp. Thresholds are **never** raised above 400 while any `channel.chat.notification` subscription remains. Separately, check `SUPPRESSION_GATING_ENABLED=false` into `docker-compose.yml` on both Flink blocks while the code-level `SuppressionConfig` default stays `true`. |
| C | Invert the order but leave the checked-in gating value `true`, matching the code default. |
| D | Change the code default to `false` as well. |

**Selection**: Option B — selected autonomously.

**Rationale**: The ordering is not a matter of taste. At two subscriptions per
channel, any threshold above 400 authorises more than 800 subscriptions, so
raising thresholds while notification subscriptions are still live is precisely
the state that overruns the 900 ceiling — decision 15 had the hazard right and
the direction backwards. Unwinding capacity first is safe at every instant,
because 400 channels on the single-subscription revision need 400 of 900 slots,
and the wait in step (c) is what makes "the transport is gone" an observation
rather than an assumption. On the switch: the code default of `true` is correct
for the module, because a detector that silently ignores its suppression input
by default would be a worse trap than one that gates. But a deploy must not
start gating before any deployed evidence exists, and the file operators
actually deploy is `docker-compose.yml`. Checking `false` in there makes the
deploy inert by construction, and enabling becomes a deliberate, reviewable edit
taken after E1-E3 and the 24-hour churn observation. Option C leaves that gap
open; option D hides the module's intended behaviour behind a default nobody
reads.

**Stage**: Remediation.

**Impact**: Rewrites the rollback order in the plan, the quickstart rehearsal
(B7), and the operations runbook task; adds risk R11 and amends research D11/D12;
adds the checked-in `SUPPRESSION_GATING_ENABLED=false` to T046 and the
enablement precondition to the rollout; clarifies that the preliminary 400/400
ramp-down on the single-subscription revision is unconditionally capacity-safe
and is not blocked by E1, which gates dual-coverage sign-off and enabling gating
instead.

## 22. Notice-bounded half-open suppression windows

**Question**: Which instants belong to a suppression window, especially when a
spike peaks before a notice but its hold reports after the notice arrives?

| Option | Description |
|--------|-------------|
| A | Deadline-only: suppress every peak before `suppress_until_ms`, regardless of whether it predates the notice. |
| B | Notice-bounded half-open: suppress exactly when `suppress_from_ms <= peak_ms < suppress_until_ms`; retain the earliest start of an extending overlapping chain, leave complete state unchanged for an earlier/equal candidate deadline, and start a new interval when a notice occurs at or after the old deadline. |
| C | Report-time: decide from the second the held spike is reported rather than from its peak. |

**Selection**: Option B — implementation review correction.

**Rationale**: Option A contradicts the reason research D5 selected peak time:
it still suppresses genuine activity that happened before the notice merely
because peak-hold delayed the report. Option C has the opposite defect: a peak
inside the burst can escape after the hold cap. A notice-bounded half-open
interval models the causal claim precisely. The notice instant is included,
the deadline is excluded, and an exact-deadline notice starts a new interval.
Keeping an earlier/equal candidate as a complete-state no-op preserves
idempotence and avoids diagnostics or lower bounds changing when the deadline
does not. This means only the maximum deadline, not the entire interval state,
is order-independent; the earlier blanket claim is corrected explicitly.

**Stage**: Implementation review correction.

**Impact**: Clarifies and corrects decision 11 and research D5 rather than
rewriting their history; adds `suppress_from_ms` to `SuppressionState`; updates
FR-006, FR-007, SC-003, SC-005, the gate predicate, overlap transition,
invariants, replay expectations, and existing tasks T030-T032, T040, T044,
T049, and T050. Genuine hype is an accepted false negative only when its peak
is inside the notice-bounded interval; a pre-notice peak remains eligible.

## 23. Fixed 30-second maximum future timestamp skew

**Question**: How much future clock skew may a decoded suppression record claim
before its occurrence time becomes untrustworthy?

| Option | Description |
|--------|-------------|
| A | Reject any `occurred_at_ms` later than the consumer receipt clock. |
| B | Allow a fixed 30-second skew: accept equality at `consumer_receipt_ms + 30_000`, clamp accepted negative age to zero with the existing clock-skew diagnostic, and reject one millisecond beyond as malformed fields before delivery observation or state access. |
| C | Apply no future bound and clamp every future timestamp to delivery age zero. |

**Selection**: Option B — implementation review correction.

**Rationale**: Option A turns ordinary cross-host clock skew into avoidable
fail-open misses. Option C lets a seconds-versus-milliseconds mistake or bad
clock install a far-future interval while also appearing as healthy age zero.
Thirty seconds matches the existing operational lag scale while strictly
bounding that failure. The bound is a defence-in-depth contract invariant, not
an operator tuning parameter, so it introduces no environment variable. The
ordering is part of the decision: decode and field types first, then future
trust, then and only then delivery observation and state.

**Stage**: Implementation review correction.

**Impact**: Clarifies decision 20 and research D13 rather than silently changing
their meaning; fixes `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30`; amends FR-017,
NFR-005, SC-010, the consumer contract, data-model invariants, risk analysis,
quickstart assertions, and existing tasks T029, T031, T037, T039, T041, T042,
T046, T047, and T050. Over-bound records increment
`suppression_records_rejected_total{reason="fields"}`, emit a structured
malformed log, fail open, and produce neither a delivery observation nor a
state write. E4 remains deployed evidence for real timestamp and delivery-age
behavior.

## 24. Future-time trust before source watermark generation

**Question**: Where must the fixed future-time trust boundary be enforced when
the source timestamp assigner runs before `process_element2`?

| Option | Description |
|--------|-------------|
| A | Clamp to Kafka record timestamp at the source for missing, unreadable, or over-bound occurrence time, retain the original payload, and keep downstream rejection in `process_element2`. |
| B | Drop or filter an over-bound record upstream before it reaches the operator. |
| C | Keep downstream-only validation and let the source assign every parsed `occurred_at_ms`. |

**Selection**: Option A — selected autonomously.

**Rationale**: Option C is unsafe watermark poisoning.
`SuppressionTimestampAssigner` sees `occurred_at_ms` before the operator can
reject it, so a far-future value can irreversibly advance the source watermark.
After a later chat-idle period, that value can jump the connected operator
watermark and permanently stall real-time timers when chat resumes. Option B
protects event time but loses the existing downstream `reason="fields"`
counter/warning and complicates the source with filtering responsibility.
Option A applies the existing fixed
`SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` at both layers using the same
injectable/current receipt/source wall-clock basis. The source uses Kafka
`record_timestamp` only for watermark assignment; it does not rewrite or
validate the payload. `process_element2` therefore still rejects the original
over-bound value with the existing warning and no delivery observation or
state access.

The boundary is exact: +30,000 ms uses `occurred_at_ms` as event time and is
accepted downstream; +30,001 ms uses Kafka record time upstream and is rejected
downstream. Missing/unreadable occurrence time keeps its existing Kafka-record
fallback. Replay must perform source assignment before downstream rejection
and keep the combined watermark monotonic.

**Stage**: Final code review correction.

**Impact**: Clarifies decision 23 without changing the fixed constant or adding
configuration, dependencies, or a new protocol. Amends FR-017/NFR-005/SC-010,
research D4/D13 and R3/R12, data-model I21, the contract's source and consumer
rules, the plan topology, quickstart A3/A4/E3/E4, existing tasks T038/T041/T050/
T058, checklist CHK020/CHK031/CHK035, and OPERATIONS diagnostics and E3/E4.
E1-E5 remain pending. Until implementation and deployment apply the source
check, the currently deployed path remains vulnerable to watermark poisoning.

## 25. Where the Python timestamp assigners actually run

**Question**: Decision 24 places the future-time trust check inside
`SuppressionTimestampAssigner`, and the chat path has assigned `sent_at`
through `SentAtTimestampAssigner` since Feature 004. Verification against the
PyFlink 1.18 source shows neither assigner runs at all as currently wired:
`StreamExecutionEnvironment.from_source()`
(`pyflink/datastream/stream_execution_environment.py`) forwards only
`watermark_strategy._j_watermark_strategy` into the Java `fromSource` call. A
Python `TimestampAssigner` attached with `.with_timestamp_assigner(...)` is
held on the Python-side `WatermarkStrategy` object only, and is installed into
an executable operator solely by
`DataStream.assign_timestamps_and_watermarks()`
(`pyflink/datastream/data_stream.py`), which wraps the stream in a Python
timestamp-assigner/watermark-generator operator. Passed to `from_source` it is
silently ignored — no error, no warning. Event time on both sources is
therefore the Kafka record timestamp today, not Twitch's clock. Where must
assignment be attached so the intended event time is real?

| Option | Description |
|--------|-------------|
| A | Build each `KafkaSource` with `WatermarkStrategy.no_watermarks()` in `env.from_source(...)`, then call `.assign_timestamps_and_watermarks(real_strategy)` on the returned `DataStream`, for **both** the chat and suppression sources. |
| B | Keep source-level assignment and supply a Java `TimestampAssignerSupplier` so the strategy handed to `from_source` carries a working assigner. |
| C | Leave the current wiring; accept Kafka record time as event time and retire the assigners. |

**Selection**: Option A — selected autonomously.

**Rationale**: Option C is the status quo and is a silent contract violation.
Feature 004 states that the detector buckets on `sent_at`, and Feature 007
states that suppression intervals are bounded by `occurred_at_ms`; under the
current wiring both use broker ingestion time, `SuppressionTimestampAssigner`
is unreachable dead code, and decision 24's entire source-side protection would
never execute. Option B would work, but it introduces a Java class and a
JVM-side build surface that this repository does not have, for a problem the
Python DataStream API already solves in one call; it is a large new surface
against the "no new module or dependency" constraint.

Option A is the documented PyFlink idiom and keeps every value already fixed by
D4 and D16: bounded out-of-orderness, `SUPPRESSION_IDLENESS_SECONDS = 5` below
chat's 10 s, `latest()` offsets, four partitions, and Python assigners carrying
the +30 s source trust fallback. The strategy handed to `from_source` becomes
`no_watermarks()` **only** because watermark generation moves one operator
downstream; it is not the rejected `no_watermarks()` end state D4 forbids,
because the real strategy is attached immediately and unconditionally to the
resulting stream. **Amendment by decision 26:** “carrying” the assigner also
requires an exact builder order. In PyFlink 1.18, `with_idleness()` returns a
fresh Python `WatermarkStrategy` wrapper and does not copy an assigner already
stored in `_timestamp_assigner`. Both real strategies must therefore be built
as bounded out-of-orderness → `with_idleness(...)` →
`with_timestamp_assigner(...)` **last**. Reversing the final two calls silently
drops the Python assigner even though the post-source attachment is correct.

The correction is applied to **both** streams, not only to suppression. Feature
007 requires the peak second and the suppression interval to be compared on
Twitch's shared clock (research §4.3). Fixing suppression alone would leave
chat bucketed on Kafka ingestion time and suppression bounded on
`occurred_at_ms`, i.e. a clock mismatch between the two sides of the very
comparison the gate performs — worse than the uniform mismatch that exists now.

One property genuinely changes and is re-derived rather than assumed.
Idleness was previously generated inside the Kafka source, per split; it is now
generated by the post-source assignment operator, per parallel subtask. The
safety argument in §4.1 depended on "one split per subtask", so it must be
restated on subtasks: the checked-in topology has topic partitions = source
parallelism = assignment/operator parallelism = 4, and the source-to-assigner
edge is a one-to-one forward chain, so each assignment subtask observes exactly
the records of exactly one split. Per-subtask idleness is therefore equivalent
to per-split idleness under the checked-in topology, and the I15/I16 bounds are
unchanged. This equivalence is conditional: any future partition/parallelism
mismatch, rescale, or repartition between source and assigner breaks it and
requires revalidation. E3 remains the deployed proof; nothing here may be
claimed from local stub tests.

**Stage**: Final code review correction.

**Impact**: Clarifies decisions 23-24 and research D4/D16 without changing any
fixed constant, threshold, dependency, environment variable, configuration
value, or file inventory. Amends FR-003/FR-017, research §4.1/§4.5/D4/R3/R12
and new R13, data-model I15/I21 and new I22, contract §1.1 and §5, the plan
topology and change table, quickstart A3/A4/B5, existing tasks
T038/T041/T045/T050/T058, checklist CHK019/CHK020/CHK021/CHK035, and OPERATIONS
diagnostics, E3/E4, and the deployment invariant. The chat source is touched
for the same reason, so Feature 004's `sent_at` contract becomes true in
practice rather than only on paper. E1-E5 remain pending; until this lands and
is deployed, the running job uses Kafka record time on both inputs and
decision 24's source-side trust check does not execute.

## 26. Chat event-time trust and strategy-builder order

**Question**: Making `SentAtTimestampAssigner` executable exposes two
previously non-blocking hazards. PyFlink 1.18's `with_idleness()` returns a
fresh wrapper without a previously stored Python `_timestamp_assigner`, so
builder order can silently undo decision 25. The now-live chat assigner would
also accept Python `bool` as an `int` and trust arbitrary future `sent_at`
values, allowing one chat record to poison the binding input watermark. How
must both paths be hardened?

| Option | Description |
|--------|-------------|
| A | Build both real strategies in the exact order bounded out-of-orderness → idleness → Python timestamp assigner last; on chat, accept only a plain `int` (not `bool`) at or before `source_clock_ms + 30_000`, otherwise use Kafka `record_timestamp` for event time while preserving the chat record. |
| B | Preserve post-source attachment but leave chat time unbounded and accept Python's `bool`-is-`int` behavior. |
| C | Reject or drop chat records with missing, malformed, or over-future `sent_at`. |

**Selection**: Option A — selected autonomously.

**Rationale**: Post-source attachment is necessary but not sufficient:
`with_idleness()` called after `with_timestamp_assigner()` discards the
Python-side assigner with no error. Requiring the assigner call last makes both
chat and suppression strategies effective. Once chat uses payload time, leaving
it unbounded creates a new active risk because chat is normally the binding
watermark input; a corrupt far-future `sent_at` can irreversibly advance it.
The already fixed `SUPPRESSION_MAX_FUTURE_SKEW_SECONDS=30` source-event-time
bound is symmetric, requires no new configuration, and keeps the two inputs on
the same trust model. A plain `int` check explicitly excludes `bool`; missing,
null, string, float, boolean, and over-bound values all fall back to Kafka
`record_timestamp`. Equality at +30,000 ms is accepted and +30,001 ms falls
back.

Option C violates the existing chat no-data-loss contract. Unlike suppression,
the chat payload is never rejected or dropped downstream because of
`sent_at`; only its assigned event timestamp falls back. This preserves chat
counting and command handling while preventing the binding chat watermark from
being poisoned.

The post-source assignment creates two additional Python operator stages, one
per input, each at parallelism four under the checked-in topology. Their real
TaskManager Python process-count and RSS impact is deployed evidence only; no
local test or static job-graph assertion may claim it.

**Stage**: Final code review hardening.

**Impact**: Amends decision 25; research §4.1.2/§4.3, R12-R13 and new R14;
data-model I22 and new I23; the suppression contract's chat-symmetry
cross-reference; plan topology; quickstart A3/E3; existing tasks
T038/T041/T045/T058; existing checklist items; and OPERATIONS E3 clock,
watermark, Python-process, and RSS checks. No task ID, dependency, environment
variable, runtime dependency, schema, or payload-drop behavior is added.
T058 and deployed evidence E1-E5 remain pending.

## 27. Approved capacity amendment: entry 400, retention and maximum 450

**Question**: Decision 5 locked a firm 400-channel ceiling with equal 400/400
thresholds and a 100-subscription reserve. The user has approved
`JOIN_THRESHOLD=400` / `LEAVE_THRESHOLD=450` and asked that every consequence
that cascaded from 400/400 be revisited from first principles rather than
patched. What capacity model applies, and on what condition?

| Option | Description |
|--------|-------------|
| A | Keep decision 5: firm 400 maximum, equal 400/400 thresholds, 800 of 900 subscriptions, 100 slots permanently reserved. |
| B | Entry threshold `JOIN_THRESHOLD=400`; retention **and** maximum `LEAVE_THRESHOLD=450`. A fresh channel enters only inside the top 400 by rank; an incumbent is retained through rank 450 and exits beyond it. The monitored maximum is 450 channels, which at two subscriptions per channel is exactly 900 of the 900 available slots — **no guaranteed free reserve**. Deployable only together with decision 28's exact-capacity engineering and its deployed evidence. |
| C | `LEAVE_THRESHOLD=450` with `JOIN_THRESHOLD=450` as well. |
| D | Option B's thresholds without decision 28: exact capacity on the existing pair placement, error classification, and `full_at` behaviour. |

**Selection**: Option B — the user-approved thresholds, explicitly conditioned
on decision 28.

**Rationale**: The 400/400 model was never the only defensible one, and the
100-slot reserve it protected was a margin rather than a structural
requirement. The approved model is the **exact analogy of the pre-007 800/900
ramp, halved**: 800/900 admitted fresh channels inside the top 800, retained
incumbents through rank 900, and at its own ceiling authorised 900 channels ×
1 subscription = 900 of 900 slots with nothing held back. 400/450 is the same
shape at two subscriptions per channel — a 400-deep entry gate, a 50-channel
retention band, and a ceiling that consumes the account exactly. The system ran
the 800/900 shape in production (OPERATIONS ramp ladder step 6, clean on
2026-08-31), so exact capacity is a returned-to operating point, not a new
class of risk, and the 50-channel band is worth more than a reserve that is
mostly idle: it removes the boundary-rank thrash that decisions 14 and 19 had
to measure and gate.

The reserve can be released because the two paths it was justified for do not
normally consume *new* slots:

- **Reconnect.** Twitch disables every subscription belonging to a session when
  that session ends, and disabled subscriptions do not count against the
  300-per-connection limit; reconnecting with a reconnect URL does not add to
  the websocket count either (research §1.1). The pool re-creates on the new
  session against slots the old session has already released. A reconnect
  therefore rotates subscription ids inside the same budget rather than
  requiring a spare 100.
- **Adoption.** A 409 conflict means the subscription already exists and is
  already counted. Adoption records the existing id in a `_Slot`; it creates
  nothing and consumes no additional slot. That is the whole point of the
  adoption path.

What zero slack does change is the *exceptional* cases, and those are accepted
explicitly rather than by silence:

1. **Parity fragmentation.** Three connections at 300 each hold 450 co-located
   pairs only under perfect packing. A connection left at an odd occupancy
   strands a single free slot that no whole pair can use, so the last channels
   are reachable only if a pair may be reserved one slot on each of two
   connections.
2. **In-flight overlap.** A delete that has not completed while a create is
   already in flight briefly needs a slot that the old subscription still
   holds.
3. **Foreign or orphaned enabled subscriptions.** Anything left on the
   client-id/user-id pair by an earlier revision or another process consumes
   the same 900 and, at exact capacity, is indistinguishable from a defect.
4. **Failed deletes.** An enabled subscription the pool believes is gone is a
   permanent slot leak once there is no reserve to absorb it.
5. **Below-cap `full_at`.** A connection marked full at an occupancy under 300
   strands the very slots the 450th channel needs.

None of these is acceptable unmitigated. They are accepted **only** with
decision 28's placement, classification, and visibility work in place, and
**only** on deployed evidence: E2a proves the dual transport converges at
400 channels / 800 subscriptions before the retention threshold moves, and E2b
proves exact-capacity behaviour at 450/900 with relational equality rather than
an approximate reading. Option A was not chosen because the user approved
otherwise; option C removes the retention band the amendment exists to create,
and would reintroduce exactly the zero-width thrash decisions 14 and 19 spent
two artifacts containing; option D is the same arithmetic without the
engineering, which converts every exceptional case above into a silent,
unrecoverable coverage hole at the ceiling.

**Stage**: Approved capacity amendment.

**Impact**: Supersedes decisions 5, 14, and 19, and amends 16 and 21 —
decision 19's 8-changes-per-poll, 24-hour release gate and decision 14's
zero-width premise are both removed, while the kill switch and the
unwind-capacity-before-relaxing-thresholds principle are kept. Rewrites
FR-013, FR-014, FR-015, NFR-001, NFR-007, SC-006, and SC-011, and the overview,
US3, edge cases, entities, assumptions, and out-of-scope text that quoted the
old numbers. Replaces the plan/research/data-model capacity tables with entry
400, maximum 450, 900 steady maximum, 0 guaranteed free, and 150 pairs per
session. Retires research §6's zero-width section and D10/D14/R5/R11, and adds
the fragmentation, foreign-subscription, below-cap `full_at`, failed-delete,
and exact-convergence risks. Restages the rollout — 400/400 single-subscription
convergence, dual transport at 400/400 with gating off, E1/E2a, a foreign
subscription sweep, then the ramp to the final 400/450, then E2b, E3, E4, E5 —
and restages the rollback so a capacity incident lowers retention to 400 and
reconverges first. Adds amendment tasks T059-T068, all unchecked. The
suppression event contract is untouched: it is capacity-independent.

## 28. Exact-capacity engineering for the 450-channel ceiling

**Question**: Decision 27 removes the free reserve, so the pool must reach 900
of 900 subscriptions and stay correct there. Today's placement takes a pair
only where two slots fit on one connection, a capacity exhaustion is
indistinguishable from other create failures, and `full_at` is a sticky
per-connection ceiling. What must change before exact capacity is deployed?

| Option | Description |
|--------|-------------|
| A | Nothing: run exact capacity on the current placement, classification, and `full_at` behaviour. |
| B | Four changes, together: (1) **co-location first, split second** — `route()` still prefers the connection already holding the channel's other slot and then a single connection with room for the whole pair, but when no connection has two free slots and total free slots across the pool is ≥ 2, it atomically reserves one slot on each of two connections inside the same critical section; (2) a distinct `PoolCapacityError` and a distinct capacity classification on the failure metric, separate from provider refusal and from transient transport errors; (3) hard capacity exhaustion must **not** arm the transient growth backoff, because there is nothing to wait for and arming it delays a legitimate later growth or repair; (4) `full_at` is cleared and re-evaluated on reconnect and retirement, and a connection that is full below the 300 cap is exposed as such. |
| C | Option B plus pair compaction: migrate an existing half-pair between connections to defragment the pool. |
| D | Option B's placement change only, leaving error classification and `full_at` as they are. |

**Selection**: Option B — selected autonomously as the condition attached to
decision 27.

**Rationale**: At 800 of 900 subscriptions the pool always had a whole free
connection's worth of slack, so every one of these behaviours was harmless. At
900 of 900 each becomes a way to strand capacity that the model says exists:

- **Split reservation is the load-bearing change.** Two free slots on two
  different connections are two free slots. Refusing the pair because neither
  connection alone can hold it turns a full pool into a *falsely* full pool,
  and at the ceiling that is the difference between converging at 450 and
  stalling at 449. Splitting a pair is already legal and already modelled
  (research R2, data-model §4): it costs locality, and a socket death then
  leaves the channel in a partial state that the reconciler already repairs.
  The reservation must be **atomic under the existing lock** — both slots
  reserved together or neither — or two concurrent pairs each reserve half of
  the same two free slots and both fail on create. Partial failure *after*
  reservation keeps the successful half, because a chat-only or
  notification-only channel is a convergent state and discarding the surviving
  half would cost a slot to recreate.
- **A hard capacity error is not a refusal and not a transient fault.** With no
  reserve, "no slot anywhere" becomes an expected, reportable operating state
  rather than an anomaly, and it must never reach the reconciler as a
  provider refusal — that path writes the durable per-channel refusal cache and
  would evict a channel for seven days over an arithmetic condition. A distinct
  `PoolCapacityError` and a distinct capacity label on the failure metric are
  what let an operator answer "is the account full, or is Twitch refusing us"
  without reading logs.
- **Transient backoff for a hard ceiling is a lie with a timer.** Growth
  backoff exists so the pool stops hammering a transport that may recover. A
  900-of-900 pool is not going to recover by waiting, and arming the backoff
  means the next genuine growth opportunity — after a retirement, a delete, or
  a set contraction — is delayed for no reason. Report it, do not arm it.
- **`full_at` must be re-evaluated, not remembered forever.** `full_at` records
  the occupancy Twitch refused at. If that number is below 300, the connection
  permanently offers fewer slots than the capacity model counts on, which at
  exact capacity is the whole margin. A session transition — reconnect or
  retirement — invalidates the observation that produced it, so it is cleared
  and re-evaluated there, and a below-cap full connection is exposed so an
  operator sees a stranded-capacity condition instead of an unexplained
  refusal at 449 channels.

Option A is decision 27's rejected option D by another name. Option C was
rejected as too complex for the benefit: migration means deleting a live
subscription and recreating it elsewhere, which opens a real coverage gap on a
channel that currently has none, needs its own ordering, failure, and
idempotence rules, and is only ever needed to recover locality that splitting
already handles correctly. Option D leaves the pool able to reach 900 but
unable to explain itself there, which is precisely the state in which an
operator cannot tell an expected ceiling from a defect.

**Stage**: Approved capacity amendment.

**Impact**: Adds the split-reservation fallback and its atomicity requirement to
the plan, research, and data-model placement rules; adds the distinct capacity
error and metric classification, the no-transient-backoff rule, and the
`full_at` clear/re-evaluate/expose behaviour to the same artifacts and to
OPERATIONS; adds the fragmentation, foreign-subscription, below-cap `full_at`,
failed-delete, and exact-convergence risks; and is carried by amendment tasks
T061 and T062 with observability in T063 and T065. No event-schema,
dependency, authorization, or reconciler-interface change: the reconciler stays
channel-keyed and the suppression contract stays capacity-independent.

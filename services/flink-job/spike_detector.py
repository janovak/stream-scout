"""
Pure spike-detection arithmetic, with no pyflink import.

Flink owns the state (MapState/ValueState) for checkpointing. This module
takes that state in as plain values. It returns what must change. See
AnomalyDetector in clip_detector_job.py for the Flink adapter that calls it.

Plan 06 Phase 3 changed the arithmetic. These are the changes:

  - The baseline no longer contains the window that it measures. Before, both
    ranges started at `second - baseline_seconds`. A spike thus increased the
    baseline that it was compared against.
  - `intensity` compares two values of the same unit. It compares the mean
    messages per second in the window against the mean in the baseline. It
    gives the result in baseline standard deviations. Before, the code
    compared a window *sum* against per-bucket statistics. That put a constant
    offset of approximately 5 x (mean / std) under every result. Flat chat
    scored 7 to 17 on a scale whose trigger was 5. Steady chat also scored
    higher than bursty chat.
  - The detector holds through an elevated period. It reports the highest
    value in that period. Before, it reported the first value that crossed
    the trigger. That value was always `k + a small amount`.

These are the bucket ranges. Plan 06 step 10 asks for explicit ranges. For a
`second` S:

    baseline  [S - window_seconds + 1 - baseline_seconds, S - window_seconds]
    window    [S - window_seconds + 1, S]

Each range holds the number of buckets that its config field names. The
defaults are 300 and 5. The ranges do not overlap. The plan writes these
ranges as `[second-300, second-window)` and `[second-window, second]`. That is
the same split, moved by one bucket. That spelling gives the window
`window_seconds + 1` buckets. Plan 06 step 10 lists this as a defect to
correct. The ranges above are the correction. The kept span is therefore
`baseline_seconds + window_seconds`, not `baseline_seconds`.

A bucket that is absent from `counts` means zero messages in that second, in
both ranges. Chat that stops sends no Kafka message, so it creates no bucket.
Absent and zero are the same event. If the code counted only the buckets that
are present, the two means would not be comparable. The window would average
its busy seconds only. The baseline would average its own busy seconds. The
warm-up gate below tells a silent channel from an unobserved one. It measures
how long the detector has watched the channel. It does not measure how busy
the channel is.
"""

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from typing import List, Mapping, Optional, Tuple

# Plan 06 Phase 2: the allowed out-of-orderness of the watermark strategy, in
# seconds. clip_detector_job.py's WatermarkStrategy and tools/replay.py's
# simulated watermark share this value. The two must compute event-time
# readiness in the same way.
#
# History. The value was 5 from Phase 2 through 2026-08-27. That was a round
# number with no measurement behind it. It became 1 on 2026-08-27, while chat
# arrived over IRC: KNOWN_ISSUES.md Issue 4 measured the live `chat-messages`
# topic per partition, in offset order, and found a worst inversion of 226ms
# and no record more than 1s out of order.
#
# The value is 2 from 2026-08-29. The transport changed. This is not a
# correction of the IRC number. Chat now arrives over EventSub (spec 004), and
# EventSub delivery lag is a different quantity from IRC inversion depth. It is
# the time between Twitch writing `metadata.message_timestamp` and this
# pipeline receiving the record. Spec 004 Phase 0 T002 measured that lag over
# 59,405 messages at 414 channels: p50 163ms, p95 217ms, p99 257ms,
# p99.9 415ms, p99.99 1,255ms. The tail did not grow against the 394-channel
# spike (154 / 220). One message went past 2s. That is 0.0017%.
#
# T002 measures delivery lag, which is not quite the quantity that matters. A
# record is late when its own second's timer has already fired, and that timer
# fires when the watermark passes the START of the second. So a record is late
# once `delivery_lag + (sent_at % 1000) > this constant`. The offset inside the
# second counts, and T002 does not include it.
#
# T038 measured the thing itself, on the live topic under this value: over
# 27,413 records, 0.0073% arrived after their own bucket's timer had fired.
# The SC-005 budget is 0.1%, so 2s holds with about 14x of headroom.
#
# Do not go back to 1s. The same sample puts a 1s bound at 0.620% -- more than
# six times over the SC-005 budget. 1s survived on IRC because IRC inversions
# topped out at 226ms; it does not survive EventSub's delivery lag plus the
# sub-second offset. This is the measurement that justifies the value, and it
# is a stronger reason than the margin argument D4 was written on.
#
# The cost is 1 second. It adds that second to the deliberate floor of the
# peak-to-clip-request delay (KNOWN_ISSUES.md Issue 4, "Post-deploy
# validation"). It does not change the separate, larger sparse-partition and
# idleness component of that delay.
#
# See specs/004-eventsub-parallel-reconciler/research.md D4.
WATERMARK_OUT_OF_ORDERNESS_SECONDS = 2

# KNOWN_ISSUES.md Issue 4: how long a source split can go silent before
# with_idleness() lets the operator watermark advance past it. Through
# 2026-08-27, chat-messages had 20 partitions against FLINK_PARALLELISM=4 --
# far more partitions than concurrent broadcasters (15-30), so most
# partitions carried 0-1 broadcasters and went silent for tens of seconds
# routinely; until this timeout fires, the operator watermark -- the minimum
# across every split -- is frozen for everyone, not just the quiet
# broadcaster. That specific mismatch is fixed (docker-compose.yml now
# creates chat-messages at 4 partitions, matching parallelism), but this
# timeout stays: a single broadcaster's own partition can still go quiet on
# its own, independent of partition count, and this is what recovers the
# watermark when it does. This only needs to be comfortably above the real
# out-of-orderness on the topic, not anywhere near the 60s it used to be.
# That figure was 226ms when measured over IRC. It is larger on EventSub:
# spec 004 T038 measured a worst inversion of 1,064ms and T046 a maximum
# ingestion skew of 1,219ms over 27,413 live records. 10s is still safe, but
# the margin is about 9x, not the ~40x the IRC number implied -- do not
# shrink this value off the 226ms figure. Not shared with tools/replay.py: that
# harness fires timers off a min-heap in strictly non-decreasing order and
# has no split-idleness concept to model.
WATERMARK_IDLENESS_SECONDS = 10

# Flink sends this as the watermark on job shutdown. Guards the arithmetic in
# next_chain_timer() below from overflowing a Java long on that one call.
MAX_WATERMARK = 9223372036854775807

# The code removes command messages (messages that start with "!") before they
# reach the detector. This regex is pure. clip_detector_job.py's CommandFilter
# and tools/replay.py share it, so the harness sees what the operator sees.
COMMAND_PATTERN = re.compile(r"^![a-zA-Z0-9]+")


def is_command(text: str) -> bool:
    return bool(COMMAND_PATTERN.match(text))


def next_chain_timer(timestamp: int, watermark: int) -> int:
    """KNOWN_ISSUES.md Issue 4, "Change B": where clip_detector_job.py's
    per-second chain timer (on_timer, ~line 757) should register its own
    successor next.

    The naive answer, timestamp + 1000, is what caused the bug: after a
    watermark jump, one advanceWatermark sweep fires a whole backlog of
    timers in ascending order, and each of those calls' re-registration
    lands after the sweep (a PyFlink/Beam bundle-boundary effect, confirmed
    by a local MiniCluster probe -- see KNOWN_ISSUES.md). The next watermark
    tick then replays the entire block again, once per tick, each round
    losing only its lowest timer -- proven by production logs matching
    triangular numbers exactly (KNOWN_ISSUES.md "Stage 2").

    The fix: only register a timer ahead of the current watermark.
    current_watermark() is confirmed (source trace + MiniCluster probe) to
    return the value that sweep is advancing to, shared by every timer that
    fires in it -- not each timer's own timestamp -- so every call in a
    replaying sweep sees the same watermark and computes the same answer,
    which collapses back to one registration (Flink dedupes same key +
    timestamp).

    A plain `if next_ts <= watermark: return None` (skip registering)
    was the first draft and is wrong: measured on the probe, it let the
    whole chain lapse rather than just skip stale rounds, silently starving
    a broadcaster's hold until its next chat message. Resuming at the first
    second after the watermark instead keeps the chain alive with exactly
    one timer, matching steady-state behavior once the jump is absorbed.

    Checks the *computed* resume point against MAX_WATERMARK, not the raw
    watermark value: watermark == MAX_WATERMARK is the only value Flink
    actually sends, but the rounding-up arithmetic overflows past it for
    any watermark in the last three digits below it too, and a check on
    the input alone wouldn't catch that band.
    """
    next_ts = timestamp + 1000
    if next_ts <= watermark:
        resumed = watermark - (watermark % 1000) + 1000
        if resumed <= MAX_WATERMARK:
            next_ts = resumed
    return next_ts


@dataclass(frozen=True)
class DetectorConfig:
    window_seconds: int = 5

    # Plan 06 step 12 puts this back to the first design value. Commit c7afdab
    # was a frontend change. It decreased this value to 10 and did not restore
    # it. The cost is a warm-up delay. See min_baseline_fraction below.
    #
    # There is a second cost. Plan 06 Phase 4 step 21 measured it on the
    # 12-hour corpus. The buckets of a spike enter the baseline
    # window_seconds later. They stay for this many seconds. The detector is
    # thus less sensitive to a second spike. Sensitivity falls to 0.66 at the
    # end of the cooldown. It is worst at 0.57, near 240 seconds. It returns
    # at approximately 305 seconds. That time is baseline_seconds +
    # window_seconds. The 30-second cooldown covers a tenth of it.
    #
    # This is mostly not a defect. The baseline mean increases 1.55 times in
    # that period. The standard deviation increases 1.57 times. Their ratio
    # stays between 1.03 and 1.14. The channel is thus truly more busy after
    # a large moment, and a trailing baseline must follow that. Only the
    # additional spread comes from the buckets of the spike. A robust
    # baseline that removed all of it gives 8.9% more detections. That is a
    # change to the arithmetic, not to a value here. It belongs with the
    # cooldown question that Plan 06 keeps for a later time.
    baseline_seconds: int = 300

    # The trigger, in standard deviations above the baseline mean. Spec 002
    # defines Intensity in these terms. This field was `std_dev_threshold`
    # before Plan 06 Phase 3. The environment variable is still
    # DETECTION_STD_DEV_THRESHOLD. docker-compose.yml and spec 002 FR-001b use
    # that name.
    #
    # Plan 06 Phase 4 step 18 read this value from the corpus distribution.
    # The corpus gives 840,225 broadcaster-seconds, in 12 hours, from 72
    # broadcasters. The tool recorded the reading of every second with the
    # trigger off. 4.0 is the 99.71st percentile of that distribution. The
    # median second scores -0.15. Flat chat thus scores approximately zero,
    # and the trigger is in the tail.
    #
    # The result is 516 detections in 12 hours. That is 2.2 for each
    # broadcaster-hour, from 61 of the 72 broadcasters. The median detection
    # is a burst 6.4 times the resting rate of its channel. The tool measured
    # the adjacent values also. 3.0 gives 4.7 for each broadcaster-hour. 5.0
    # gives 1.1, and only 54 broadcasters. The earlier value of 5.0 came from
    # the formula that Phase 3 deleted. It had no meaning on this scale.
    k: float = 4.0

    # The maximum length of one elevated period, before the detector reports a
    # result. Plan 06 Phase 4 step 19 measured real periods on the corpus with
    # no cap. The median period lasts 2 seconds. The 99th percentile is 14
    # seconds. The longest one in 12 hours is 24 seconds. A cap of 25 thus
    # cuts short no period. This is true for each trigger from 4.0 up. At a
    # trigger of 2.5 the cap cuts short 0.4% of the periods.
    #
    # Use the smallest cap that cuts short no period. Do not use a large one.
    # Phase 3 moved the report from the start of a period to its end. The cap
    # thus sets the maximum distance between the peak and the second that
    # asks Twitch for a clip. The earlier value of 60 was a placeholder. It
    # permitted a distance 2.5 times more than any real period needs. Phase 6
    # must correct that defect. This value does not correct it. It keeps the
    # defect no larger than the measurements permit.
    hold_cap_seconds: int = 25

    # Plan 06 Phase 4 step 20 measured the interval between periods with this
    # value at zero. The distribution has two parts, with almost nothing
    # between them. 22.7% of the adjacent pairs are less than 3 seconds apart.
    # Each of those is one chat reaction that moves across the trigger more
    # than one time. 61.2% are more than 300 seconds apart. Those are separate
    # events. Only 2.7% are between 10 and 30 seconds. Thus 30 removes very
    # few real events. But a 3-second cooldown removes almost the same
    # flicker.
    #
    # The value stays at 30. Plan 06 keeps it at 30 for now on purpose. Two
    # changes to behavior at the same time make attribution impossible. Also,
    # the correct tool for flicker is re-arm hysteresis, not a flat delay.
    # The measurement above is what that later decision needs.
    cooldown_seconds: int = 30

    # The warm-up gate. The detector must watch a channel for
    # min_baseline_fraction x baseline_seconds before it reports anything. At
    # the defaults this is 240 seconds, or approximately 4 minutes.
    #
    # This gate measures elapsed observation time. It does not measure how many
    # baseline buckets hold messages. A quiet second is data, because an absent
    # bucket counts as zero. A count of populated buckets would instead reject
    # every quiet channel permanently. On the Plan 06 dev-slice corpus, a count
    # of populated buckets blocks 7 of 23 broadcasters for the full hour.
    #
    # Plan 06 Phase 4 step 22 measured this value, which no one had measured
    # before. 0.8 blocks 2.22% of the evaluated seconds. It costs nothing
    # more. The shortest appearance in the corpus is 410 seconds, so each
    # channel gets past the gate. Each value from 0.5 to 0.9 gives the same
    # result, to within 5 detections of 516. The value is thus on a flat part
    # of the curve.
    #
    # The two ends are different. Below approximately 0.1 the readings have no
    # value. With the gate open, a second whose baseline holds one populated
    # bucket in 300 scores as much as 478. Do not use 1.0. observed_seconds
    # reaches the full 300 only when a message is in the single oldest
    # baseline second. 1.0 thus becomes a test of bucket density. It blocks
    # 39.9% of the seconds, against 2.7% at 0.9. That is the same fault that
    # the new gate removed. __post_init__ rejects 1.0 for that reason.
    min_baseline_fraction: float = 0.8

    def __post_init__(self):
        # Fail when the object is built. For the operator this means the job
        # stops at start-up with a clear message. A detector with a window of
        # zero divides by zero. A detector with a bad gate stays silent. Both
        # faults appear far from the docker-compose.yml error that caused them.
        if self.window_seconds < 1:
            raise ValueError(f"window_seconds must be >= 1, got {self.window_seconds}")
        if self.baseline_seconds < 2:
            raise ValueError(
                f"baseline_seconds must be >= 2 to have a standard deviation, "
                f"got {self.baseline_seconds}"
            )
        if self.hold_cap_seconds < 0:
            raise ValueError(f"hold_cap_seconds must be >= 0, got {self.hold_cap_seconds}")
        if self.cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds must be >= 0, got {self.cooldown_seconds}")
        # The range excludes 1.0, and not only the values above it.
        # observed_seconds reaches the full baseline only when a message is in
        # the single oldest baseline second. 1.0 thus stops the measurement of
        # elapsed time. It becomes a test of bucket density. On the Plan 06
        # corpus it blocks 39.9% of the seconds, against 2.7% at 0.9. The
        # fault is silent, so reject the value here.
        if not 0.0 < self.min_baseline_fraction < 1.0:
            raise ValueError(
                f"min_baseline_fraction must be in (0, 1), got "
                f"{self.min_baseline_fraction}. 1.0 needs a message in the oldest "
                f"baseline second, which makes the gate a density test."
            )
        # The timer chain of the operator runs only while the key holds
        # buckets. That is baseline_seconds + window_seconds after the last
        # message of the key. A longer cap cannot report a result before the
        # chain stops. The period would stay in state until its TTL removes it.
        # See AnomalyDetector.on_timer.
        retained_seconds = self.retained_seconds
        if self.hold_cap_seconds >= retained_seconds:
            raise ValueError(
                f"hold_cap_seconds ({self.hold_cap_seconds}) must be less than "
                f"baseline_seconds + window_seconds ({retained_seconds}), or a "
                f"hold can outlive the buckets whose timers report it"
            )

    @property
    def retained_seconds(self) -> int:
        """The full span of buckets that evaluate() keeps for one key."""
        return self.baseline_seconds + self.window_seconds

    @classmethod
    def from_env(cls) -> "DetectorConfig":
        return cls(
            window_seconds=int(os.getenv("DETECTION_WINDOW_SECONDS", cls.window_seconds)),
            baseline_seconds=int(os.getenv("DETECTION_BASELINE_SECONDS", cls.baseline_seconds)),
            k=float(os.getenv("DETECTION_STD_DEV_THRESHOLD", cls.k)),
            hold_cap_seconds=int(os.getenv("DETECTION_HOLD_CAP_SECONDS", cls.hold_cap_seconds)),
            cooldown_seconds=int(os.getenv("DETECTION_COOLDOWN_SECONDS", cls.cooldown_seconds)),
            min_baseline_fraction=float(
                os.getenv("DETECTION_MIN_BASELINE_FRACTION", cls.min_baseline_fraction)
            ),
        )


@dataclass(frozen=True)
class Spike:
    message_count: int              # the number of messages in the window
    baseline_mean: float            # messages per second
    baseline_std: float
    intensity: float                # (window_mean - baseline_mean) / baseline_std
    detected_at_seconds: int        # the event-time second of this measurement


@dataclass(frozen=True)
class HoldState:
    """An elevated period in progress, and its highest reading so far.

    Every peak_* field comes from one single second. That second is the peak.
    The reported Spike is therefore one complete measurement. It is not a peak
    intensity joined to the counts of a different second. Plan 06 states the
    rule: never record a different quantity than the one you compared against
    the trigger.
    """

    started_at: int                 # the event-time second the hold opened
    peak_intensity: float
    peak_at: int                    # the event-time second of the peak
    peak_message_count: int
    peak_baseline_mean: float
    peak_baseline_std: float

    @classmethod
    def opened(cls, measurement: Spike) -> "HoldState":
        return cls(
            started_at=measurement.detected_at_seconds,
            peak_intensity=measurement.intensity,
            peak_at=measurement.detected_at_seconds,
            peak_message_count=measurement.message_count,
            peak_baseline_mean=measurement.baseline_mean,
            peak_baseline_std=measurement.baseline_std,
        )

    def with_peak(self, measurement: Spike) -> "HoldState":
        """This hold, increased to `measurement` if that is a new maximum.

        Equal values keep the earlier second. The detector thus reports a flat
        peak at the second it first occurred. The result does not change with
        the length of the flat part.
        """
        if measurement.intensity <= self.peak_intensity:
            return self
        return HoldState(
            started_at=self.started_at,
            peak_intensity=measurement.intensity,
            peak_at=measurement.detected_at_seconds,
            peak_message_count=measurement.message_count,
            peak_baseline_mean=measurement.baseline_mean,
            peak_baseline_std=measurement.baseline_std,
        )

    def to_spike(self) -> Spike:
        return Spike(
            message_count=self.peak_message_count,
            baseline_mean=self.peak_baseline_mean,
            baseline_std=self.peak_baseline_std,
            intensity=self.peak_intensity,
            detected_at_seconds=self.peak_at,
        )

    # Flink has no TypeInformation for a dataclass. AnomalyDetector therefore
    # keeps this object in a Types.STRING() ValueState. asdict() reads the
    # field list from the dataclass, so the encoding cannot lose a new field.
    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, encoded: Optional[str]) -> Optional["HoldState"]:
        if not encoded:
            return None
        return cls(**json.loads(encoded))


@dataclass(frozen=True)
class Decision:
    emit: Optional[Spike]           # report now, with the peak
    hold: Optional[HoldState]       # the updated hold to keep in ValueState
    expired_buckets: List[int]      # the operator removes these from MapState

    # The two fields below are diagnostic. The operator does not read them.
    # AnomalyDetector uses `emit`, `hold` and `expired_buckets` only.
    #
    # Plan 06 Phase 4 step 17 needs the reading of every second. `emit` gives
    # the seconds that reported a spike only. It also carries the peak of a
    # period that ended before it. A separate tool could calculate the same
    # numbers again. But that tool could then disagree with the detector.
    # These fields thus give the arithmetic of the detector itself. The
    # measured distribution is therefore the distribution that the detector
    # sees.

    # The reading of this second, at the trigger or not. It is None for a
    # second that the detector cannot measure. That occurs when the warm-up
    # gate rejects the second, or when the baseline has no spread.
    # `intensity` does not depend on `k`, `hold_cap_seconds` or
    # `cooldown_seconds`. Thus one replay gives the full distribution for each
    # value of those three fields.
    measurement: Optional[Spike] = None

    # The time that the detector has watched this key, in seconds. The warm-up
    # gate compares this quantity against `min_baseline_fraction x
    # baseline_seconds`. Plan 06 Phase 4 step 22 uses it to measure the cost
    # of the gate at other fractions.
    observed_seconds: int = 0

    # True when this call passed an open hold through, unmeasured. The cause
    # is that peak_at sat ahead of second (Plan 09, KNOWN_ISSUES.md Issue 3).
    # Only a late or out-of-order call can cause this. A chat message can
    # arrive late, for a bucket behind an already-recorded peak. That message
    # still registers a timer for its own bucket. The timer then fires as
    # soon as the watermark passes it.
    #
    # AnomalyDetector.on_timer logs this event. evaluate() cannot log it,
    # because evaluate() must stay pure. This field is also the only
    # production signal for one open question: why does the cursor regress?
    # The old bug made duplicate clips. That symptom is now gone. The log
    # line that showed the bug is also gone. This field replaces it.
    hold_regressed: bool = False


def evaluate(
    counts: Mapping[int, int],      # bucket second -> message count
    second: int,                    # the event-time second to evaluate
    hold: Optional[HoldState],
    last_fire_second: Optional[int],
    config: DetectorConfig,
) -> Decision:
    """Pure. No I/O, no clock, no globals. The caller supplies `second`.

    A timer calls this once per event-time second per broadcaster. It does not
    run once per message. The module docstring gives the bucket ranges. It also
    explains why an absent bucket counts as zero.

    The caller must not supply buckets that are newer than `second`. Both
    callers remove them (AnomalyDetector.on_timer and replay._fire). The
    watermark that makes `second` ready has already admitted messages a few
    seconds later than `second`. This function ignores any such bucket.
    """
    window_start = second - config.window_seconds + 1
    baseline_start = window_start - config.baseline_seconds

    window_total = 0
    oldest_baseline_bucket = None
    baseline_counts = [0] * config.baseline_seconds
    expired_buckets: List[int] = []

    for ts_bucket, count in counts.items():
        if ts_bucket < baseline_start:
            expired_buckets.append(ts_bucket)
        elif ts_bucket < window_start:
            baseline_counts[ts_bucket - baseline_start] = count
            if oldest_baseline_bucket is None or ts_bucket < oldest_baseline_bucket:
                oldest_baseline_bucket = ts_bucket
        elif ts_bucket <= second:
            window_total += count

    # MapState.keys() gives no order. Sort the list. The eviction order of the
    # operator and the output of the replay harness must not change with the
    # internal order of the map.
    expired_buckets.sort()

    # The hold-regression guard below and the warm-up gate further down both
    # need observed_seconds. Compute it once, here.
    observed_seconds = (
        0 if oldest_baseline_bucket is None else window_start - oldest_baseline_bucket
    )

    # A hold's peak must never be later than the cursor evaluating it. A hold
    # sets peak_at to second at the moment it opens or updates
    # (HoldState.opened, HoldState.with_peak). So peak_at can be greater than
    # second only when this call itself is late or out of order.
    #
    # process_element in clip_detector_job.py registers an event-time timer
    # for every message's own bucket. It does this with no check on the
    # bucket's age. A message can arrive late, for a bucket behind an
    # already-recorded peak. That message still registers a timer for its own
    # bucket. The watermark has usually already passed that bucket. So the
    # timer fires on the next watermark advance. That call then reaches this
    # function with a second behind the hold it is about to read.
    #
    # Such a call cannot measure this hold, in either direction. Its own
    # counts stop at second (clip_detector_job.py's counts_as_of_now). That
    # window is less complete than the one the earlier, in-order call already
    # saw when it wrote peak_at. So this call's own intensity is a partial
    # reading. It is not a fair comparison against the hold's recorded peak.
    #
    # An emit from this hold would repeat an old peak, or report one too
    # early. That is KNOWN_ISSUES.md Issue 3: second minus hold.peak_at goes
    # negative, and can never exceed hold_cap_seconds. So the old code never
    # retired the hold this way. It re-reported the same peak once per
    # second, until the gap closed to cooldown_seconds on its own. One spike
    # then produced a dozen or more duplicate clips.
    #
    # An update from this call would be a different bug. This call's own
    # intensity is partial, so it is usually lower than the true peak. When
    # that holds, with_peak() leaves the hold alone -- a safe no-op. But
    # nothing guarantees that. A hold can peak, then decline. A late call's
    # partial reading can then register as a new maximum. That would silently
    # replace a correct, later peak with a smaller, earlier, and wrong one.
    #
    # So: pass the hold through, completely unchanged. Do this exactly as for
    # the warm-up gate and the no-spread case below. This call cannot measure
    # the hold, for an emit or for an update. Only a later, in-order call
    # (second >= peak_at) may retire, extend, or emit this hold.
    if hold is not None and hold.peak_at > second:
        return _unmeasurable(hold, expired_buckets, observed_seconds, hold_regressed=True)

    # Remove a hold whose peak is older than the cap. This rule applies to
    # every path below, so no reported peak is ever older than
    # hold_cap_seconds.
    #
    # The cap normally ends a period on the elevated path. It cannot do so when
    # the detector cannot measure a second, because the code keeps the hold and
    # does not reach the cap test. An unmeasurable baseline can therefore hold a
    # peak for many minutes. The clip for such a peak shows the wrong part of
    # the stream. The test must be here, before the gate, and not only on the
    # unmeasurable path: a measurable second that follows a blind period would
    # otherwise report that old peak.
    if hold is not None and (second - hold.peak_at) > config.hold_cap_seconds:
        hold = None

    # The warm-up gate. It measures how long the detector has watched this key.
    # The oldest bucket that is still in the baseline range gives that time.
    # Buckets older than baseline_start are removed each second, so a warm key
    # always reaches back to baseline_start.
    min_observed_seconds = int(config.baseline_seconds * config.min_baseline_fraction)
    if observed_seconds < min_observed_seconds:
        return _unmeasurable(hold, expired_buckets, observed_seconds)

    baseline_mean, baseline_std = _mean_and_sample_stdev(baseline_counts)

    if baseline_std <= 0.0:
        # A baseline with no spread gives the score nothing to divide by. In
        # practice this means very little traffic. It does not mean a channel
        # so regular that any change is very large.
        return _unmeasurable(hold, expired_buckets, observed_seconds)

    window_mean = window_total / config.window_seconds
    intensity = (window_mean - baseline_mean) / baseline_std

    measurement = Spike(
        message_count=window_total,
        baseline_mean=baseline_mean,
        baseline_std=baseline_std,
        intensity=intensity,
        detected_at_seconds=second,
    )
    elevated = intensity >= config.k

    # Each branch below gives the same three values. Only `emit` and `hold`
    # change. This local function keeps the three values in one place. A new
    # field on Decision is thus added one time, and not at five different
    # points. At five points, the one that you forget returns a default value
    # with no error.
    def decide(emit: Optional[Spike], hold: Optional[HoldState]) -> Decision:
        return Decision(
            emit=emit,
            hold=hold,
            expired_buckets=expired_buckets,
            measurement=measurement,
            observed_seconds=observed_seconds,
        )

    if hold is None:
        if not elevated:
            return decide(emit=None, hold=None)
        if _in_cooldown(second, last_fire_second, config):
            # The cooldown stops a new period from opening. It does not stop
            # each report. An open period always runs to its own end. The hold
            # already gives one report per period. A cooldown that could stop
            # an open period would only cut it short. It would then report a
            # peak that had not yet occurred.
            return decide(emit=None, hold=None)
        hold = HoldState.opened(measurement)
    elif elevated:
        hold = hold.with_peak(measurement)
    else:
        # The intensity fell below the trigger. The period is complete, so
        # report its peak. This second is not part of the period. It therefore
        # cannot become the peak.
        return decide(emit=hold.to_spike(), hold=None)

    # The channel is still elevated. Report a result when the hold reaches its
    # full cap. A period that stays elevated must still produce a clip.
    if second - hold.started_at >= config.hold_cap_seconds:
        return decide(emit=hold.to_spike(), hold=None)

    return decide(emit=None, hold=hold)


def _unmeasurable(
    hold: Optional[HoldState],
    expired_buckets: List[int],
    observed_seconds: int,
    hold_regressed: bool = False,
) -> Decision:
    """The result for a second that the detector cannot measure.

    Keep an open hold without a change. Do not report it and do not remove it.
    A report here would give a peak that was measured against a baseline the
    detector can no longer see. Removal would lose a real spike. evaluate()
    has already removed the hold if its peak is too old.

    `measurement` stays None here. A caller can thus tell an unmeasurable
    second from a second with a low intensity. `observed_seconds` still comes
    out. The warm-up gate is one of the two causes of an unmeasurable second,
    and step 22 must count how frequently it is the cause. `hold_regressed`
    marks a third cause (Plan 09). Here, a hold's peak sits ahead of `second`.
    This call cannot measure that hold, for an emit or for an update.
    """
    return Decision(
        emit=None,
        hold=hold,
        expired_buckets=expired_buckets,
        observed_seconds=observed_seconds,
        hold_regressed=hold_regressed,
    )


def _mean_and_sample_stdev(values: List[int]) -> Tuple[float, float]:
    """Two-pass mean and sample (n-1) standard deviation.

    This code does not use statistics.mean and statistics.stdev. Those
    functions calculate in exact rational arithmetic. They need approximately
    380 us for a 300-bucket baseline. The detector evaluates that baseline once
    per second for each broadcaster. The replay harness pays the same cost for
    every second of a 12-hour corpus. This code agrees with those functions to
    approximately 1e-15 relative on message counts. It is approximately 10
    times faster. It uses two passes, not a sum of squares, so it does not lose
    precision.
    """
    n = len(values)
    mean = sum(values) / n
    # Each term is a product of a float with itself, so no term is negative.
    # The sum is therefore always zero or more.
    variance = sum((value - mean) * (value - mean) for value in values) / (n - 1)
    return mean, math.sqrt(variance)


def _in_cooldown(
    second: int, last_fire_second: Optional[int], config: DetectorConfig
) -> bool:
    if last_fire_second is None:
        return False
    return (second - last_fire_second) <= config.cooldown_seconds

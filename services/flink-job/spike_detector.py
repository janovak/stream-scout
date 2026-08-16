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
WATERMARK_OUT_OF_ORDERNESS_SECONDS = 5

# The code removes command messages (messages that start with "!") before they
# reach the detector. This regex is pure. clip_detector_job.py's CommandFilter
# and tools/replay.py share it, so the harness sees what the operator sees.
COMMAND_PATTERN = re.compile(r"^![a-zA-Z0-9]+")


def is_command(text: str) -> bool:
    return bool(COMMAND_PATTERN.match(text))


@dataclass(frozen=True)
class DetectorConfig:
    window_seconds: int = 5

    # Plan 06 step 12 puts this back to the first design value. Commit c7afdab
    # was a frontend change. It decreased this value to 10 and did not restore
    # it. The cost is a warm-up delay. See min_baseline_fraction below.
    #
    # There is a second cost, and Plan 06 Phase 4 step 21 measured it on the
    # 12-hour corpus. The buckets of a spike enter the baseline window_seconds
    # later and stay for this many seconds. Sensitivity to a second spike falls
    # to 0.66 of its value at the first spike's onset by the time the cooldown
    # ends, reaches its worst at 0.57 around 240 seconds, and returns only at
    # approximately 305 seconds, which is baseline_seconds + window_seconds.
    # The 30-second cooldown therefore covers a tenth of the real recovery
    # time.
    #
    # It is mostly not a defect. The baseline mean rises 1.55x over that period
    # and the standard deviation 1.57x, so their ratio stays within 1.03x to
    # 1.14x. The channel is genuinely busier after a big moment, and a trailing
    # baseline is supposed to follow that. Only the excess spread comes from
    # the spike's own buckets, and a robust baseline that removed all of it
    # would recover 8.9% more detections. That is a change to the arithmetic,
    # not to a value here, so it belongs with the cooldown question that Plan
    # 06 defers to after shipping.
    baseline_seconds: int = 300

    # The trigger, in standard deviations above the baseline mean. Spec 002
    # defines Intensity in these terms. This field was `std_dev_threshold`
    # before Plan 06 Phase 3. The environment variable is still
    # DETECTION_STD_DEV_THRESHOLD. docker-compose.yml and spec 002 FR-001b use
    # that name.
    #
    # Plan 06 Phase 4 step 18 read this off the corpus distribution: 840,225
    # broadcaster-seconds, 12 hours, 72 broadcasters, every second's reading
    # recorded with the trigger disabled. 4.0 is the 99.71st percentile of that
    # distribution. The median second scores -0.15, so flat chat now sits at
    # zero and the trigger is genuinely in the tail.
    #
    # What that buys: 516 detections in 12 hours, or 2.2 per broadcaster-hour,
    # from 61 of the 72 broadcasters. The median detection is a burst 6.4 times
    # the channel's own resting rate. Neighbouring values were measured too --
    # 3.0 gives 4.7 per broadcaster-hour, 5.0 gives 1.1 and drops to 54
    # broadcasters. The earlier value of 5.0 came from the formula that Phase 3
    # deleted and meant nothing on this scale.
    k: float = 4.0

    # The maximum length of one elevated period, before the detector reports a
    # result. Plan 06 Phase 4 step 19 measured real periods on the corpus with
    # no cap at all: the median lasts 2 seconds, the 99th percentile 14, and
    # the longest in 12 hours is 24. A cap of 25 therefore truncates none of
    # them, and that holds for every trigger from 4.0 up. It stays true within
    # a quarter of a percent down at 2.5.
    #
    # Prefer the smallest cap that truncates nothing, not a generous one. Phase
    # 3 moved the report from a period's onset to its end, so the cap bounds
    # how far the reported peak can sit behind the second that asks Twitch for
    # a clip. The previous value of 60 was a placeholder and allowed a gap
    # 2.5 times longer than any real period needs. Phase 6 owns that defect;
    # this value does not fix it, it stops making it worse than measurement
    # supports.
    hold_cap_seconds: int = 25

    # Plan 06 Phase 4 step 20 measured the interval between periods with this
    # disabled. The distribution is in two parts and nothing sits between them.
    # 22.7% of consecutive pairs are less than 3 seconds apart, which is one
    # chat reaction flickering across the trigger, and 61.2% are more than 300
    # seconds apart, which is separate events. Only 2.7% land between 10 and 30
    # seconds, so 30 swallows very few real events -- but a 3-second cooldown
    # would suppress nearly the same flicker.
    #
    # Left at 30 all the same. Plan 06 fixes it "for now" on purpose, because
    # shipping two behavioural changes at once destroys attribution, and the
    # right tool for flicker is re-arm hysteresis rather than any flat delay.
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
    # Plan 06 Phase 4 step 22 measured the value itself, which had never been
    # measured. 0.8 blocks 2.22% of evaluated seconds and costs nothing else:
    # the shortest appearance in the corpus runs 410 seconds, so every channel
    # clears the gate. Anything from 0.5 to 0.9 gives the same answer to within
    # 5 detections out of 516, so the value sits on a flat part of the curve.
    #
    # Both ends are real. Below approximately 0.1 the readings are worthless:
    # with the gate open, seconds whose baseline holds one populated bucket in
    # 300 score up to 478. And 1.0 must not be used. observed_seconds only
    # reaches the full 300 when a message lands in the single oldest baseline
    # second, so 1.0 turns back into a bucket-density test and blocks 39.9% of
    # seconds, against 2.7% at 0.9. That is the same failure the gate was
    # rewritten to remove.
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
        if not 0.0 < self.min_baseline_fraction <= 1.0:
            raise ValueError(
                f"min_baseline_fraction must be in (0, 1], got {self.min_baseline_fraction}"
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
    # AnomalyDetector uses `emit`, `hold` and `expired_buckets` only. Plan 06
    # Phase 4 step 17 needs every second's reading, and not only the seconds
    # that reported a spike, because `emit` carries the peak of a period that
    # has already ended. A tool that computed the same numbers a second time
    # could disagree with the detector. These fields therefore carry the
    # detector's own arithmetic out, so the measured distribution is the
    # distribution that the detector sees.

    # This second's reading, whether or not it reached the trigger. It is None
    # for a second that the detector cannot measure, which means the warm-up
    # gate rejected it or the baseline had no spread. `intensity` does not
    # depend on `k`, `hold_cap_seconds` or `cooldown_seconds`, so one replay
    # gives the uncensored distribution for every value of those three.
    measurement: Optional[Spike] = None

    # How long the detector has watched this key, in seconds. This is the
    # quantity that the warm-up gate compares against
    # `min_baseline_fraction x baseline_seconds`. Plan 06 Phase 4 step 22 uses
    # it to price that gate at fractions other than the one it ran with.
    observed_seconds: int = 0


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
    observed_seconds = (
        0 if oldest_baseline_bucket is None else window_start - oldest_baseline_bucket
    )
    if observed_seconds < min_observed_seconds:
        return _unmeasurable(second, hold, expired_buckets, config, observed_seconds)

    baseline_mean, baseline_std = _mean_and_sample_stdev(baseline_counts)

    if baseline_std <= 0.0:
        # A baseline with no spread gives the score nothing to divide by. In
        # practice this means very little traffic. It does not mean a channel
        # so regular that any change is very large.
        return _unmeasurable(second, hold, expired_buckets, config, observed_seconds)

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

    # Every return below carries `measurement`, so a caller that wants the
    # distribution reads one field on every measurable second. It never
    # changes which branch runs.
    if hold is None:
        if not elevated:
            return Decision(
                emit=None,
                hold=None,
                expired_buckets=expired_buckets,
                measurement=measurement,
                observed_seconds=observed_seconds,
            )
        if _in_cooldown(second, last_fire_second, config):
            # The cooldown stops a new period from opening. It does not stop
            # each report. An open period always runs to its own end. The hold
            # already gives one report per period. A cooldown that could stop
            # an open period would only cut it short. It would then report a
            # peak that had not yet occurred.
            return Decision(
                emit=None,
                hold=None,
                expired_buckets=expired_buckets,
                measurement=measurement,
                observed_seconds=observed_seconds,
            )
        hold = HoldState.opened(measurement)
    elif elevated:
        hold = hold.with_peak(measurement)
    else:
        # The intensity fell below the trigger. The period is complete, so
        # report its peak. This second is not part of the period. It therefore
        # cannot become the peak.
        return Decision(
            emit=hold.to_spike(),
            hold=None,
            expired_buckets=expired_buckets,
            measurement=measurement,
            observed_seconds=observed_seconds,
        )

    # The channel is still elevated. Report a result when the hold reaches its
    # full cap. A period that stays elevated must still produce a clip.
    if second - hold.started_at >= config.hold_cap_seconds:
        return Decision(
            emit=hold.to_spike(),
            hold=None,
            expired_buckets=expired_buckets,
            measurement=measurement,
            observed_seconds=observed_seconds,
        )

    return Decision(
        emit=None,
        hold=hold,
        expired_buckets=expired_buckets,
        measurement=measurement,
        observed_seconds=observed_seconds,
    )


def _unmeasurable(
    second: int,
    hold: Optional[HoldState],
    expired_buckets: List[int],
    config: DetectorConfig,
    observed_seconds: int,
) -> Decision:
    """The result for a second that the detector cannot measure.

    Keep an open hold without a change. Do not report it and do not remove it.
    A report here would give a peak that was measured against a baseline the
    detector can no longer see. Removal would lose a real spike. evaluate()
    has already removed the hold if its peak is too old.

    `measurement` stays None here, which is how a caller tells an unmeasurable
    second from one that measured a low intensity. `observed_seconds` still
    comes out, because the warm-up gate is one of the two reasons to be here
    and step 22 has to count how often it is the reason.
    """
    return Decision(
        emit=None,
        hold=hold,
        expired_buckets=expired_buckets,
        observed_seconds=observed_seconds,
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

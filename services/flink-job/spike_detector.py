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
    # There is a second cost. The buckets of a spike enter the baseline
    # window_seconds later and stay for this many seconds. They increase the
    # baseline standard deviation for that time. The detector is therefore less
    # sensitive to a second spike for up to 5 minutes after the first one. At
    # baseline_seconds=10 that effect cleared in 10 seconds. The 30-second
    # cooldown no longer describes the full recovery time. Plan 06 Phase 4 step
    # 20 measures the interval between real spikes and decides what to do.
    baseline_seconds: int = 300

    # The trigger, in standard deviations above the baseline mean. Spec 002
    # defines Intensity in these terms. This field was `std_dev_threshold`
    # before Plan 06 Phase 3. The environment variable is still
    # DETECTION_STD_DEV_THRESHOLD. docker-compose.yml and spec 002 FR-001b use
    # that name.
    #
    # The value 5.0 comes from the earlier formula. It has no meaning on the
    # new scale, because Phase 3 removed the constant offset that it was
    # chosen against. Plan 06 Phase 4 selects the correct value from a
    # percentile of the corpus distribution.
    k: float = 5.0

    # The maximum length of one elevated period, before the detector reports a
    # result. This value is a placeholder. Plan 06 Phase 4 step 19 sets it from
    # the measured length of real spikes.
    hold_cap_seconds: int = 60

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
        return _unmeasurable(second, hold, expired_buckets, config)

    baseline_mean, baseline_std = _mean_and_sample_stdev(baseline_counts)

    if baseline_std <= 0.0:
        # A baseline with no spread gives the score nothing to divide by. In
        # practice this means very little traffic. It does not mean a channel
        # so regular that any change is very large.
        return _unmeasurable(second, hold, expired_buckets, config)

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

    if hold is None:
        if not elevated:
            return Decision(emit=None, hold=None, expired_buckets=expired_buckets)
        if _in_cooldown(second, last_fire_second, config):
            # The cooldown stops a new period from opening. It does not stop
            # each report. An open period always runs to its own end. The hold
            # already gives one report per period. A cooldown that could stop
            # an open period would only cut it short. It would then report a
            # peak that had not yet occurred.
            return Decision(emit=None, hold=None, expired_buckets=expired_buckets)
        hold = HoldState.opened(measurement)
    elif elevated:
        hold = hold.with_peak(measurement)
    else:
        # The intensity fell below the trigger. The period is complete, so
        # report its peak. This second is not part of the period. It therefore
        # cannot become the peak.
        return Decision(emit=hold.to_spike(), hold=None, expired_buckets=expired_buckets)

    # The channel is still elevated. Report a result when the hold reaches its
    # full cap. A period that stays elevated must still produce a clip.
    if second - hold.started_at >= config.hold_cap_seconds:
        return Decision(emit=hold.to_spike(), hold=None, expired_buckets=expired_buckets)

    return Decision(emit=None, hold=hold, expired_buckets=expired_buckets)


def _unmeasurable(
    second: int,
    hold: Optional[HoldState],
    expired_buckets: List[int],
    config: DetectorConfig,
) -> Decision:
    """The result for a second that the detector cannot measure.

    Keep an open hold without a change. Do not report it and do not remove it.
    A report here would give a peak that was measured against a baseline the
    detector can no longer see. Removal would lose a real spike. evaluate()
    has already removed the hold if its peak is too old.
    """
    return Decision(emit=None, hold=hold, expired_buckets=expired_buckets)


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

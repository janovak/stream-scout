"""
Pure spike-detection arithmetic, with no pyflink import.

Flink owns the state (MapState/ValueState) for checkpointing; this module takes
that state in as plain values and returns what should change. See
AnomalyDetector in clip_detector_job.py for the Flink adapter that calls this.

Plan 06 Phase 3 rewrote the arithmetic. What changed, and why:

  - The baseline no longer contains the window it is measuring. Before, both
    ranges started at `second - baseline_seconds`, so a spike inflated the
    baseline it was compared against.
  - `intensity` compares like with like: the window's mean messages-per-second
    against the baseline's, in units of the baseline's standard deviation.
    Before, a window *sum* (~6 buckets) was compared against per-bucket
    statistics, which put a constant pedestal of roughly 5 x (mean / std) under
    every reading -- so flat chat scored 7-17 on a scale whose trigger was 5,
    and steadier chat scored higher than burstier chat.
  - The detector holds through an elevation episode and reports its peak,
    instead of reporting the first instant that crossed the trigger. The old
    edge-trigger could only ever record `k + epsilon`, which is why the
    recorded numbers carried no information.

Bucket ranges, stated explicitly (Plan 06 step 10 asks for exactly this rather
than inferring the edges from a `>=`). For a `second` S:

    baseline  [S - window_seconds + 1 - baseline_seconds, S - window_seconds]
    window    [S - window_seconds + 1, S]

Each range holds exactly as many buckets as the config names -- 300 and 5 by
default -- and they do not overlap. Note the plan writes these as
`[second-300, second-window)` and `[second-window, second]`, which is the same
partition shifted by one: that spelling gives the window `window_seconds + 1`
buckets, the 6-vs-5 off-by-one Plan 06 step 10 lists as a defect to fix. The
ranges above are that fix, so the retained span is `baseline_seconds +
window_seconds`, not `baseline_seconds`.

A bucket missing from `counts` means zero messages in that second, in both
ranges. Chat that goes quiet has no Kafka message to create the bucket, so
absent and zero are the same event. Counting only the buckets that happen to be
present would make the two means incomparable -- the window would average over
its busy seconds only while the baseline averaged over its own -- and
`intensity` is exactly that comparison. The warm-up gate below is what
separates "silent" from "not observed yet": it counts buckets that are really
present, and refuses to evaluate until `min_baseline_fraction` of the baseline
range has been seen.
"""

import json
import math
import os
import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

# Plan 06 Phase 2: the watermark strategy's allowed out-of-orderness, in
# seconds. Shared between clip_detector_job.py's WatermarkStrategy and
# tools/replay.py's simulated watermark so the two compute event-time
# readiness identically.
WATERMARK_OUT_OF_ORDERNESS_SECONDS = 5

# Command messages (starting with "!") are filtered out before they ever
# reach the detector. Pure regex, shared by clip_detector_job.py's
# CommandFilter and tools/replay.py so the harness sees what the operator
# sees.
COMMAND_PATTERN = re.compile(r"^![a-zA-Z0-9]+")


def is_command(text: str) -> bool:
    return bool(COMMAND_PATTERN.match(text))


@dataclass(frozen=True)
class DetectorConfig:
    window_seconds: int = 5

    # Restored to the original design intent (Plan 06 step 12). Commit c7afdab,
    # a frontend change, dropped it to 10 and never put it back. The cost is a
    # warm-up: at min_baseline_fraction 0.8 a channel produces nothing for its
    # first ~4 minutes of chat, which is what stream-monitoring's join/leave
    # hysteresis exists to protect.
    baseline_seconds: int = 300

    # The trigger, in standard deviations above the baseline mean -- spec 002's
    # definition of Intensity. Named `std_dev_threshold` until Plan 06 Phase 3;
    # the environment variable is still DETECTION_STD_DEV_THRESHOLD, because
    # docker-compose.yml, spec 002 FR-001b and the comments around them all
    # refer to it by that name.
    #
    # 5.0 is carried over from the old formula and is NOT meaningful on the new
    # scale -- the pedestal it was chosen against is gone. Plan 06 Phase 4
    # picks the real value off a percentile of the corpus distribution.
    k: float = 5.0

    # How long an elevation episode may run before the detector fires anyway.
    # Untuned placeholder: Plan 06 Phase 4 (corpus tuning) has not run yet, and
    # step 19 picks this by plotting intensity through real spikes and seeing
    # how long they actually stay elevated.
    hold_cap_seconds: int = 60

    cooldown_seconds: int = 30
    min_baseline_fraction: float = 0.8

    def __post_init__(self):
        # Fail at construction, which for the operator means job startup with a
        # readable message. A detector built on a nonsense window would divide
        # by zero, or silently never fire, several layers away from the typo in
        # docker-compose.yml that caused it.
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
        # The operator's timer chain only runs while the key still has buckets,
        # which is baseline_seconds + window_seconds past its last message. A
        # cap longer than that could not fire before the chain lapsed, leaving
        # the episode stranded in state until its TTL. See
        # AnomalyDetector.on_timer.
        retained_seconds = self.baseline_seconds + self.window_seconds
        if self.hold_cap_seconds >= retained_seconds:
            raise ValueError(
                f"hold_cap_seconds ({self.hold_cap_seconds}) must be under "
                f"baseline_seconds + window_seconds ({retained_seconds}), or a "
                f"hold can outlive the buckets whose timers would fire it"
            )

    @classmethod
    def from_env(cls) -> "DetectorConfig":
        return cls(
            window_seconds=int(os.getenv("DETECTION_WINDOW_SECONDS", cls.window_seconds)),
            baseline_seconds=int(os.getenv("DETECTION_BASELINE_SECONDS", cls.baseline_seconds)),
            k=float(os.getenv("DETECTION_STD_DEV_THRESHOLD", cls.k)),
            hold_cap_seconds=int(os.getenv("DETECTION_HOLD_CAP_SECONDS", cls.hold_cap_seconds)),
            cooldown_seconds=int(os.getenv("DETECTION_COOLDOWN_SECONDS", cls.cooldown_seconds)),
        )


@dataclass(frozen=True)
class Spike:
    message_count: int              # messages in the window, summed
    baseline_mean: float            # messages per second
    baseline_std: float
    intensity: float                # (window_mean - baseline_mean) / baseline_std
    detected_at_seconds: int        # event-time second this was measured at


@dataclass(frozen=True)
class HoldState:
    """An elevation episode in progress, and the highest reading in it so far.

    Every peak_* field comes from one single second -- the peak's -- so the
    emitted Spike is one coherent measurement rather than a peak intensity
    stapled to whatever the counts happened to be when the hold ended. Plan 06:
    "never record a different quantity than the one you thresholded."
    """

    started_at: int                 # event-time second the hold opened
    peak_intensity: float
    peak_at: int                    # event-time second of the peak
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
        """This hold, raised to `measurement` if it is a new high.

        Ties keep the earlier second: a plateau is reported at the moment it
        was first reached, and the result does not depend on how long the
        plateau happens to run.
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

    # Flink has no built-in TypeInformation for a dataclass, so AnomalyDetector
    # keeps this in a Types.STRING() ValueState. The encoding lives here so the
    # operator does not have to know the field list.
    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at,
                "peak_intensity": self.peak_intensity,
                "peak_at": self.peak_at,
                "peak_message_count": self.peak_message_count,
                "peak_baseline_mean": self.peak_baseline_mean,
                "peak_baseline_std": self.peak_baseline_std,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, encoded: Optional[str]) -> Optional["HoldState"]:
        if not encoded:
            return None
        return cls(**json.loads(encoded))


@dataclass(frozen=True)
class Decision:
    emit: Optional[Spike]           # fire now, carrying the peak
    hold: Optional[HoldState]       # updated hold to persist in ValueState
    expired_buckets: List[int]      # operator removes these from MapState


def evaluate(
    counts: Mapping[int, int],      # bucket second -> message count
    second: int,                    # the event-time second being evaluated
    hold: Optional[HoldState],
    last_fire_second: Optional[int],
    config: DetectorConfig,
) -> Decision:
    """Pure. No I/O, no clock, no globals. The caller supplies `second`.

    Called once per event-time second per broadcaster, from a timer -- not once
    per message. See the module docstring for the bucket ranges and for why an
    absent bucket counts as zero.

    The caller must not pass buckets newer than `second`; both callers filter
    them out (AnomalyDetector.on_timer, replay._fire) because the watermark
    that makes `second` due has already admitted messages a few seconds past
    it. Any that slip through are ignored here rather than counted.
    """
    window_start = second - config.window_seconds + 1
    baseline_start = window_start - config.baseline_seconds

    window_total = 0
    baseline_present = 0
    baseline_counts = [0] * config.baseline_seconds
    expired_buckets: List[int] = []

    for ts_bucket, count in counts.items():
        if ts_bucket < baseline_start:
            expired_buckets.append(ts_bucket)
        elif ts_bucket < window_start:
            baseline_counts[ts_bucket - baseline_start] = count
            baseline_present += 1
        elif ts_bucket <= second:
            window_total += count

    # MapState.keys() promises no particular order, so sort: the operator's
    # eviction order and the replay harness's output must not depend on how
    # Flink happened to lay the map out.
    expired_buckets.sort()

    # Warm-up gate. Counts buckets genuinely present, not the zero-filled
    # slots -- an unobserved channel and a silent one look identical in
    # `counts`, and only this gate tells them apart.
    min_required = int(config.baseline_seconds * config.min_baseline_fraction)
    if baseline_present < min_required:
        # No measurement is possible this second. Leave any open hold exactly
        # as it is and let a later second resolve it: dropping it would lose a
        # spike that was really there, and firing it would report a peak the
        # detector never finished watching.
        return Decision(emit=None, hold=hold, expired_buckets=expired_buckets)

    baseline_mean, baseline_std = _mean_and_sample_stdev(baseline_counts)

    if baseline_std <= 0.0:
        # A perfectly uniform baseline leaves the z-score nothing to divide by.
        # In practice this is an artifact of very thin traffic, not a channel
        # so regular that any deviation is infinitely significant. Same
        # treatment as an unmeasurable baseline above.
        return Decision(emit=None, hold=hold, expired_buckets=expired_buckets)

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
            # The cooldown gates *starting* an episode, not each fire. Once a
            # hold is open it runs to its own end; peak-hold already fires
            # once per episode, so a cooldown that could interrupt one would
            # only truncate it and report a peak that had not arrived yet.
            return Decision(emit=None, hold=None, expired_buckets=expired_buckets)
        hold = HoldState.opened(measurement)
    elif elevated:
        hold = hold.with_peak(measurement)
    else:
        # Intensity fell back below the trigger: the episode is over, so fire
        # the peak it reached. This second is not part of the episode and does
        # not get to claim the peak.
        return Decision(emit=hold.to_spike(), hold=None, expired_buckets=expired_buckets)

    # Still elevated. Fire anyway once the hold has been open for its full cap,
    # so an episode that stays up indefinitely still produces a clip.
    if second - hold.started_at >= config.hold_cap_seconds:
        return Decision(emit=hold.to_spike(), hold=None, expired_buckets=expired_buckets)

    return Decision(emit=None, hold=hold, expired_buckets=expired_buckets)


def _mean_and_sample_stdev(values: List[int]) -> Tuple[float, float]:
    """Two-pass mean and sample (n-1) standard deviation.

    Not statistics.mean/stdev: those compute in exact rational arithmetic and
    cost ~380us on a 300-bucket baseline, which this detector evaluates once
    per second per broadcaster -- and which the replay harness pays again for
    every second of a 12-hour corpus. This agrees with them to ~1e-15 relative
    on message counts and is ~10x faster. Two-pass, not sum-of-squares, so it
    does not lose precision to cancellation.
    """
    n = len(values)
    mean = sum(values) / n
    variance = sum((value - mean) * (value - mean) for value in values) / (n - 1)
    # Guard the sqrt rather than the caller: rounding can leave a variance a
    # hair below zero when every value is identical.
    return mean, math.sqrt(variance) if variance > 0.0 else 0.0


def _in_cooldown(
    second: int, last_fire_second: Optional[int], config: DetectorConfig
) -> bool:
    if last_fire_second is None:
        return False
    return (second - last_fire_second) <= config.cooldown_seconds

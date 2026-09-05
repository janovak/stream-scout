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

Spec 007 adds a second, independent half to this module. It holds the gift and
raid suppression window policy, the keyed state that policy writes, and the
decoder for the `suppression-events` topic. That half sits at the bottom of the
file behind its own banner. It lives here so the operator ships one file and
needs no new -pyFiles entry, and it reads and writes nothing that evaluate()
reads or writes.
"""

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, List, Mapping, Optional, Tuple

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
# T038 measured the thing itself, on the live topic under this value: 2
# records in 66,154, across three 600s windows, arrived after their own
# bucket's timer had fired. That is 0.0030% against an SC-005 budget of 0.1%,
# so 2s holds with roughly 33x of headroom. Worst inversion 998-1,041ms.
#
# Do not go back to 1s. The same samples put a 1s bound at 0.20-0.62% -- two
# to six times over the SC-005 budget, in every one of the three windows. 1s
# survived on IRC because IRC inversions topped out at 226ms; it does not
# survive EventSub's delivery lag plus the sub-second offset. This is the
# measurement that justifies the value, and it is a stronger reason than the
# margin argument D4 was written on.
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
# watermark when it does.
#
# What bounds this value is NOT out-of-orderness, despite what this comment
# used to say. This timeout decides how long a LIVE split may be silent before
# Flink stops waiting for it. Too low and a still-active split is marked idle:
# the operator watermark then runs ahead of it, and its next records arrive
# late. Too high and one quiet split freezes the clock for everyone, which is
# the 60s bug this issue was opened for.
#
# A SPLIT IS A PARTITION, NOT A BROADCASTER. with_idleness() acts per source
# split. chat-messages has 4 partitions and the producer keys on
# broadcaster_id, so each partition carries several broadcasters -- about 5 at
# the current 20-channel operating point. A split therefore goes idle only
# when EVERY broadcaster hashed to it has been silent for the full timeout at
# once. That is a much shorter gap than any single broadcaster's, and it gets
# shorter as the monitored set grows.
#
# Do not size this off one broadcaster's inter-message gaps. Those run to tens
# of seconds in quiet chat, and reasoning from them argues for raising this
# back toward 30-60s -- which is precisely the watermark freeze that cutting
# it to 10 fixed. The quantity that bounds it is the silence gap of a whole
# partition, over the union of the broadcasters on it.
#
# The 226ms IRC out-of-orderness figure this comment used to cite is the wrong
# input as well, and it is also stale: spec 004 measured worst inversions
# above 1s on EventSub (research.md, Phase 4 T038).
#
# UNMEASURED, and deliberately left so (spec 004, 2026-08-29). Removing the
# wrong justification did not supply a right one: 10 is inherited from the
# KNOWN_ISSUES Issue 4 fix, where the only requirement was "far below 60".
# Nobody has measured the per-partition silence gap. It is left at 10 because
# it is working -- 5+ hours on the deployed job with zero `hold_regressed`
# events -- and because raising it is the direction that reopens Issue 4.
#
# This is a real hole in SC-005's guarantee, not a cosmetic one: a split
# marked idle leaves the watermark minimum, so the operator watermark can run
# AHEAD of that split, and records arriving after that are late in a way the
# 0.0041% figure does not bound (research.md, Phase 4 T038, the idleness
# exception). Whoever picks that up: measure per-partition gaps, not
# per-broadcaster ones, and remember the answer depends on partition count and
# on how many channels are monitored.
#
# Not shared with tools/replay.py: that
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


# ===========================================================================
# Spec 007 -- suppress gift and raid chat bursts. The pure half.
# ===========================================================================
#
# Arithmetic and validation only: no clock, no Kafka, no Flink, no I/O.
# AnomalyDetector.process_element2 in clip_detector_job.py is the adapter. It
# decodes a `suppression-events` value with decode_suppression_record(), folds
# the notice into a keyed ValueState with apply_notice(), and asks
# is_suppressed() once, at the very end of on_timer, when a decision would
# otherwise be yielded.
#
# The gate is strictly downstream of every state write that evaluate() drives,
# and nothing in this section is reachable from evaluate(). A gated run and an
# ungated run therefore leave identical detector state, which is what SC-004
# asserts and data-model I11 states.
#
# References: specs/007-suppress-gift-raid-bursts/data-model.md sections 3.1
# and 3.2, contracts/suppression-events.schema.md sections 2 and 4, research
# D4, D5, D7, D11, D13, D15, D16.

# The only `schema_version` this consumer understands. An unknown value -- and
# a missing one -- is ignored rather than guessed at, which is what makes a
# later field addition safe against a live topic (contract section 4.1 rule 2,
# research D8).
SUPPRESSION_SCHEMA_VERSION = 1

# The two gift categories share one window; a raid gets its own. Both spellings
# come from Twitch's `channel.chat.notification` notice_type.
SUPPRESSION_GIFT_NOTICE_TYPES = ("community_sub_gift", "sub_gift")
SUPPRESSION_RAID_NOTICE_TYPE = "raid"

# The closed trigger set, in contract order. Anything outside it -- `unraid`,
# `sub`, `resub`, an empty string, a different case, a future Twitch category
# -- creates no state at the producer and none at the consumer either. The
# consumer's copy of the allow-list is defence in depth for FR-005, not the
# only filter (contract invariant 3).
SUPPRESSION_TRIGGER_NOTICE_TYPES = SUPPRESSION_GIFT_NOTICE_TYPES + (
    SUPPRESSION_RAID_NOTICE_TYPE,
)

# How long a suppression source split may be silent before with_idleness()
# releases it from the operator's watermark minimum. Deliberately below
# WATERMARK_IDLENESS_SECONDS (10): the suppression input is sparse by nature --
# minutes pass with no notice on a channel -- so it must never become the
# binding minimum for the chat stream it gates (data-model I15).
#
# The cost of the low value is bounded and one-sided: a long-idle subtask that
# becomes active again with a single isolated notice can hold the two-input
# watermark for at most this plus WATERMARK_OUT_OF_ORDERNESS_SECONDS, i.e. 7
# seconds, which is a twentieth of the shortest window it can open
# (data-model I16, research R10).
SUPPRESSION_IDLENESS_SECONDS = 5

# The delivery age, in seconds, at or below which a consumed record counts as
# healthy. Above it the record is lagging and the operator logs it. This is a
# reporting threshold only: a lagging record is still applied, because lateness
# is not an error here (contract section 4.1 rules 5 and 8).
SUPPRESSION_DELIVERY_LAG_WARN_SECONDS = 30

# How far ahead of the consumer receipt a decoded occurrence time may claim to
# be and still be trusted. Fixed at 30 seconds by contract, deliberately NOT an
# environment variable: this is defence in depth against a poisoned deadline,
# not an operator tuning knob (FR-017, contract section 4.1 rule 4, decision
# 23).
#
# The bound exists because apply_notice() is a register that only ever moves a
# deadline outward. One record claiming to have occurred centuries from now --
# microseconds read as milliseconds, a badly set producer clock, a writer that
# is not this producer -- would pin suppress_until_ms past every later notice
# and silence that channel until its keyed state expired. Nothing downstream
# can undo it, so the only place to stop it is before it.
#
# Ordinary clock disagreement is not that. Within the allowance a future
# timestamp is applied normally and its negative raw age is clamped to zero
# with the existing skew diagnostic (contract section 2.2).
SUPPRESSION_MAX_FUTURE_SKEW_SECONDS = 30

# Why a record was ignored. These become a Prometheus label, so the set is
# closed by construction and ordered decode -> version -> fields, which is also
# the order the decoder tests them in (contract section 4.1, T042).
SUPPRESSION_REJECT_DECODE = "decode"
SUPPRESSION_REJECT_SCHEMA_VERSION = "schema_version"
SUPPRESSION_REJECT_FIELDS = "fields"
SUPPRESSION_REJECT_REASONS = (
    SUPPRESSION_REJECT_DECODE,
    SUPPRESSION_REJECT_SCHEMA_VERSION,
    SUPPRESSION_REJECT_FIELDS,
)

# Delivery health has exactly two published classes, and no third one for
# silence. A window with no record is idle/unknown and publishes nothing at
# all, because any value published during legitimate silence would be invented
# (NFR-005, research D13, data-model I19).
SUPPRESSION_LAG_HEALTHY = "healthy"
SUPPRESSION_LAG_LAGGING = "lagging"
SUPPRESSION_LAG_CLASSES = (SUPPRESSION_LAG_HEALTHY, SUPPRESSION_LAG_LAGGING)

# The spellings SUPPRESSION_GATING_ENABLED accepts, compared after strip() and
# lower(). Anything else is a start-up error rather than a silent default: a
# kill switch that reads as its default leaves the operator believing gating is
# off while clips are being dropped.
_TRUE_SPELLINGS = ("true", "1", "yes", "on")
_FALSE_SPELLINGS = ("false", "0", "no", "off")

# Distinguishes "the key was not in the decoded object" from "the key was there
# and held None". Only the first is a legacy state that may be reconstructed;
# the second is a value this code never wrote (SuppressionState.from_json).
_ABSENT = object()


def _is_plain_int(value: Any) -> bool:
    """True for an `int` that is not a `bool`.

    `bool` subclasses `int` and `True == 1`, so every check below that accepts
    an int has to say so explicitly. A `schema_version` of `true`, or a
    `broadcaster_id` of `false`, must be rejected rather than read as 1 and 0.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _suppression_env_int(name: str, default: int) -> int:
    """The variable as a whole number, or the code default when it is unset.

    Unset and empty are different: an unset variable takes the code default,
    while `NAME=` is an operator who meant to configure something and wrote
    nothing. The error names the variable, so the message points at the
    docker-compose.yml line that caused it.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be a whole number of seconds, got {raw!r}"
        ) from None


def _suppression_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_SPELLINGS:
        return True
    if normalized in _FALSE_SPELLINGS:
        return False
    raise ValueError(
        f"{name} must be one of {_TRUE_SPELLINGS + _FALSE_SPELLINGS}, "
        f"got {raw!r}"
    )


@dataclass(frozen=True)
class SuppressionConfig:
    """The window policy. It lives entirely in the consumer (research D7).

    The topic carries a notice and never a deadline, so retuning a window here
    changes the next decision and never invalidates a record already written.
    Nothing in this object depends on the size of a gift batch or on a raid's
    audience: decision 1 answered "should raid windows scale with audience?"
    with no, and viewer_count therefore reaches no field and no argument.
    """

    # 120 s and 180 s are the shipped defaults, duplicated into both Flink
    # blocks of docker-compose.yml so the jobmanager and the taskmanager run
    # the same policy.
    gift_window_seconds: int = 120
    raid_window_seconds: int = 180

    # The kill switch. True in code and `false` in docker-compose.yml on
    # purpose: deploying this feature must change no clip behaviour until an
    # operator flips the compose value after the E1-E3 evidence gates
    # (research D11, autonomous decision 21). SuppressionSourceSettings
    # .checked_in_gating_enabled carries the checked-in half of that pair.
    gating_enabled: bool = True

    delivery_lag_warn_seconds: int = SUPPRESSION_DELIVERY_LAG_WARN_SECONDS

    def __post_init__(self):
        # Fail where the object is built, exactly as DetectorConfig does, so
        # the job stops at start-up with a message naming the field rather than
        # running a policy that can never gate anything.
        for name in ("gift_window_seconds", "raid_window_seconds"):
            value = getattr(self, name)
            if not _is_plain_int(value) or value < 1:
                raise ValueError(
                    f"{name} must be a whole number of seconds >= 1, got "
                    f"{value!r}. A zero or negative window writes a deadline "
                    f"in the past, which costs a state write and gates nothing."
                )
        if not _is_plain_int(self.delivery_lag_warn_seconds) or (
            self.delivery_lag_warn_seconds < 0
        ):
            raise ValueError(
                f"delivery_lag_warn_seconds must be a whole number of seconds "
                f">= 0, got {self.delivery_lag_warn_seconds!r}"
            )
        # Zero is allowed above and is a real tuning choice: every record with
        # any measurable age is then reported as lagging.
        if not isinstance(self.gating_enabled, bool):
            raise ValueError(
                f"gating_enabled must be a bool, got {self.gating_enabled!r}"
            )

    def window_for(self, notice_type: Any) -> Optional[int]:
        """The window this category opens, or None when it opens none.

        None is the whole of the FR-005 defence: a category outside the trigger
        set has no entry here, so apply_notice() ignores it rather than
        defaulting it to some window. The comparison is case-sensitive and
        exact, because the topic carries Twitch's own spelling.
        """
        if not isinstance(notice_type, str):
            return None
        if notice_type in SUPPRESSION_GIFT_NOTICE_TYPES:
            return self.gift_window_seconds
        if notice_type == SUPPRESSION_RAID_NOTICE_TYPE:
            return self.raid_window_seconds
        return None

    @classmethod
    def from_env(cls) -> "SuppressionConfig":
        """The runtime reader. __post_init__ validates whatever it returns."""
        return cls(
            gift_window_seconds=_suppression_env_int(
                "SUPPRESSION_GIFT_WINDOW_SECONDS", cls.gift_window_seconds
            ),
            raid_window_seconds=_suppression_env_int(
                "SUPPRESSION_RAID_WINDOW_SECONDS", cls.raid_window_seconds
            ),
            gating_enabled=_suppression_env_bool(
                "SUPPRESSION_GATING_ENABLED", cls.gating_enabled
            ),
            delivery_lag_warn_seconds=_suppression_env_int(
                "SUPPRESSION_DELIVERY_LAG_WARN_SECONDS",
                cls.delivery_lag_warn_seconds,
            ),
        )


@dataclass(frozen=True)
class SuppressionState:
    """The per-broadcaster half-open interval, kept in a Types.STRING() ValueState.

    AnomalyDetector keeps this the same way it already keeps HoldState, under
    the same TTL. Absent state -- never written, or read back as absent under
    NeverReturnExpired -- means not suppressed. There is no third value and no
    "unknown": that is the structural form of the fail-open rule (FR-011,
    data-model I10).

    Both ends are stored, because both are needed. A deadline alone says only
    when suppression ENDS, which reads as "every instant in recorded history is
    suppressed" -- and on_timer reports a peak up to hold_cap_seconds after it
    happened, so a notice landing during that hold would retroactively gate a
    spike that peaked before the gift or raid existed (FR-006, FR-007, FR-018).

    The window duration is deliberately not stored. Only the instants it
    produced are, so a retuned window applies to the next notice and never
    retroactively.
    """

    suppress_from_ms: int           # epoch ms; inclusive start of the current chain
    suppress_until_ms: int          # epoch ms; exclusive end of the same interval
    notice_type: str                # the category that last MOVED the deadline
    notice_at_ms: int               # occurred_at_ms of that same notice; diagnostic

    def to_json(self) -> str:
        # asdict() carries all four fields, so a newly written state always
        # states where its interval opens. Only the reader tolerates the field
        # being absent, and only for a state written before it existed.
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, encoded: Optional[str]) -> Optional["SuppressionState"]:
        """The state, or None for anything this consumer cannot read.

        HoldState.from_json can raise, because a HoldState it cannot read is a
        bug worth failing on. This one must not: it is read inside on_timer,
        and an exception there fails the operator and stops chat detection for
        every key on the subtask. Unreadable therefore degrades to absent,
        which is the same value as not-suppressed, so the failure mode is a
        clip that is allowed rather than a pipeline that stops.

        Unknown keys are ignored, matching decode_suppression_record(): a
        newer writer may add a field, and an older reader must still read the
        fields it knows.

        A MISSING suppress_from_ms is the one exception, and it is a rolling-
        upgrade allowance rather than a default: a state string written by the
        pre-interval consumer opened its window at the notice it recorded, so
        notice_at_ms is the correct reconstruction. Present-but-unreadable is
        not the same thing -- a wrong type means the string did not come from
        this code, and it fails open like every other unreadable state.
        """
        if not encoded:
            return None
        try:
            decoded = json.loads(encoded)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        suppress_until_ms = decoded.get("suppress_until_ms")
        notice_type = decoded.get("notice_type")
        notice_at_ms = decoded.get("notice_at_ms")
        if not _is_plain_int(suppress_until_ms) or not _is_plain_int(notice_at_ms):
            return None
        # apply_notice() is the only writer and it only ever writes a trigger
        # category, so a value outside the set means the string did not come
        # from this code and cannot be trusted to gate anything.
        if notice_type not in SUPPRESSION_TRIGGER_NOTICE_TYPES:
            return None
        suppress_from_ms = decoded.get("suppress_from_ms", _ABSENT)
        if suppress_from_ms is _ABSENT:
            suppress_from_ms = notice_at_ms
        elif not _is_plain_int(suppress_from_ms):
            return None
        # Every state this code writes satisfies this ordering: the chain start
        # is at or before the notice that last moved the deadline, and that
        # notice is strictly inside the interval it opened (data-model I6, I7).
        # A string that does not is inconsistent rather than merely old, and an
        # inconsistent interval fails open like any other unreadable state.
        if not suppress_from_ms <= notice_at_ms < suppress_until_ms:
            return None
        return cls(
            suppress_from_ms=suppress_from_ms,
            suppress_until_ms=suppress_until_ms,
            notice_type=notice_type,
            notice_at_ms=notice_at_ms,
        )


@dataclass(frozen=True)
class SuppressionSourceSettings:
    """Every value the sparse suppression source depends on, as plain data.

    clip_detector_job.py builds its KafkaSource and WatermarkStrategy from this
    object instead of inline literals. The point is evidence: the numbers that
    decide watermark behaviour can then be asserted with nothing installed and
    no broker running, which is what keeps this feature's offline proof
    unconditional (research D16).

    Nothing here reads a file, a socket or the environment. The fields are the
    checked-in shape of the topic and of docker-compose.yml, and a static test
    compares the two.
    """

    topic: str = "suppression-events"

    # earliest() would feed hours-old occurred_at_ms values into event time and
    # pin the operator watermark in the past, stalling detection for every
    # channel. A restart therefore starts every channel fail-open, which is
    # intended (research D4, data-model section 5.6).
    starting_offsets: str = "latest"

    # The same bound the chat stream uses. Two inputs on one operator with
    # different bounds would make the joint watermark harder to reason about
    # for no gain.
    out_of_orderness_seconds: int = WATERMARK_OUT_OF_ORDERNESS_SECONDS

    idleness_seconds: int = SUPPRESSION_IDLENESS_SECONDS

    # One split per source subtask, so split idleness is well defined and no
    # subtask owns two partitions whose silence gaps interleave (research
    # section 4.1, contract section 1).
    expected_partitions: int = 4
    expected_parallelism: int = 4

    delivery_lag_warn_seconds: int = SUPPRESSION_DELIVERY_LAG_WARN_SECONDS

    # The value docker-compose.yml checks in, which is deliberately NOT the
    # SuppressionConfig code default. The pair is the deploy-inert rule stated
    # once, in data: shipping the code changes nothing until an operator edits
    # compose (research D11).
    checked_in_gating_enabled: bool = False


@dataclass(frozen=True)
class SuppressionNotice:
    """One accepted version-1 record, reduced to what the consumer may use.

    The optional fields the topic carries -- notice_id, received_at_ms,
    viewer_count -- are accepted by the decoder and deliberately dropped here.
    A field that never reaches this object cannot grow a consumer that depends
    on it: notice_id must not become a de-dup key (apply_notice()'s max() is
    already duplicate-safe), received_at_ms must not become a classification
    input, and viewer_count must not reach the window policy at all
    (contract section 2.2).
    """

    broadcaster_id: int             # the payload identity; the consumer never sees the key
    notice_type: str                # always one of SUPPRESSION_TRIGGER_NOTICE_TYPES
    occurred_at_ms: int             # epoch ms on the same clock as chat-messages.sent_at


@dataclass(frozen=True)
class SuppressionDecode:
    """Either a notice or a reason, never both and never an exception."""

    notice: Optional[SuppressionNotice] = None
    rejected_reason: Optional[str] = None       # one of SUPPRESSION_REJECT_REASONS


def decode_suppression_record(
    value: Any, config: Optional[SuppressionConfig] = None
) -> SuppressionDecode:
    """Decode one `suppression-events` value. Never raises.

    The job's Kafka sources deserialize values only, so the record key is not
    observable here at all. Routing, keying and state all come from the payload
    `broadcaster_id`, and key/payload agreement is asserted at the producer,
    where the key exists (contract section 4.0, research D15). That is also why
    payload validation is not optional: it is the only defence this side has.

    A rejection is a typed reason rather than an exception, because an
    exception out of process_element2 would fail the operator and stop chat
    detection for every key on the subtask (contract section 4.1 rule 1).

    `config` decides nothing about what decodes -- only which categories have a
    window, and that set is fixed. Retuning a window therefore cannot change
    which records are accepted (research D7).
    """
    if config is None:
        config = SuppressionConfig()

    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_DECODE)
    if not isinstance(payload, dict):
        # Valid JSON, but a list, a string or a number is not a record.
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_DECODE)

    schema_version = payload.get("schema_version")
    if (
        not _is_plain_int(schema_version)
        or schema_version != SUPPRESSION_SCHEMA_VERSION
    ):
        # Missing is treated as unknown. A version 2 must therefore ship its
        # consumer first (contract section 6).
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_SCHEMA_VERSION)

    broadcaster_id = payload.get("broadcaster_id")
    notice_type = payload.get("notice_type")
    occurred_at_ms = payload.get("occurred_at_ms")
    if not _is_plain_int(broadcaster_id):
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_FIELDS)
    if config.window_for(notice_type) is None:
        # A category outside the window map is ignored, not defaulted to some
        # window (contract section 4.1 rule 3, FR-005 defence in depth).
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_FIELDS)
    if not _is_plain_int(occurred_at_ms):
        # Never substituted with a receipt clock: that would fabricate a
        # deadline, which FR-017 forbids on both sides of the topic.
        return SuppressionDecode(rejected_reason=SUPPRESSION_REJECT_FIELDS)

    # Every other key, known-optional or entirely new, is dropped here. That is
    # what makes adding an optional field a version-1 change (contract 6).
    return SuppressionDecode(
        notice=SuppressionNotice(
            broadcaster_id=broadcaster_id,
            notice_type=notice_type,
            occurred_at_ms=occurred_at_ms,
        )
    )


def apply_notice(
    state: Optional[SuppressionState],
    notice_type: Any,
    occurred_at_ms: Any,
    config: SuppressionConfig,
) -> Optional[SuppressionState]:
    """The notice-bounded interval transition of data-model section 3.1.

    Returns the SAME object when nothing moved. The operator writes state only
    when the returned object is not the one it read, which is the write-only-on-
    change rule `hold` already follows (contract section 4.1 rule 6).

    Three cases, and no bookkeeping beyond them:

      - no state, or a notice at or after the current deadline: the old
        half-open interval has already ended, so a NEW interval opens at this
        occurrence rather than merging back into a window that is over
        (spec edge case "exactly at the deadline");
      - a candidate deadline earlier than or equal to the current one: the
        COMPLETE state is unchanged, including its start and its diagnostics.
        That is what makes a redelivery free and an out-of-order notice a
        no-op, and it is why the contract forbids a de-dup cache (FR-010);
      - anything else is an overlapping notice that extends: the deadline moves
        out, and the start stays at the earliest occurrence of the chain, so
        one continuous suppressed period is described by one interval
        (FR-006, US4-1).

    Only the maximum deadline is order-independent. The lower bound follows the
    transition above rather than an order-independence claim, because an
    earlier/equal candidate is deliberately a complete no-op (decision 22).

    Viewer count is not a parameter. Raid audience size cannot reach the
    arithmetic because the arithmetic never accepts it (FR-006, decision 1).
    """
    window_seconds = config.window_for(notice_type)
    if window_seconds is None:
        return state
    if not _is_plain_int(occurred_at_ms):
        # The decoder rejects this before it gets here, so reaching it means a
        # caller bypassed the decoder. Leave the state alone rather than raise
        # or fabricate a deadline from an untrustworthy time (FR-017).
        return state

    candidate_ms = occurred_at_ms + window_seconds * 1000
    if state is None or occurred_at_ms >= state.suppress_until_ms:
        return SuppressionState(
            suppress_from_ms=occurred_at_ms,
            suppress_until_ms=candidate_ms,
            notice_type=notice_type,
            notice_at_ms=occurred_at_ms,
        )
    if candidate_ms <= state.suppress_until_ms:
        return state
    return SuppressionState(
        suppress_from_ms=min(state.suppress_from_ms, occurred_at_ms),
        suppress_until_ms=candidate_ms,
        notice_type=notice_type,
        notice_at_ms=occurred_at_ms,
    )


def is_trustworthy_notice_time(
    occurred_at_ms: Any,
    consumer_receipt_ms: Any,
    max_future_skew_seconds: Any = SUPPRESSION_MAX_FUTURE_SKEW_SECONDS,
) -> bool:
    """Whether a decoded occurrence time may be applied at this receipt instant.

    Pure, and with no clock of its own: the receipt instant is passed in, the
    same way process_element2 captures it from its injected clock. A helper
    that read the wall clock could not be tested deterministically and would
    make that injected clock a lie (research D13).

    The allowance is bounded rather than zero because two hosts a few seconds
    apart are ordinary and the delivery-age clamp already answers that case
    (contract section 2.2). Equality at the bound passes; one millisecond
    beyond is malformed. Lateness is never untrustworthy -- an hour-old record
    is applied and reported as lagging (contract section 4.1 rule 8).

    Anything it cannot read -- a bool on either side, a string, a float, a
    negative allowance -- is untrustworthy rather than raising, because the
    only caller is inside process_element2, where an exception would stop chat
    detection for every key on the subtask.
    """
    if not _is_plain_int(occurred_at_ms) or not _is_plain_int(consumer_receipt_ms):
        return False
    if not _is_plain_int(max_future_skew_seconds) or max_future_skew_seconds < 0:
        return False
    return occurred_at_ms <= consumer_receipt_ms + max_future_skew_seconds * 1000


def is_suppressed(state: Optional[SuppressionState], peak_second: Any) -> bool:
    """Whether a decision peaking at `peak_second` is inside an open interval.

    Two arguments and no clock. The kill switch, the metric and the structured
    log belong to the operator, not to the arithmetic, and the report time is
    not an input: the compared instant is the PEAK second, so a burst that
    peaks inside a window cannot escape by being reported hold_cap_seconds
    later (research D5, data-model section 3.2).

    Absent state is not suppressed, which is the fail-open rule (FR-011). An
    unreadable peak second is treated the same way, for the same reason: this
    predicate runs inside on_timer, where refusing to answer would cost every
    key on the subtask.

    The interval is half-open at both ends, and both ends matter. A peak before
    the notice is eligible -- the gift or raid had not happened yet, so it
    cannot have caused that burst (FR-007, FR-018) -- and a peak exactly at the
    deadline is eligible too, which pairs with a notice at the deadline opening
    a new interval: the two together neither double-count nor leave a gap.
    """
    if state is None:
        return False
    if not _is_plain_int(peak_second) or peak_second < 0:
        return False
    peak_ms = peak_second * 1000
    return state.suppress_from_ms <= peak_ms < state.suppress_until_ms


@dataclass(frozen=True)
class DeliveryObservation:
    """How stale one consumed record was, and what that makes it.

    Carries nothing else. There is no per-channel gauge and no third class for
    silence: a window with no record is idle/unknown and publishes no value,
    because any value published during legitimate silence would be invented
    (NFR-005, research D13, data-model I19).
    """

    delivery_age_ms: int
    lag_class: str                  # one of SUPPRESSION_LAG_CLASSES
    clock_skew: bool                # the raw age was negative and was clamped

    @property
    def delivery_age_seconds(self) -> float:
        """The value suppression_delivery_age_seconds observes."""
        return self.delivery_age_ms / 1000


def observe_delivery_age(
    occurred_at_ms: int, consumer_receipt_ms: int, config: SuppressionConfig
) -> DeliveryObservation:
    """Classify one record's delivery health from the consumer receipt alone.

    `consumer_receipt_ms - occurred_at_ms` is the only classification input.
    The optional producer-side `received_at_ms` is not a parameter at all, so
    no logic here can come to depend on its presence: a fast Twitch-to-producer
    hop followed by a slow producer-to-consumer hop is lagging, which is the
    case NFR-005 exists for (contract section 2.2, section 4.1 rule 5).

    A negative raw age means the two clocks disagree, not that the record
    arrived before it happened. It is clamped to zero for both the observation
    and the classification, and flagged so the operator can log the skew
    without a second metric.

    At or below the threshold is healthy; above it is lagging. A lagging record
    is still applied -- lateness is a reporting fact here, not an error
    (contract section 4.1 rule 8).
    """
    if isinstance(occurred_at_ms, bool) or isinstance(consumer_receipt_ms, bool):
        raise TypeError(
            "delivery age needs epoch milliseconds on both sides, not a bool"
        )

    raw_age_ms = int(consumer_receipt_ms - occurred_at_ms)
    delivery_age_ms = max(0, raw_age_ms)
    threshold_ms = config.delivery_lag_warn_seconds * 1000
    lag_class = (
        SUPPRESSION_LAG_HEALTHY
        if delivery_age_ms <= threshold_ms
        else SUPPRESSION_LAG_LAGGING
    )
    return DeliveryObservation(
        delivery_age_ms=delivery_age_ms,
        lag_class=lag_class,
        clock_skew=raw_age_ms < 0,
    )

"""Tests for spike_detector.evaluate(). Pure module, no mocks, no pyflink."""

import inspect
import itertools
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

# Feature 007's suppression API -- SuppressionConfig, SuppressionState,
# SuppressionSourceSettings, decode_suppression_record(), apply_notice(),
# is_suppressed(), observe_delivery_age() -- is reached through the module
# rather than named in the `from` list below, on purpose. T029/T030 are
# written before T031/T032 exist, and a missing name must fail its own test
# rather than break collection for the detector tests this file already
# carries. Once T031/T032 land, every reference below resolves.
import spike_detector
from spike_detector import (
    MAX_WATERMARK,
    WATERMARK_IDLENESS_SECONDS,
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    DetectorConfig,
    HoldState,
    Spike,
    evaluate,
    is_command,
    next_chain_timer,
)

# A deliberately small baseline so fixtures stay readable. The shipped
# defaults (300s baseline) are asserted separately in TestShippedDefaults --
# they are the Plan 06 restoration and deserve their own guard, but a 300
# bucket fixture would tell a reader nothing.
CONFIG = DetectorConfig(
    window_seconds=5,
    baseline_seconds=20,
    k=3.0,
    hold_cap_seconds=10,
    cooldown_seconds=30,
)

# With CONFIG at second 1000: window is [996, 1000], baseline is [976, 995].
WINDOW = range(996, 1001)
BASELINE = range(976, 996)
NOW = 1000


def steady_baseline(level=10, wobble=1):
    """A baseline that alternates level+/-wobble, so std is exactly `wobble`."""
    return {ts: level + (wobble if ts % 2 else -wobble) for ts in BASELINE}


def baseline_std(counts):
    """Sample standard deviation over the baseline range, absent buckets as 0."""
    values = [counts.get(ts, 0) for ts in BASELINE]
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def evaluate_at(counts, second=NOW, hold=None, last_fire_second=None, config=CONFIG):
    return evaluate(counts, second, hold, last_fire_second, config)


def intensity_of(counts, second=NOW, config=CONFIG):
    """The intensity evaluate() measured this second, fired or not.

    `emit` only carries a measurement when an episode ends, so most seconds
    surface nothing there. Decision.measurement is the same second's reading,
    reported whether or not it reached the trigger (Plan 06 Phase 4 step 17).
    Returns None only when the second was genuinely unmeasurable (warm-up gate,
    or a baseline with no spread).
    """
    decision = evaluate(counts, second, None, None, config)
    return None if decision.measurement is None else decision.measurement.intensity


class TestCommandFilter:
    def test_is_command_matches_bang_prefix(self):
        assert is_command("!clip")
        assert is_command("!8ball magic")

    def test_is_command_false_for_ordinary_chat(self):
        assert not is_command("hello world")
        assert not is_command("")
        assert not is_command("? not a command")


class TestNextChainTimer:
    """KNOWN_ISSUES.md Issue 4, "Change B". next_chain_timer() decides where
    clip_detector_job.py's per-second chain timer registers its own
    successor. The naive answer (timestamp + 1000, unconditionally) is what
    caused the bug: after a watermark jump, PyFlink replays a whole backlog
    of already-fired timers once per watermark tick. These pin the corrected
    arithmetic, including the case a first draft of this fix got wrong (see
    the "chain lapses" test below)."""

    def test_steady_state_unchanged(self):
        """The watermark trails the newest message by
        WATERMARK_OUT_OF_ORDERNESS_SECONDS or more in normal operation, so
        the head of any sweep sits comfortably below it. Behavior here must
        match the pre-fix timestamp + 1000."""
        assert next_chain_timer(timestamp=24000, watermark=24999) == 25000

    def test_next_second_exactly_at_watermark_still_resumes(self):
        """A timer for exactly the current watermark fires in this same
        sweep -- re-registering it would replay a timer that just ran. `<=`,
        not `<`, is the correct boundary."""
        assert next_chain_timer(timestamp=28999, watermark=29999) == 30000

    def test_deep_replay_resumes_after_watermark_instead_of_lapsing(self):
        """The bug this guards against: a first draft of this fix simply
        skipped registering when timestamp + 1000 was stale, which measured
        as the whole chain dying until the broadcaster's next chat message --
        a silent hold-tracking gap, not just a suppressed replay. Resuming at
        the first second after the watermark keeps exactly one timer alive."""
        assert next_chain_timer(timestamp=20000, watermark=29999) == 30000

    def test_resume_point_rounds_up_to_the_next_full_second(self):
        """Real watermarks are epoch milliseconds and are not multiples of
        1000. The resume point must still land on a whole second."""
        assert next_chain_timer(timestamp=1000, watermark=30500) == 31000

    def test_shutdown_watermark_does_not_overflow(self):
        """Flink sends Long.MAX_VALUE as the watermark on job shutdown.
        MAX_WATERMARK must guard the modulo/subtraction from running at all
        on that value, not just from overflowing -- shutdown behavior stays
        identical to the pre-fix code."""
        assert next_chain_timer(timestamp=1000, watermark=MAX_WATERMARK) == 2000

    def test_near_max_watermark_does_not_overflow(self):
        """A watermark just below MAX_WATERMARK also overflows the rounding
        arithmetic (round up past the last 1000 lands above Long.MAX_VALUE),
        not only the exact sentinel value. Code review caught this: checking
        the raw watermark against MAX_WATERMARK misses this whole band, since
        it isn't the input that overflows, it's the computed resume point."""
        near_max = MAX_WATERMARK - 307
        assert next_chain_timer(timestamp=1000, watermark=near_max) == 2000


class TestShippedDefaults:
    """The defaults are the Plan 06 Phase 3 decisions; pin them."""

    def test_baseline_restored_to_five_minutes(self):
        # Commit c7afdab (a frontend change) dropped this to 10 and never put
        # it back. Plan 06 step 12 restores it.
        assert DetectorConfig().baseline_seconds == 300

    def test_window_and_gate_defaults(self):
        config = DetectorConfig()
        assert config.window_seconds == 5
        assert config.min_excess_messages == 2.0
        assert config.min_excess_gating_enabled is True
        assert config.min_baseline_fraction == 0.8
        assert config.cooldown_seconds == 30
        # 0.8 x 300 = 240 seconds of observation before a channel can produce
        # anything -- about 4 minutes. stream-monitoring's rank hysteresis
        # (JOIN 15 / LEAVE 30) softens the cost but does not remove it: those
        # are ranks, not dwell times, so nothing guarantees a channel is
        # watched for 4 minutes.
        assert int(config.baseline_seconds * config.min_baseline_fraction) == 240
        assert config.retained_seconds == 305

    def test_min_baseline_fraction_is_tunable_from_the_environment(self, monkeypatch):
        # It was the one field from_env() did not read, so relaxing the gate
        # needed a code change and a redeploy.
        monkeypatch.setenv("DETECTION_MIN_BASELINE_FRACTION", "0.5")
        assert DetectorConfig.from_env().min_baseline_fraction == 0.5

    def test_minimum_lift_is_tunable_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("DETECTION_MIN_EXCESS_MESSAGES", "2.5")
        monkeypatch.setenv("DETECTION_MIN_EXCESS_GATING_ENABLED", "off")
        config = DetectorConfig.from_env()
        assert config.min_excess_messages == 2.5
        assert config.min_excess_gating_enabled is False

    def test_invalid_minimum_lift_boolean_is_not_silently_defaulted(self, monkeypatch):
        monkeypatch.setenv("DETECTION_MIN_EXCESS_GATING_ENABLED", "sometimes")
        with pytest.raises(ValueError, match="DETECTION_MIN_EXCESS_GATING_ENABLED"):
            DetectorConfig.from_env()

    def test_tuned_defaults_come_from_the_corpus(self):
        """Plan 06 Phase 4 replaced two placeholders with measured values.

        Both were read off 840,225 per-second readings from the 12-hour
        corpus, recorded with the trigger disabled. k is the 99.71st
        percentile of that distribution. The cap is one second past the
        longest elevated period in the whole corpus, which ran 24 seconds --
        deliberately the smallest value that truncates nothing, because the
        cap bounds how far a reported peak can sit behind the clip request.
        """
        config = DetectorConfig()
        assert config.k == 4.0
        assert config.hold_cap_seconds == 25

    def test_the_cap_still_leaves_room_for_the_longest_measured_period(self):
        # The longest elevated period in 12h was 24s, at every k from 2.5 up.
        # A cap at or under that would truncate real spikes.
        assert DetectorConfig().hold_cap_seconds > 24

    def test_k_reads_the_unchanged_environment_variable_name(self, monkeypatch):
        # Renamed std_dev_threshold -> k in code only. docker-compose.yml and
        # spec 002 FR-001b still say DETECTION_STD_DEV_THRESHOLD.
        monkeypatch.setenv("DETECTION_STD_DEV_THRESHOLD", "4.25")
        monkeypatch.setenv("DETECTION_HOLD_CAP_SECONDS", "45")
        config = DetectorConfig.from_env()
        assert config.k == 4.25
        assert config.hold_cap_seconds == 45

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"window_seconds": 0},
            {"baseline_seconds": 1},
            {"hold_cap_seconds": -1},
            {"cooldown_seconds": -1},
            {"min_excess_messages": -1},
            {"min_excess_messages": float("inf")},
            {"min_excess_messages": True},
            {"min_excess_gating_enabled": "true"},
            {"min_baseline_fraction": 0.0},
            {"min_baseline_fraction": 1.5},
        ],
    )
    def test_nonsense_config_is_rejected_at_construction(self, kwargs):
        with pytest.raises(ValueError):
            DetectorConfig(**kwargs)

    def test_a_full_baseline_fraction_is_rejected(self):
        """1.0 looks like "use the whole baseline". It does something else.

        observed_seconds reaches baseline_seconds only when a message is in
        the single oldest baseline second. The gate thus stops the measurement
        of elapsed time and becomes a test of density. On the Plan 06 corpus
        it blocks 39.9% of the seconds, against 2.7% at 0.9. Reject the value
        when the object is built. Do not let a job run that way in silence.
        """
        with pytest.raises(ValueError, match="min_baseline_fraction"):
            DetectorConfig(min_baseline_fraction=1.0)
        assert DetectorConfig(min_baseline_fraction=0.99).min_baseline_fraction == 0.99

    def test_hold_cap_longer_than_the_retained_span_is_rejected(self):
        """A cap that outlives the buckets could never fire -- the operator's
        timer chain lapses once a key's last bucket expires."""
        with pytest.raises(ValueError, match="hold_cap_seconds"):
            DetectorConfig(window_seconds=5, baseline_seconds=10, hold_cap_seconds=15)
        # One under the retained span is the largest workable cap.
        assert DetectorConfig(
            window_seconds=5, baseline_seconds=10, hold_cap_seconds=14
        ).hold_cap_seconds == 14

    def test_shipped_defaults_satisfy_that_coupling(self):
        config = DetectorConfig()
        assert config.hold_cap_seconds < config.baseline_seconds + config.window_seconds


class TestIntensityScale:
    """Plan 06's headline defect: the number did not mean what it said."""

    def test_flat_traffic_scores_near_zero(self):
        """The regression test for the whole plan.

        The old formula compared a 5-bucket window *sum* against per-bucket
        statistics, leaving a resting value of roughly 5 x (mean / std) --
        between 4 and 33 on real traffic, against a trigger of 5. Flat chat has
        no spike in it and must score ~0.
        """
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        assert intensity_of(counts) == pytest.approx(0.0, abs=0.05)

    def test_flat_traffic_does_not_fire_at_any_plausible_trigger(self):
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        low_trigger = DetectorConfig(
            window_seconds=5, baseline_seconds=20, k=1.0, hold_cap_seconds=10
        )
        decision = evaluate_at(counts, config=low_trigger)
        assert decision.emit is None
        assert decision.hold is None

    def test_steady_chat_does_not_outscore_bursty_chat_at_rest(self):
        """The inversion that motivated this work.

        std sat in the denominator of a resting pedestal, so the metric partly
        measured how metronomic a channel was: the steadier the chat, the
        higher it scored with no spike at all. Both of these channels are
        resting at the same mean rate; neither is spiking.
        """
        metronomic = {ts: 10 + (1 if ts % 2 else -1) for ts in BASELINE}
        metronomic.update({ts: 10 for ts in WINDOW})

        swingy = {ts: 10 + (5 if ts % 2 else -5) for ts in BASELINE}
        swingy.update({ts: 10 for ts in WINDOW})

        steady_score = intensity_of(metronomic)
        bursty_score = intensity_of(swingy)
        assert steady_score == pytest.approx(0.0, abs=0.05)
        assert bursty_score == pytest.approx(0.0, abs=0.05)
        # The old formula put the metronomic channel an order of magnitude
        # above the swingy one here. Neither may now outrank the other.
        assert steady_score == pytest.approx(bursty_score, abs=0.05)

    def test_intensity_is_window_mean_minus_baseline_mean_over_baseline_std(self):
        """Worked example, pinned."""
        counts = steady_baseline(level=10, wobble=1)  # mean 10, sample std ~1.0127
        counts.update({ts: 25 for ts in WINDOW})

        baseline_values = [counts[ts] for ts in BASELINE]
        mean = sum(baseline_values) / len(baseline_values)
        variance = sum((v - mean) ** 2 for v in baseline_values) / (len(baseline_values) - 1)
        std = variance**0.5
        window_mean = 25.0

        assert intensity_of(counts) == pytest.approx((window_mean - mean) / std)

    def test_window_mean_not_window_sum(self):
        """A window at exactly the baseline rate scores 0, not 5x the rate/std.

        This is the unit mismatch: `window_sum` added ~5 one-second buckets
        while `mean` and `std` stayed per-bucket.
        """
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        decision = evaluate_at(counts)
        assert decision.emit is None and decision.hold is None
        # Sanity: the window really does hold 5 buckets summing to 50, so the
        # old sum-vs-per-bucket comparison would have flagged this hard.
        elevated = dict(counts)
        elevated.update({ts: 100 for ts in WINDOW})
        assert evaluate_at(elevated).hold.peak_message_count == 500

    def test_absent_bucket_counts_as_zero_messages(self):
        """Chat going quiet produces no bucket; that is 0 messages, not 'no data'."""
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        silent = dict(counts)
        for ts in WINDOW:
            del silent[ts]
        # A silent window sits a full baseline-mean below the baseline.
        assert intensity_of(silent) < intensity_of(counts)
        assert intensity_of(silent) == pytest.approx(-10.0 / baseline_std(counts), rel=1e-9)


class TestBaselineWindowSeparation:
    def test_baseline_excludes_the_window_it_measures(self):
        """A spike must not inflate the baseline it is compared against.

        The old code started both ranges at `second - baseline_seconds`, so the
        window's own buckets landed in both.
        """
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 500 for ts in WINDOW})
        decision = evaluate_at(counts)
        # Baseline mean is the outer buckets alone (10), untouched by the 500s.
        assert decision.hold.peak_baseline_mean == pytest.approx(10.0)

    def test_baseline_and_window_ranges_are_exact_and_adjacent(self):
        """window_seconds buckets in the window, baseline_seconds before it."""
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})

        # Bucket 995 is the newest baseline bucket: changing it moves the
        # baseline mean and leaves the window's message count alone.
        moved = dict(counts)
        moved[995] = 30
        assert evaluate_at(moved, hold=None).hold is None  # still not elevated
        assert intensity_of(moved) != intensity_of(counts)

        # Bucket 996 is the oldest window bucket: it counts toward the window.
        counts[996] = 60
        assert evaluate_at(counts).hold.peak_message_count == 60 + 10 * 4

    def test_buckets_older_than_the_baseline_are_expired(self):
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        counts[970] = 3   # older than baseline_start (976)
        counts[975] = 4   # the last second before the baseline begins
        decision = evaluate_at(counts)
        assert decision.expired_buckets == [970, 975]

    def test_expired_buckets_are_sorted_regardless_of_map_order(self):
        """MapState.keys() promises no order; the eviction list must be stable."""
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        scrambled = {}
        for ts in [960, 950, 970, 955]:
            scrambled[ts] = 1
        scrambled.update(counts)
        assert evaluate_at(scrambled).expired_buckets == [950, 955, 960, 970]


class TestWarmUpGate:
    """The gate measures elapsed observation time, not how busy a channel is."""

    def test_a_channel_watched_for_too_short_a_time_never_fires(self):
        """min_baseline_fraction is 0.8, so 20s of baseline needs 16s watched."""
        counts = {ts: 1000 for ts in WINDOW}
        # Oldest baseline bucket is 981, so window_start - 981 = 15s watched.
        counts.update({ts: 5 for ts in range(981, 996)})
        decision = evaluate_at(counts)
        assert decision.emit is None
        assert decision.hold is None

    def test_one_more_second_of_history_clears_the_gate(self):
        counts = {ts: 1000 for ts in WINDOW}
        counts.update({ts: 5 + (ts % 3) for ts in range(980, 996)})  # 16s watched
        assert evaluate_at(counts).hold is not None

    def test_a_quiet_channel_is_not_blocked_by_its_own_silence(self):
        """Regression for the finding that blocked 7 of 23 real broadcasters.

        A gate that counted populated buckets rejected any channel whose chat
        paused often, permanently rather than during warm-up. Silence is data:
        an absent bucket is zero messages, and the arithmetic already reads it
        that way. Only elapsed observation should gate.
        """
        counts = {ts: 1000 for ts in WINDOW}
        # Watched for the full baseline, but only every 4th second has chat --
        # 5 populated buckets out of 20, far under any density threshold.
        counts.update({ts: 3 + (ts % 2) for ts in range(976, 996, 4)})
        decision = evaluate_at(counts)
        assert decision.hold is not None, "a sparse but long-observed channel must be measurable"

    def test_a_returning_channel_must_warm_up_again(self):
        """Buckets expire, so a channel that was away re-earns its history."""
        counts = {ts: 1000 for ts in WINDOW}
        counts.update({ts: 5 for ts in range(990, 996)})  # only 6s of history
        assert evaluate_at(counts).hold is None

    def test_uniform_baseline_has_no_scale_and_does_not_fire(self):
        """std 0 would divide by zero; treat it as unmeasurable, not infinite."""
        counts = {ts: 5 for ts in BASELINE}
        counts.update({ts: 5000 for ts in WINDOW})
        decision = evaluate_at(counts)
        assert decision.emit is None
        assert decision.hold is None


class TestMinimumLiftGate:
    @staticmethod
    def sparse_counts(config, window_messages):
        window_start = NOW - config.window_seconds + 1
        baseline_start = window_start - config.baseline_seconds
        counts = {baseline_start: 1}
        counts.update(
            {NOW - offset: 1 for offset in range(window_messages)}
        )
        return counts

    def test_two_messages_clear_shipped_sigma_but_not_minimum_lift(self):
        config = DetectorConfig()
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=2),
            config=config,
        )

        assert decision.measurement.intensity > config.k
        assert (
            decision.measurement.message_count
            - decision.measurement.baseline_mean * config.window_seconds
            < config.min_excess_messages
        )
        assert decision.min_lift_candidate is True
        assert decision.min_lift_would_open is True
        assert decision.hold is None
        assert decision.emit is None

    def test_shadow_mode_preserves_the_pre_gate_decision(self):
        config = DetectorConfig(min_excess_gating_enabled=False)
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=2),
            config=config,
        )

        assert decision.min_lift_candidate is True
        assert decision.min_lift_would_open is True
        assert decision.hold is not None
        assert decision.emit is None

    def test_a_real_multi_message_spike_still_opens_a_hold(self):
        config = DetectorConfig()
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=3),
            config=config,
        )

        assert decision.min_lift_candidate is False
        assert decision.hold is not None
        assert decision.hold.peak_message_count == 3

    def test_the_minimum_lift_boundary_is_inclusive(self):
        config = DetectorConfig(
            window_seconds=5,
            baseline_seconds=20,
            k=0.3,
            hold_cap_seconds=10,
            cooldown_seconds=30,
        )
        counts = steady_baseline(level=1, wobble=1)
        counts.update({ts: 1 for ts in WINDOW})
        counts[NOW] = 3

        decision = evaluate_at(counts, config=config)

        assert decision.measurement.message_count == 7
        assert decision.measurement.baseline_mean == pytest.approx(1.0)
        assert decision.hold is not None
        assert decision.min_lift_candidate is False

    def test_a_blocked_reading_ends_and_reports_an_existing_valid_hold(self):
        config = DetectorConfig()
        peak = Spike(
            message_count=10,
            baseline_mean=0.1,
            baseline_std=0.1,
            intensity=10.0,
            detected_at_seconds=NOW - 1,
        )
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=2),
            hold=HoldState.opened(peak),
            config=config,
        )

        assert decision.min_lift_candidate is True
        assert decision.min_lift_would_open is False
        assert decision.emit == peak
        assert decision.hold is None

    def test_a_candidate_in_cooldown_would_not_open_without_the_gate(self):
        config = DetectorConfig(min_excess_gating_enabled=False)
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=2),
            last_fire_second=NOW,
            config=config,
        )

        assert decision.min_lift_candidate is True
        assert decision.min_lift_would_open is False
        assert decision.hold is None

    def test_would_open_is_computed_after_an_over_age_hold_is_discarded(self):
        config = DetectorConfig(min_excess_gating_enabled=False)
        stale_peak = Spike(
            message_count=10,
            baseline_mean=0.1,
            baseline_std=0.1,
            intensity=10.0,
            detected_at_seconds=NOW - config.hold_cap_seconds - 1,
        )
        decision = evaluate_at(
            self.sparse_counts(config, window_messages=2),
            hold=HoldState.opened(stale_peak),
            config=config,
        )

        assert decision.min_lift_candidate is True
        assert decision.min_lift_would_open is True
        assert decision.hold is not None
        assert decision.hold.peak_at == NOW


class TestPerSecondMeasurement:
    """Plan 06 Phase 4 step 17: every second's reading, elevated or not.

    `emit` reports the peak of a period that has already ended, so it censors
    the distribution twice over -- it drops every quiet second, and it reports
    one value per period instead of one per second. Decision.measurement is
    additive and carries the same arithmetic the trigger compared. The
    operator ignores it.
    """

    def test_a_quiet_second_still_reports_its_reading(self):
        counts = steady_baseline()
        counts.update({ts: 10 for ts in WINDOW})
        decision = evaluate_at(counts)
        assert decision.emit is None and decision.hold is None
        assert decision.measurement is not None
        assert decision.measurement.detected_at_seconds == NOW
        # Window mean equals baseline mean, so the reading is 0 -- a real
        # number, and the value the trigger compared against k.
        assert decision.measurement.intensity == pytest.approx(0.0, abs=1e-9)

    def test_the_reading_does_not_depend_on_the_trigger(self):
        """The whole point of step 17: one replay gives every k's distribution."""
        counts = steady_baseline()
        counts.update({ts: 40 for ts in WINDOW})
        readings = [
            evaluate_at(counts, config=replace(CONFIG, k=k)).measurement
            for k in (0.5, 3.0, 1000.0)
        ]
        assert readings[0] == readings[1] == readings[2]
        assert readings[0].intensity > 0

    def test_the_reading_is_this_second_not_the_reported_peak(self):
        """On the second an episode ends, the two carry different values."""
        counts = steady_baseline()
        counts.update({ts: 10 for ts in WINDOW})  # back to resting, ends the episode
        peak = Spike(
            message_count=500,
            baseline_mean=10.0,
            baseline_std=1.0,
            intensity=90.0,
            detected_at_seconds=NOW - 4,
        )
        decision = evaluate_at(counts, hold=HoldState.opened(peak))
        assert decision.emit.intensity == 90.0
        assert decision.emit.detected_at_seconds == NOW - 4
        assert decision.measurement.detected_at_seconds == NOW
        assert decision.measurement.intensity == pytest.approx(0.0, abs=1e-9)

    def test_an_unmeasurable_second_reports_no_reading(self):
        """None distinguishes 'could not measure' from 'measured a low value'."""
        warming_up = {ts: 1000 for ts in WINDOW}
        warming_up.update({ts: 5 for ts in range(981, 996)})  # 15s watched, gate needs 16
        assert evaluate_at(warming_up).measurement is None

        no_spread = {ts: 5 for ts in BASELINE}
        no_spread.update({ts: 5000 for ts in WINDOW})
        assert evaluate_at(no_spread).measurement is None

    def test_observed_seconds_is_what_the_gate_compares(self):
        """Step 22 prices min_baseline_fraction from a run at another value."""
        counts = {ts: 1000 for ts in WINDOW}
        counts.update({ts: 5 for ts in range(981, 996)})
        # Window starts at 996 and the oldest baseline bucket is 981.
        assert evaluate_at(counts).observed_seconds == 15
        # Reported on the blocked path too -- that is the path step 22 counts.
        assert evaluate_at(counts).measurement is None

    def test_observed_seconds_is_zero_when_nothing_has_been_seen(self):
        assert evaluate_at({ts: 3 for ts in WINDOW}).observed_seconds == 0

    def test_observed_seconds_saturates_at_the_full_baseline(self):
        counts = steady_baseline()
        counts.update({ts: 10 for ts in WINDOW})
        assert evaluate_at(counts).observed_seconds == CONFIG.baseline_seconds


class TestPeakHold:
    """Plan 06 step 15: hold while elevated, emit the peak."""

    def climb_and_fall(self, levels):
        """Run consecutive seconds whose window sits at each level in turn.

        Returns the decisions, one per second. Each second gets a fresh
        baseline at 10 +/- 1 and a window filled to `level`, so `level` alone
        drives intensity.
        """
        decisions = []
        hold = None
        last_fire_second = None
        for offset, level in enumerate(levels):
            second = NOW + offset
            counts = {ts: 10 + (1 if ts % 2 else -1) for ts in range(second - 24, second - 4)}
            counts.update({ts: level for ts in range(second - 4, second + 1)})
            decision = evaluate_at(counts, second=second, hold=hold, last_fire_second=last_fire_second)
            hold = decision.hold
            if decision.emit is not None:
                last_fire_second = second
            decisions.append((second, decision))
        return decisions

    def test_spike_that_climbs_then_falls_emits_once_at_the_peak(self):
        # Levels: quiet, rising, peak, falling, quiet again.
        decisions = self.climb_and_fall([10, 20, 40, 20, 10])
        emits = [(second, d.emit) for second, d in decisions if d.emit is not None]
        assert len(emits) == 1

        fired_at, spike = emits[0]
        peak_second = NOW + 2
        # Emitted when intensity fell back under k...
        assert fired_at == NOW + 4
        # ...but carrying the peak's value and the peak's timestamp, not the
        # firing second's. This is what reaches the clips table as detected_at.
        assert spike.detected_at_seconds == peak_second
        assert spike.message_count == 40 * 5

        # And the peak really is the maximum of the same quantity we triggered
        # on, not some other statistic.
        peak_decision = dict(decisions)[peak_second]
        assert spike.intensity == pytest.approx(peak_decision.hold.peak_intensity)

    def test_hold_is_open_and_silent_while_chat_stays_elevated(self):
        decisions = self.climb_and_fall([10, 20, 40, 30])
        assert [d.emit for _, d in decisions] == [None, None, None, None]
        for second, decision in decisions[1:]:
            assert decision.hold is not None
            assert decision.hold.started_at == NOW + 1

    def test_ties_keep_the_earlier_peak(self):
        """A plateau reports the moment it was first reached."""
        decisions = self.climb_and_fall([10, 40, 40, 40, 10])
        spike = [d.emit for _, d in decisions if d.emit is not None][0]
        assert spike.detected_at_seconds == NOW + 1

    def test_spike_elevated_past_the_cap_emits_at_the_cap(self):
        # hold_cap_seconds is 10 in CONFIG. Stay elevated for 20 seconds.
        decisions = self.climb_and_fall([10] + [40] * 20)
        emits = [(second, d.emit) for second, d in decisions if d.emit is not None]
        assert len(emits) >= 1

        fired_at, _ = emits[0]
        hold_opened_at = NOW + 1
        assert fired_at == hold_opened_at + CONFIG.hold_cap_seconds
        # Not later: the episode was still elevated and would otherwise have
        # run to the end of the input.
        assert fired_at < decisions[-1][0]

    def test_hold_open_then_no_further_messages_still_fires(self):
        """The 'hold open, no further messages' case from Plan 06.

        Timers keep ticking while the key has buckets, so the window drains to
        zero, intensity goes negative, and the episode closes on its own.
        """
        second = NOW
        counts = {ts: 10 + (1 if ts % 2 else -1) for ts in range(second - 24, second - 4)}
        counts.update({ts: 60 for ts in range(second - 4, second + 1)})
        decision = evaluate_at(counts, second=second)
        assert decision.hold is not None
        peak_second = second

        # No new messages arrive; the timer chain keeps evaluating each second.
        emitted = None
        for offset in range(1, 6):
            decision = evaluate_at(counts, second=second + offset, hold=decision.hold)
            if decision.emit is not None:
                emitted = decision.emit
                break
        assert emitted is not None
        assert emitted.detected_at_seconds == peak_second
        assert decision.hold is None

    def test_insufficient_baseline_mid_hold_passes_the_hold_through(self):
        """Nothing is measurable, so nothing is decided -- the hold survives."""
        open_hold = self.a_hold_peaking_at(NOW)
        counts = {ts: 5 for ts in range(990, 996)}  # far below the warm-up gate
        decision = evaluate_at(counts, second=NOW, hold=open_hold)
        assert decision.emit is None
        assert decision.hold == open_hold

    def test_a_hold_whose_peak_ages_past_the_cap_is_abandoned(self):
        """The unmeasurable path suspends the cap, so it needs its own bound.

        Passing the hold through unchanged is right for a second or two of
        unmeasurable baseline. Held indefinitely it becomes a trap: when the
        baseline recovers minutes later, the detector emits a peak from long
        ago and ClipCreator cuts a clip of whatever is happening now. Drop the
        hold instead once its peak is older than the cap, so no emitted peak is
        ever staler than hold_cap_seconds.
        """
        open_hold = self.a_hold_peaking_at(NOW)
        # No buckets at all: unmeasurable at every second below, so the cap
        # never gets a chance to end the period on its own.
        blind = {}

        # Still inside the cap: the hold survives untouched.
        within = evaluate_at(blind, second=NOW + CONFIG.hold_cap_seconds, hold=open_hold)
        assert within.emit is None
        assert within.hold == open_hold

        # Past it: abandoned, and never emitted.
        beyond = evaluate_at(blind, second=NOW + CONFIG.hold_cap_seconds + 1, hold=open_hold)
        assert beyond.emit is None
        assert beyond.hold is None

    def a_hold_peaking_at(self, peak_at, peak_intensity=9.5):
        """A hold whose only interesting field, for these tests, is peak_at."""
        return HoldState(
            started_at=NOW,
            peak_intensity=peak_intensity,
            peak_at=peak_at,
            peak_message_count=300,
            peak_baseline_mean=10.0,
            peak_baseline_std=1.0,
        )

    def test_a_hold_whose_peak_is_ahead_of_the_cursor_passes_through_unchanged(self):
        """Plan 09 / Issue 3: the mirror image of the "ages past the cap" test.

        peak_at is always set to `second` at the moment a hold is written
        (HoldState.opened / with_peak), so peak_at > second can only mean this
        call is itself late or out of order relative to the hold's own
        history -- see the long comment at the guard in spike_detector.py for
        why. The old retirement check `(second - hold.peak_at) >
        hold_cap_seconds` can never be true when the subtraction is negative,
        so a hold like this was never retired -- it re-emitted the same peak
        once per second until the gap narrowed to cooldown_seconds on its own.
        This is the exact shape from the taskmanager log evidence in
        KNOWN_ISSUES.md: the `(Ns ago)` field running negative from -58 to
        -30.

        The fix is pass-through, not drop-and-reopen: dropping the hold and
        letting this call open a fresh one from its own (necessarily partial)
        reading could silently downgrade an already-correct, further-
        progressed peak to a smaller, earlier, wrong one. The hold must come
        out of this call exactly as it went in.
        """
        open_hold = self.a_hold_peaking_at(NOW + 50)
        # Blind: unmeasurable regardless, so the only thing under test is
        # whether the hold survives entry into evaluate() untouched.
        decision = evaluate_at({}, second=NOW, hold=open_hold)
        assert decision.emit is None
        assert decision.hold == open_hold
        assert decision.hold_regressed is True

    def test_even_a_small_regression_passes_through_not_just_a_large_one(self):
        """Distinguishes this fix from a symmetric abs() cap check.

        A symmetric `abs(second - hold.peak_at) > hold_cap_seconds` guard
        would still miss this: 1 second ahead is nowhere near CONFIG's
        10-second cap. But peak_at > second means this call cannot measure the
        hold at any magnitude -- it can only mean the call is out of order,
        and that is true whether the gap is 1 second or 50.
        """
        open_hold = self.a_hold_peaking_at(NOW + 1)
        decision = evaluate_at({}, second=NOW, hold=open_hold)
        assert decision.hold == open_hold
        assert decision.hold_regressed is True

    def test_hold_regressed_is_false_on_every_other_unmeasurable_path(self):
        """hold_regressed is specific to this one cause, not a catch-all.

        The warm-up gate and the no-spread case were already unmeasurable
        before Plan 09; neither is a regressed hold, so neither should set
        the new flag.
        """
        warming_up = {ts: 1000 for ts in WINDOW}
        warming_up.update({ts: 5 for ts in range(981, 996)})  # 15s watched, gate needs 16
        assert evaluate_at(warming_up).hold_regressed is False

        no_spread = {ts: 5 for ts in BASELINE}
        no_spread.update({ts: 5000 for ts in WINDOW})
        assert evaluate_at(no_spread).hold_regressed is False

    def test_a_regressing_cursor_never_produces_more_than_one_emit_for_the_same_peak(self):
        """End to end: the production symptom from KNOWN_ISSUES.md Issue 3.

        One real peak was reported a dozen-plus times, each carrying
        identical intensity, counts, and baseline stats, because a stale
        hold's peak_at stayed ahead of a cursor that kept re-evaluating it.
        Simulate the cursor landing behind the hold's peak across many
        consecutive calls: it must never re-emit the same peak, and the hold
        itself must survive every one of those calls unchanged, ready for a
        later, legitimate call to retire or extend it properly.
        """
        stale_hold = self.a_hold_peaking_at(NOW + 58, peak_intensity=90.0)
        emits = []
        hold = stale_hold
        for second in range(NOW, NOW + 30):
            decision = evaluate_at({}, second=second, hold=hold)
            if decision.emit is not None:
                emits.append(decision.emit)
            assert decision.hold == stale_hold, "must pass through untouched, not be replaced"
            hold = decision.hold
        assert len(emits) == 0, "a regressed hold must never emit"
        assert hold == stale_hold, "the true peak must survive for a later, legitimate call"

    def test_a_stale_hold_cannot_survive_a_blind_stretch_and_emit_later(self):
        """End to end: the peak from a blind stretch never reaches a clip."""
        counts = {ts: 10 + (1 if ts % 2 else -1) for ts in BASELINE}
        counts.update({ts: 60 for ts in WINDOW})
        hold = evaluate_at(counts).hold
        assert hold is not None and hold.peak_at == NOW

        # Chat stops entirely for well over the cap.
        for offset in range(1, CONFIG.hold_cap_seconds * 3):
            decision = evaluate_at({}, second=NOW + offset, hold=hold)
            assert decision.emit is None, "a blind second must never emit"
            hold = decision.hold
            if hold is None:
                break
        assert hold is None, "the stale hold must be abandoned, not carried forever"

    def test_uniform_baseline_mid_hold_passes_the_hold_through(self):
        open_hold = HoldState(
            started_at=NOW,
            peak_intensity=9.5,
            peak_at=NOW,
            peak_message_count=300,
            peak_baseline_mean=10.0,
            peak_baseline_std=1.0,
        )
        counts = {ts: 5 for ts in BASELINE}  # std 0
        counts.update({ts: 5 for ts in WINDOW})
        decision = evaluate_at(counts, second=NOW, hold=open_hold)
        assert decision.emit is None
        assert decision.hold == open_hold


class TestCooldown:
    def spiking_counts(self):
        counts = {ts: 10 + (1 if ts % 2 else -1) for ts in BASELINE}
        counts.update({ts: 60 for ts in WINDOW})
        return counts

    def test_cooldown_blocks_a_new_hold_from_starting(self):
        decision = evaluate_at(self.spiking_counts(), last_fire_second=NOW - 5)
        assert decision.hold is None
        assert decision.emit is None

    def test_hold_starts_once_the_cooldown_has_passed(self):
        decision = evaluate_at(self.spiking_counts(), last_fire_second=NOW - 35)
        assert decision.hold is not None

    def test_cooldown_boundary_is_exclusive_of_its_own_length(self):
        at_edge = evaluate_at(self.spiking_counts(), last_fire_second=NOW - 30)
        just_past = evaluate_at(self.spiking_counts(), last_fire_second=NOW - 31)
        assert at_edge.hold is None
        assert just_past.hold is not None

    def test_cooldown_does_not_interrupt_a_hold_already_open(self):
        """Plan 06: the cooldown gates starting an episode, not each fire.

        An episode that opened legitimately runs to its own end even if a
        cooldown from an earlier fire is still ticking, because peak-hold
        already guarantees one fire per episode.
        """
        counts = self.spiking_counts()
        open_hold = HoldState(
            started_at=NOW - 2,
            peak_intensity=99.0,
            peak_at=NOW - 2,
            peak_message_count=400,
            peak_baseline_mean=10.0,
            peak_baseline_std=1.0,
        )
        # Still elevated, deep inside a cooldown: the hold survives untouched.
        held = evaluate_at(counts, hold=open_hold, last_fire_second=NOW - 1)
        assert held.emit is None
        assert held.hold == open_hold

        # And when it falls back, it fires -- the cooldown does not suppress it.
        quiet = {ts: 10 + (1 if ts % 2 else -1) for ts in BASELINE}
        quiet.update({ts: 10 for ts in WINDOW})
        fired = evaluate_at(quiet, hold=open_hold, last_fire_second=NOW - 1)
        assert fired.emit is not None
        assert fired.emit.intensity == pytest.approx(99.0)
        assert fired.hold is None


class TestHoldStateSerialization:
    """AnomalyDetector keeps the hold in a Types.STRING() ValueState."""

    def test_round_trips_through_json(self):
        hold = HoldState(
            started_at=1000,
            peak_intensity=12.5,
            peak_at=1003,
            peak_message_count=420,
            peak_baseline_mean=10.25,
            peak_baseline_std=1.75,
        )
        assert HoldState.from_json(hold.to_json()) == hold

    def test_absent_state_decodes_to_none(self):
        # ValueState.value() is None when nothing was ever written.
        assert HoldState.from_json(None) is None
        assert HoldState.from_json("") is None

    def test_to_spike_carries_every_field_from_the_peak(self):
        hold = HoldState(
            started_at=1000,
            peak_intensity=12.5,
            peak_at=1003,
            peak_message_count=420,
            peak_baseline_mean=10.25,
            peak_baseline_std=1.75,
        )
        assert hold.to_spike() == Spike(
            message_count=420,
            baseline_mean=10.25,
            baseline_std=1.75,
            intensity=12.5,
            detected_at_seconds=1003,
        )


class TestFutureBuckets:
    def test_buckets_newer_than_the_evaluated_second_are_ignored(self):
        """Callers filter these; evaluate() must not count them if one slips through."""
        counts = steady_baseline(level=10, wobble=1)
        counts.update({ts: 10 for ts in WINDOW})
        with_future = dict(counts)
        with_future.update({ts: 5000 for ts in range(NOW + 1, NOW + 6)})
        assert intensity_of(with_future) == pytest.approx(intensity_of(counts))


class TestAtShippedDefaults:
    """Every other test runs a small config, so nothing else exercises the
    300-second baseline, the 240-second gate, or the 60-second cap that the
    job actually runs with. A regression that only appears at those values
    would otherwise pass the whole suite."""

    DEFAULTS = DetectorConfig()
    SECOND = 1_000_000
    W_START = SECOND - DEFAULTS.window_seconds + 1
    B_START = W_START - DEFAULTS.baseline_seconds

    def full_baseline(self, level=20, wobble=2):
        return {
            ts: level + (wobble if ts % 2 else -wobble)
            for ts in range(self.B_START, self.W_START)
        }

    def test_flat_traffic_scores_near_zero_at_300_seconds(self):
        counts = self.full_baseline()
        counts.update({ts: 20 for ts in range(self.W_START, self.SECOND + 1)})
        assert intensity_of(
            counts, second=self.SECOND, config=self.DEFAULTS
        ) == pytest.approx(0.0, abs=0.05)

    def test_the_gate_opens_at_exactly_240_seconds_of_history(self):
        window = {ts: 5000 for ts in range(self.W_START, self.SECOND + 1)}

        short = dict(window)
        short.update({ts: 20 + ts % 3 for ts in range(self.W_START - 239, self.W_START)})
        assert evaluate(short, self.SECOND, None, None, self.DEFAULTS).hold is None

        just_enough = dict(window)
        just_enough.update({ts: 20 + ts % 3 for ts in range(self.W_START - 240, self.W_START)})
        assert evaluate(just_enough, self.SECOND, None, None, self.DEFAULTS).hold is not None

    def test_a_real_spike_holds_to_its_60_second_cap(self):
        hold, last_fire, emitted = None, None, []
        for offset in range(70):
            second = self.SECOND + offset
            w_start = second - self.DEFAULTS.window_seconds + 1
            counts = {
                ts: 20 + (2 if ts % 2 else -2)
                for ts in range(w_start - self.DEFAULTS.baseline_seconds, w_start)
            }
            counts.update({ts: 400 for ts in range(w_start, second + 1)})
            decision = evaluate(counts, second, hold, last_fire, self.DEFAULTS)
            hold = decision.hold
            if decision.emit is not None:
                emitted.append((second, decision.emit))
                last_fire = second

        assert len(emitted) == 1
        fired_at, spike = emitted[0]
        assert fired_at == self.SECOND + self.DEFAULTS.hold_cap_seconds
        assert fired_at - spike.detected_at_seconds <= self.DEFAULTS.hold_cap_seconds

    def test_expiry_keeps_exactly_the_retained_span(self):
        counts = self.full_baseline()
        counts.update({ts: 20 for ts in range(self.W_START, self.SECOND + 1)})
        counts[self.B_START - 1] = 7
        decision = evaluate(counts, self.SECOND, None, None, self.DEFAULTS)
        assert decision.expired_buckets == [self.B_START - 1]
        assert self.SECOND - self.B_START + 1 == self.DEFAULTS.retained_seconds


# ===========================================================================
# Feature 007 -- suppress gift and raid chat bursts. The pure half.
# ===========================================================================
#
# Tasks T029 and T030. Everything below runs with no PyFlink installed, no
# broker, and no cluster, which is what makes it the feature's non-skippable
# evidence (plan "Offline testability", research D16). The operator wiring
# that genuinely needs PyFlink lives in test_clip_detector.py and is
# conditional on that optional package; nothing here may depend on it.
#
# References: specs/007-suppress-gift-raid-bursts/data-model.md §3,
# contracts/suppression-events.schema.md §2 and §4, research D4/D5/D7/D13/D16.

GIFT = "community_sub_gift"
SUB_GIFT = "sub_gift"
RAID = "raid"

# Documented notice categories that must never suppress (FR-005, contract
# invariant 3). `unraid`, `sub` and `resub` are named by the spec itself.
EXCLUDED_NOTICE_TYPES = ("unraid", "sub", "resub", "announcement", "bits_badge_tier")

# The four environment variables T046 adds to both Flink blocks.
SUPPRESSION_ENV_VARS = (
    "SUPPRESSION_GIFT_WINDOW_SECONDS",
    "SUPPRESSION_RAID_WINDOW_SECONDS",
    "SUPPRESSION_GATING_ENABLED",
    "SUPPRESSION_DELIVERY_LAG_WARN_SECONDS",
)

MINIMUM_LIFT_ENV_VARS = (
    "DETECTION_MIN_EXCESS_MESSAGES",
    "DETECTION_MIN_EXCESS_GATING_ENABLED",
)

# A sentinel for the record builder: this field is left out of the payload
# entirely, which is a different failure from carrying it as null.
OMIT = object()

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def suppression_record(
    schema_version=1,
    broadcaster_id=123456789,
    notice_type=GIFT,
    occurred_at_ms=1_772_668_800_123,
    **optional,
):
    """One version-1 `suppression-events` value, exactly as the topic carries it.

    Deliberately a second, tiny copy of the builder T002 adds to
    test_clip_detector.py: that file imports clip_detector_job and therefore
    PyFlink, and this file must import neither. Pass OMIT to drop a required
    field, or any keyword to add an optional one.
    """
    payload = {
        "schema_version": schema_version,
        "broadcaster_id": broadcaster_id,
        "notice_type": notice_type,
        "occurred_at_ms": occurred_at_ms,
    }
    payload.update(optional)
    return json.dumps({k: v for k, v in payload.items() if v is not OMIT})


def compose_text():
    return COMPOSE_PATH.read_text(encoding="utf-8")


def compose_service_block(name):
    """The raw text of one top-level service block in docker-compose.yml.

    A text scan, not a YAML parse: this file must not grow a dependency on
    PyYAML to keep the feature's offline evidence unconditional.
    """
    lines = compose_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^  {re.escape(name)}:\s*$", line):
            start = i + 1
            break
    assert start is not None, f"no `{name}:` service in {COMPOSE_PATH}"
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^\S", lines[i]) or re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def compose_env(name):
    """The `- KEY=value` entries of one service, as a mapping."""
    env = {}
    for line in compose_service_block(name).splitlines():
        match = re.match(r"^\s+- ([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            env[match.group(1)] = match.group(2).strip()
    return env


@pytest.fixture
def clean_suppression_env(monkeypatch):
    """No SUPPRESSION_* variable set, so from_env() reports its code defaults.

    The deployed value of SUPPRESSION_GATING_ENABLED is `false` while the code
    default is `true` (research D11), so a test of the default that inherited
    the ambient environment would assert the wrong thing.
    """
    for name in SUPPRESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestSuppressionConfigDefaults:
    """T029. The window policy lives entirely in the consumer's config; the
    topic carries a notice and never a deadline (research D7)."""

    def test_shipped_window_defaults(self, clean_suppression_env):
        config = spike_detector.SuppressionConfig()
        assert config.gift_window_seconds == 120
        assert config.raid_window_seconds == 180

    def test_both_gift_categories_share_the_gift_window(self, clean_suppression_env):
        config = spike_detector.SuppressionConfig()
        assert config.window_for(GIFT) == 120
        assert config.window_for(SUB_GIFT) == 120
        assert config.window_for(RAID) == 180

    def test_gating_defaults_to_true_in_code(self, clean_suppression_env):
        # The kill switch is on by default in code and off in docker-compose.yml,
        # so a deploy is inert until an operator flips the compose value after
        # E1-E3 (research D11, autonomous decision 21).
        assert spike_detector.SuppressionConfig().gating_enabled is True
        assert spike_detector.SuppressionConfig.from_env().gating_enabled is True

    def test_delivery_lag_warning_defaults_to_thirty_seconds(self, clean_suppression_env):
        config = spike_detector.SuppressionConfig()
        assert config.delivery_lag_warn_seconds == 30
        assert spike_detector.SUPPRESSION_DELIVERY_LAG_WARN_SECONDS == 30
        assert config.delivery_lag_warn_seconds == (
            spike_detector.SUPPRESSION_DELIVERY_LAG_WARN_SECONDS
        )

    @pytest.mark.parametrize("notice_type", EXCLUDED_NOTICE_TYPES + ("", "RAID", None))
    def test_excluded_categories_have_no_window(self, notice_type, clean_suppression_env):
        # FR-005 defence in depth: an excluded category is ignored rather than
        # defaulted to a window, at the consumer as well as at the producer.
        assert spike_detector.SuppressionConfig().window_for(notice_type) is None

    def test_the_consumer_allow_list_is_the_contract_triple(self):
        assert spike_detector.SUPPRESSION_TRIGGER_NOTICE_TYPES == (
            "community_sub_gift",
            "sub_gift",
            "raid",
        )

    def test_the_schema_version_this_consumer_accepts(self):
        assert spike_detector.SUPPRESSION_SCHEMA_VERSION == 1

    def test_environment_overrides_every_field(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_GIFT_WINDOW_SECONDS", "45")
        monkeypatch.setenv("SUPPRESSION_RAID_WINDOW_SECONDS", "90")
        monkeypatch.setenv("SUPPRESSION_GATING_ENABLED", "false")
        monkeypatch.setenv("SUPPRESSION_DELIVERY_LAG_WARN_SECONDS", "7")
        config = spike_detector.SuppressionConfig.from_env()
        assert config.gift_window_seconds == 45
        assert config.raid_window_seconds == 90
        assert config.gating_enabled is False
        assert config.delivery_lag_warn_seconds == 7
        assert config.window_for(SUB_GIFT) == 45
        assert config.window_for(RAID) == 90

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True), ("True", True), ("TRUE", True), ("1", True),
            ("yes", True), ("on", True), (" true ", True),
            ("false", False), ("False", False), ("0", False),
            ("no", False), ("off", False), (" false ", False),
        ],
    )
    def test_gating_accepts_the_documented_boolean_spellings(self, raw, expected, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_GATING_ENABLED", raw)
        assert spike_detector.SuppressionConfig.from_env().gating_enabled is expected

    @pytest.mark.parametrize("raw", ["", "maybe", "2", "truthy", "y3s", "none"])
    def test_an_unreadable_boolean_is_rejected_at_start_up(self, raw, monkeypatch):
        # A kill switch that silently reads as its default is worse than a job
        # that refuses to start: the operator believes gating is off while
        # clips are being dropped.
        monkeypatch.setenv("SUPPRESSION_GATING_ENABLED", raw)
        with pytest.raises(ValueError, match="SUPPRESSION_GATING_ENABLED"):
            spike_detector.SuppressionConfig.from_env()

    @pytest.mark.parametrize(
        "name", ["SUPPRESSION_GIFT_WINDOW_SECONDS", "SUPPRESSION_RAID_WINDOW_SECONDS"]
    )
    @pytest.mark.parametrize("raw", ["", "abc", "120.5", "1e2", " "])
    def test_a_non_integer_window_is_rejected(self, name, raw, monkeypatch):
        monkeypatch.setenv(name, raw)
        with pytest.raises(ValueError):
            spike_detector.SuppressionConfig.from_env()

    @pytest.mark.parametrize(
        "name", ["SUPPRESSION_GIFT_WINDOW_SECONDS", "SUPPRESSION_RAID_WINDOW_SECONDS"]
    )
    @pytest.mark.parametrize("raw", ["0", "-1", "-180"])
    def test_a_non_positive_window_is_rejected(self, name, raw, monkeypatch):
        # A zero window would make apply_notice() write a deadline in the past
        # -- state that can never gate anything and only costs a write.
        monkeypatch.setenv(name, raw)
        with pytest.raises(ValueError):
            spike_detector.SuppressionConfig.from_env()

    def test_a_negative_delivery_lag_threshold_is_rejected(self, monkeypatch):
        monkeypatch.setenv("SUPPRESSION_DELIVERY_LAG_WARN_SECONDS", "-1")
        with pytest.raises(ValueError):
            spike_detector.SuppressionConfig.from_env()

    def test_a_zero_delivery_lag_threshold_is_allowed(self, monkeypatch):
        # Degenerate but meaningful: every record with any measurable age is
        # then lagging. It is a tuning choice, not a mistake.
        monkeypatch.setenv("SUPPRESSION_DELIVERY_LAG_WARN_SECONDS", "0")
        assert spike_detector.SuppressionConfig.from_env().delivery_lag_warn_seconds == 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"gift_window_seconds": 0},
            {"gift_window_seconds": -1},
            {"raid_window_seconds": 0},
            {"raid_window_seconds": -180},
            {"delivery_lag_warn_seconds": -1},
        ],
    )
    def test_nonsense_config_is_rejected_at_construction_too(self, kwargs):
        # Same rule as DetectorConfig: fail where the object is built, so the
        # error names the compose value that caused it.
        with pytest.raises(ValueError):
            spike_detector.SuppressionConfig(**kwargs)

    def test_no_viewer_count_anywhere_in_the_config_surface(self):
        # Decision 1 / FR-006: raid audience size never affects the window.
        # The data to revisit that lives on the topic, not in the policy.
        fields = set(spike_detector.SuppressionConfig().__dataclass_fields__)
        assert not any("viewer" in name for name in fields)


class TestSuppressionStateSerialization:
    """T029. AnomalyDetector keeps this in a Types.STRING() ValueState, the
    same way HoldState already travels (data-model §3)."""

    STATE_KWARGS = dict(
        suppress_from_ms=1_772_668_740_123,
        suppress_until_ms=1_772_668_920_123,
        notice_type=RAID,
        notice_at_ms=1_772_668_740_123,
    )

    def test_round_trips_through_json(self):
        state = spike_detector.SuppressionState(**self.STATE_KWARGS)
        assert spike_detector.SuppressionState.from_json(state.to_json()) == state

    def test_newly_written_state_always_carries_its_lower_bound(self):
        """The window is a half-open interval, so the instant it opens is state,
        not something the gate may infer. A writer that omits it would leave the
        reader guessing, and the reader's only safe guess is the whole past."""
        encoded = json.loads(
            spike_detector.SuppressionState(**self.STATE_KWARGS).to_json()
        )
        assert encoded["suppress_from_ms"] == 1_772_668_740_123
        assert set(encoded) == {
            "suppress_from_ms", "suppress_until_ms", "notice_type", "notice_at_ms"
        }

    def test_a_legacy_state_without_a_lower_bound_reads_as_the_notice_time(self):
        """Backward compatibility across a rolling upgrade only. A state string
        written by the pre-lower-bound consumer opened its window at the notice
        it recorded, so notice_at_ms is the correct -- not merely convenient --
        default. It is a read-side allowance: to_json() never omits the field."""
        legacy = json.dumps({
            "suppress_until_ms": 1_772_668_920_123,
            "notice_type": RAID,
            "notice_at_ms": 1_772_668_740_123,
        })
        state = spike_detector.SuppressionState.from_json(legacy)
        assert state.suppress_from_ms == 1_772_668_740_123
        assert state.suppress_until_ms == 1_772_668_920_123

    @pytest.mark.parametrize(
        "suppress_from_ms", ["1772668740123", 1.772e12, True, None, [], {}]
    )
    def test_a_malformed_lower_bound_reads_as_absent_rather_than_raising(
        self, suppress_from_ms
    ):
        """Present-but-unreadable is not the same as absent: only a missing key
        may fall back to notice_at_ms. A wrong type means the string did not
        come from this code, so it fails open like every other unreadable state."""
        encoded = json.dumps({
            "suppress_from_ms": suppress_from_ms,
            "suppress_until_ms": 1_772_668_920_123,
            "notice_type": RAID,
            "notice_at_ms": 1_772_668_740_123,
        })
        assert spike_detector.SuppressionState.from_json(encoded) is None

    def test_absent_state_decodes_to_none(self):
        # ValueState.value() is None when nothing was ever written, and reads
        # back as None under NeverReturnExpired once the TTL passes.
        assert spike_detector.SuppressionState.from_json(None) is None
        assert spike_detector.SuppressionState.from_json("") is None

    @pytest.mark.parametrize(
        "encoded",
        ["{not json", "[]", '"a string"', "3", "null", "{}", '{"suppress_until_ms": 1}'],
    )
    def test_undecodable_state_reads_as_absent_rather_than_raising(self, encoded):
        """Absent and not-suppressed are the same value; there is no third one
        (data-model §3, FR-011). A state string this operator cannot read must
        therefore fail open, not raise out of on_timer and stop chat detection
        for every key on the subtask."""
        assert spike_detector.SuppressionState.from_json(encoded) is None

    def test_the_state_carries_the_diagnostic_fields_the_log_needs(self):
        state = spike_detector.SuppressionState(**self.STATE_KWARGS)
        assert state.suppress_from_ms == 1_772_668_740_123
        assert state.suppress_until_ms == 1_772_668_920_123
        assert state.notice_type == RAID
        assert state.notice_at_ms == 1_772_668_740_123

    def test_the_state_never_carries_a_window_or_a_viewer_count(self):
        # Both ends of the interval are policy OUTPUT, kept as instants;
        # recomputing either from a stored window duration would make a
        # retuning apply retroactively.
        fields = set(spike_detector.SuppressionState(**self.STATE_KWARGS).__dataclass_fields__)
        assert fields == {
            "suppress_from_ms", "suppress_until_ms", "notice_type", "notice_at_ms"
        }


class TestSuppressionSourceSettings:
    """T029. Every value the sparse second source depends on, asserted with no
    PyFlink import (research §4.7, D16). clip_detector_job.py builds the real
    KafkaSource and WatermarkStrategy from this construct."""

    def test_topic_and_offset_mode(self):
        settings = spike_detector.SuppressionSourceSettings()
        assert settings.topic == "suppression-events"
        # earliest() would feed hours-old occurred_at_ms into event time and
        # pin the operator watermark in the past (research D4).
        assert settings.starting_offsets == "latest"

    def test_out_of_orderness_is_shared_with_the_chat_stream(self):
        settings = spike_detector.SuppressionSourceSettings()
        assert settings.out_of_orderness_seconds == WATERMARK_OUT_OF_ORDERNESS_SECONDS
        assert settings.out_of_orderness_seconds == 2

    def test_idleness_is_strictly_below_the_chat_streams(self):
        """I15: the suppression input must never be the binding watermark
        minimum in steady state, so it is released first."""
        settings = spike_detector.SuppressionSourceSettings()
        assert spike_detector.SUPPRESSION_IDLENESS_SECONDS == 5
        assert settings.idleness_seconds == 5
        assert WATERMARK_IDLENESS_SECONDS == 10
        assert settings.idleness_seconds < WATERMARK_IDLENESS_SECONDS

    def test_partitions_equal_parallelism(self):
        # One split per source subtask, so split idleness is well defined
        # (research §4.1, contract §1).
        settings = spike_detector.SuppressionSourceSettings()
        assert settings.expected_partitions == 4
        assert settings.expected_parallelism == 4
        assert settings.expected_partitions == settings.expected_parallelism

    def test_the_pure_delivery_and_gating_fields(self):
        settings = spike_detector.SuppressionSourceSettings()
        assert settings.delivery_lag_warn_seconds == 30
        # The value docker-compose.yml checks in, which is deliberately NOT the
        # SuppressionConfig code default (research D11).
        assert settings.checked_in_gating_enabled is False
        assert spike_detector.SuppressionConfig().gating_enabled is True
        assert settings.checked_in_gating_enabled != (
            spike_detector.SuppressionConfig().gating_enabled
        )

    def test_the_idle_re_entry_bound_stays_far_below_the_shortest_window(self):
        """I16 / R10: a long-idle subtask that becomes active again holds the
        two-input watermark for at most idleness + out-of-orderness."""
        settings = spike_detector.SuppressionSourceSettings()
        bound = settings.idleness_seconds + settings.out_of_orderness_seconds
        assert bound == 7
        assert bound < spike_detector.SuppressionConfig().gift_window_seconds / 10

    def test_the_settings_module_imports_no_pyflink(self):
        """The whole point of the construct: this evidence must never be
        conditional on an optional package being installed. (The word appears
        in the module's prose, so match the import statement itself.)"""
        source = Path(spike_detector.__file__).read_text(encoding="utf-8")
        assert re.search(r"^\s*(import|from)\s+pyflink", source, re.MULTILINE) is None


class TestDockerComposeSuppressionWiring:
    """T029, closed by T046. These assertions are authored during US4 and are
    expected to fail until T046 writes both Flink environment blocks -- the
    tasks file makes T046 a hard closure dependency for T029 for exactly this
    reason. Static text assertions only: nothing here starts a container."""

    @pytest.mark.parametrize("service", ["flink-jobmanager", "flink-taskmanager"])
    def test_both_flink_blocks_declare_minimum_lift_shadow_mode(self, service):
        env = compose_env(service)
        defaults = DetectorConfig()
        assert env["DETECTION_MIN_EXCESS_MESSAGES"] == str(
            defaults.min_excess_messages
        )
        assert env["DETECTION_MIN_EXCESS_GATING_ENABLED"] == "false"
        assert defaults.min_excess_gating_enabled is True

    def test_the_two_flink_blocks_agree_on_minimum_lift(self):
        jobmanager = compose_env("flink-jobmanager")
        taskmanager = compose_env("flink-taskmanager")
        for name in MINIMUM_LIFT_ENV_VARS:
            assert jobmanager.get(name) == taskmanager.get(name), name
            assert jobmanager.get(name) is not None, name

    def test_the_suppression_topic_is_created_with_four_partitions(self):
        init = compose_service_block("kafka-init")
        assert (
            "--topic suppression-events --partitions 4 --replication-factor 1 "
            "--config retention.ms=3600000" in init
        )
        settings = spike_detector.SuppressionSourceSettings()
        assert f"--topic {settings.topic} --partitions {settings.expected_partitions} " in init
        # The same partitions-equal-parallelism rule chat-messages already
        # documents; they must move together (contract §6).
        assert "--topic chat-messages --partitions 4 " in init

    @pytest.mark.parametrize("service", ["flink-jobmanager", "flink-taskmanager"])
    def test_both_flink_blocks_declare_the_windows(self, service):
        env = compose_env(service)
        defaults = spike_detector.SuppressionConfig()
        assert env.get("SUPPRESSION_GIFT_WINDOW_SECONDS") == "120"
        assert env.get("SUPPRESSION_RAID_WINDOW_SECONDS") == "180"
        assert env["SUPPRESSION_GIFT_WINDOW_SECONDS"] == str(defaults.gift_window_seconds)
        assert env["SUPPRESSION_RAID_WINDOW_SECONDS"] == str(defaults.raid_window_seconds)

    @pytest.mark.parametrize("service", ["flink-jobmanager", "flink-taskmanager"])
    def test_both_flink_blocks_check_in_gating_disabled(self, service):
        # D11 / decision 21: the checked-in value is false even though the code
        # default is true, so deploying the feature changes no clip behaviour
        # until an operator flips it after E1, E2a, E2b, and E3 pass.
        env = compose_env(service)
        assert env.get("SUPPRESSION_GATING_ENABLED") == "false"
        settings = spike_detector.SuppressionSourceSettings()
        assert (env["SUPPRESSION_GATING_ENABLED"] == "true") == (
            settings.checked_in_gating_enabled
        )

    @pytest.mark.parametrize("service", ["flink-jobmanager", "flink-taskmanager"])
    def test_both_flink_blocks_set_the_delivery_lag_warning(self, service):
        env = compose_env(service)
        settings = spike_detector.SuppressionSourceSettings()
        assert env.get("SUPPRESSION_DELIVERY_LAG_WARN_SECONDS") == "30"
        assert env["SUPPRESSION_DELIVERY_LAG_WARN_SECONDS"] == str(
            settings.delivery_lag_warn_seconds
        )

    def test_the_two_flink_blocks_agree(self):
        """The DETECTION_* variables are duplicated across both blocks; a
        SUPPRESSION_* value set on only one of them would make the jobmanager
        and the taskmanager run different policy."""
        jobmanager = compose_env("flink-jobmanager")
        taskmanager = compose_env("flink-taskmanager")
        for name in SUPPRESSION_ENV_VARS:
            assert jobmanager.get(name) == taskmanager.get(name), name
            assert jobmanager.get(name) is not None, name

    def test_flink_pyfiles_is_unchanged(self):
        """The structure decision put the suppression arithmetic in
        spike_detector.py precisely so no new -pyFiles entry and no new
        bind-mount are needed. A new module here is the wiring hazard the plan
        avoids (OPERATIONS.md "Adding a new Python module")."""
        expected = (
            "/opt/flink/usrlib/spike_detector.py,"
            "/opt/flink/usrlib/token_manager.py,"
            "/opt/flink/usrlib/clip_attempt.py"
        )
        assert compose_env("flink-jobmanager")["FLINK_PYFILES"] == expected
        assert compose_text().count("FLINK_PYFILES=") == 1


class TestApplyNotice:
    """T030. data-model §3.1: a monotone max-register, and nothing else. Every
    duplicate, ordering and overlap property below falls out of max() alone."""

    @pytest.fixture
    def config(self, clean_suppression_env):
        return spike_detector.SuppressionConfig()

    def test_a_gift_notice_opens_a_120_second_window(self, config):
        state = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        assert state.suppress_from_ms == 1_000_000
        assert state.suppress_until_ms == 1_000_000 + 120_000
        assert state.notice_type == GIFT
        assert state.notice_at_ms == 1_000_000

    def test_a_sub_gift_notice_uses_the_same_gift_window(self, config):
        state = spike_detector.apply_notice(None, SUB_GIFT, 1_000_000, config)
        assert state.suppress_until_ms == 1_000_000 + 120_000

    def test_a_raid_notice_opens_a_180_second_window(self, config):
        state = spike_detector.apply_notice(None, RAID, 1_000_000, config)
        assert state.suppress_until_ms == 1_000_000 + 180_000
        assert state.notice_type == RAID

    def test_operator_configured_windows_are_honoured(self, config):
        tuned = replace(config, gift_window_seconds=30, raid_window_seconds=45)
        assert spike_detector.apply_notice(None, GIFT, 1_000, tuned).suppress_until_ms == 31_000
        assert spike_detector.apply_notice(None, RAID, 1_000, tuned).suppress_until_ms == 46_000

    def test_a_later_candidate_extends_the_deadline(self, config):
        """US4-1: a second burst must not be exposed by the first window ending."""
        first = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        second = spike_detector.apply_notice(first, GIFT, 1_060_000, config)
        assert second.suppress_until_ms == 1_060_000 + 120_000
        assert second.notice_at_ms == 1_060_000
        # The extension moves the far end only. The near end is where the
        # suppressed period actually began, and it never moves forward.
        assert second.suppress_from_ms == 1_000_000

    def test_an_earlier_candidate_never_moves_the_deadline_backward(self, config):
        """US4-2 / I6. Returning the same object is what lets the operator
        write state only on extension (contract §4.1 rule 6)."""
        state = spike_detector.apply_notice(None, RAID, 1_000_000, config)
        unchanged = spike_detector.apply_notice(state, GIFT, 1_000_000, config)
        assert unchanged is state
        assert unchanged.suppress_until_ms == 1_180_000

    def test_an_equal_candidate_is_a_no_op(self, config):
        state = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        assert spike_detector.apply_notice(state, GIFT, 1_000_000, config) is state

    def test_a_notice_exactly_at_the_deadline_extends(self, config):
        """Spec edge case "exactly at the deadline": the candidate is strictly
        greater than the current deadline, so it extends."""
        state = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        at_deadline = spike_detector.apply_notice(state, GIFT, state.suppress_until_ms, config)
        assert at_deadline.suppress_until_ms == 1_120_000 + 120_000

    def test_the_same_notice_twice_is_idempotent(self, config):
        """FR-010. The contract forbids a de-dup cache precisely because max()
        already makes redelivery free (contract §2.2, notice_id)."""
        once = spike_detector.apply_notice(None, RAID, 1_000_000, config)
        twice = spike_detector.apply_notice(once, RAID, 1_000_000, config)
        assert twice is once

    def test_any_arrival_order_gives_the_same_deadline(self, config):
        """I7 / contract invariant 5."""
        notices = [(GIFT, 1_000_000), (RAID, 1_010_000), (SUB_GIFT, 1_005_000)]
        deadlines = set()
        for order in itertools.permutations(notices):
            state = None
            for notice_type, occurred_at in order:
                state = spike_detector.apply_notice(state, notice_type, occurred_at, config)
            deadlines.add(state.suppress_until_ms)
        assert deadlines == {1_010_000 + 180_000}

    def test_simultaneous_gift_and_raid_leave_the_later_candidate_winning(self, config):
        """US4-3 / SC-005: overlapping categories with different windows."""
        gift_first = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        gift_first = spike_detector.apply_notice(gift_first, RAID, 1_000_000, config)
        raid_first = spike_detector.apply_notice(None, RAID, 1_000_000, config)
        raid_first = spike_detector.apply_notice(raid_first, GIFT, 1_000_000, config)
        assert gift_first.suppress_until_ms == raid_first.suppress_until_ms == 1_180_000
        assert gift_first.notice_type == raid_first.notice_type == RAID

    @pytest.mark.parametrize("notice_type", EXCLUDED_NOTICE_TYPES + ("", None, "RAID"))
    def test_an_excluded_category_creates_no_state(self, notice_type, config):
        """I8 / FR-005. No window_for entry means the notice is ignored, not
        defaulted to some window."""
        assert spike_detector.apply_notice(None, notice_type, 1_000_000, config) is None

    @pytest.mark.parametrize("notice_type", EXCLUDED_NOTICE_TYPES)
    def test_an_excluded_category_never_moves_an_existing_deadline(self, notice_type, config):
        state = spike_detector.apply_notice(None, GIFT, 1_000_000, config)
        assert spike_detector.apply_notice(state, notice_type, 9_000_000, config) is state

    def test_the_deadline_is_non_decreasing_under_any_multiset(self, config):
        """I6, asserted over a deliberately jumbled batch rather than a pair."""
        state = None
        seen = 0
        for occurred_at in [1_050_000, 1_000_000, 1_200_000, 1_100_000, 1_000_000]:
            for notice_type in (GIFT, RAID, SUB_GIFT):
                state = spike_detector.apply_notice(state, notice_type, occurred_at, config)
                assert state.suppress_until_ms >= seen
                seen = state.suppress_until_ms
        assert seen == 1_200_000 + 180_000

    def test_viewer_count_is_not_an_argument(self, config):
        """FR-006 structurally: raid audience size cannot reach the arithmetic
        because the arithmetic never accepts it."""
        params = inspect.signature(spike_detector.apply_notice).parameters
        assert not any("viewer" in name for name in params)
        assert list(params) == ["state", "notice_type", "occurred_at_ms", "config"]


class TestSuppressionWindowLowerBound:
    """T030 regression. The window is the half-open interval
    `[occurred_at_ms, occurred_at_ms + window)`, not "everything before the
    deadline".

    Without a stored lower bound the state says only when suppression ENDS, so
    the gate reads every instant in recorded history as suppressed. That is not
    a theoretical gap: on_timer reports a peak up to `hold_cap_seconds` after it
    happened, and a notice that lands during the hold would then retroactively
    gate a spike that peaked before the gift or raid existed -- suppressing a
    clip the burst did not cause (FR-006, FR-007, FR-018, research D5).
    """

    NOTICE_MS = 1_000_000            # epoch ms; second 1000 on the Twitch clock
    NOTICE_SECOND = 1_000
    DEADLINE_MS = 1_120_000          # + the 120 s gift window
    DEADLINE_SECOND = 1_120

    @pytest.fixture
    def config(self, clean_suppression_env):
        return spike_detector.SuppressionConfig()

    @pytest.fixture
    def state(self, config):
        return spike_detector.apply_notice(None, GIFT, self.NOTICE_MS, config)

    def test_a_first_notice_opens_a_half_open_interval_at_its_occurrence(self, state):
        assert state.suppress_from_ms == self.NOTICE_MS
        assert state.suppress_until_ms == self.DEADLINE_MS

    def test_a_peak_one_second_before_the_notice_is_not_suppressed(self, state):
        """The burst that peaked here cannot have been caused by a gift that had
        not happened yet, and FR-018 forbids the notice reaching backwards."""
        assert spike_detector.is_suppressed(state, self.NOTICE_SECOND - 1) is False

    def test_a_peak_exactly_at_the_notice_is_suppressed(self, state):
        """The lower bound is inclusive: the notice second is inside its own
        window, which is where the gift or raid burst actually starts."""
        assert spike_detector.is_suppressed(state, self.NOTICE_SECOND) is True

    def test_a_peak_just_before_the_deadline_is_suppressed(self, state):
        assert spike_detector.is_suppressed(state, self.DEADLINE_SECOND - 1) is True

    def test_a_peak_exactly_at_the_deadline_is_not_suppressed(self, state):
        """The upper bound stays exclusive, so two adjacent windows neither
        double-count nor leave a gap."""
        assert spike_detector.is_suppressed(state, self.DEADLINE_SECOND) is False

    def test_the_whole_recorded_past_is_not_suppressed(self, state):
        """The bug, stated as its own assertion: a deadline alone would gate
        every peak from the epoch onward."""
        for peak_second in (0, 1, 500, self.NOTICE_SECOND - 120, self.NOTICE_SECOND - 1):
            assert spike_detector.is_suppressed(state, peak_second) is False

    def test_an_overlapping_notice_extends_the_far_end_and_keeps_the_near_one(
        self, state, config
    ):
        """US4-1 with both ends: the second notice arrives inside the first
        window, so the two intervals are one continuous suppressed period that
        began at the first notice."""
        extended = spike_detector.apply_notice(state, RAID, 1_060_000, config)
        assert extended.suppress_until_ms == 1_060_000 + 180_000
        assert extended.suppress_from_ms == self.NOTICE_MS
        assert extended.notice_at_ms == 1_060_000
        # And the extension still cannot reach behind the first notice.
        assert spike_detector.is_suppressed(extended, self.NOTICE_SECOND - 1) is False
        assert spike_detector.is_suppressed(extended, self.NOTICE_SECOND) is True

    def test_an_earlier_or_equal_notice_is_still_a_no_op_object(self, state, config):
        """Write-on-change is unchanged by the lower bound: a notice that moves
        neither end returns the SAME object, so the operator writes no state
        (contract §4.1 rule 6, FR-010)."""
        assert spike_detector.apply_notice(state, GIFT, self.NOTICE_MS, config) is state
        assert spike_detector.apply_notice(state, GIFT, 940_000, config) is state
        assert spike_detector.apply_notice(state, RAID, 900_000, config) is state

    def test_a_notice_at_the_expired_deadline_starts_a_new_interval(self, state, config):
        """Spec edge case "exactly at the deadline". The old window is closed at
        that instant -- is_suppressed() is already False there -- so the new
        state describes the new burst, not a merged interval reaching back to a
        window that has ended."""
        renewed = spike_detector.apply_notice(state, GIFT, self.DEADLINE_MS, config)
        assert renewed.suppress_from_ms == self.DEADLINE_MS
        assert renewed.suppress_until_ms == self.DEADLINE_MS + 120_000
        assert spike_detector.is_suppressed(renewed, self.DEADLINE_SECOND - 1) is False

    def test_a_notice_after_an_expired_deadline_starts_a_new_interval(self, state, config):
        renewed = spike_detector.apply_notice(state, RAID, 2_000_000, config)
        assert renewed.suppress_from_ms == 2_000_000
        assert renewed.suppress_until_ms == 2_180_000
        # The gap between the two windows is not suppressed by either of them.
        assert spike_detector.is_suppressed(renewed, 1_500) is False

    def test_duplicates_and_simultaneous_categories_stay_deterministic(self, config):
        """FR-010 / I7 with the near end included. The same notice repeated, and
        a gift and a raid at one instant, give one interval in any order."""
        intervals = set()
        for order in itertools.permutations(
            [(GIFT, self.NOTICE_MS), (RAID, self.NOTICE_MS), (GIFT, self.NOTICE_MS)]
        ):
            folded = None
            for notice_type, occurred_at in order:
                folded = spike_detector.apply_notice(folded, notice_type, occurred_at, config)
            intervals.add((folded.suppress_from_ms, folded.suppress_until_ms))
        assert intervals == {(self.NOTICE_MS, self.NOTICE_MS + 180_000)}

    def test_the_interval_is_well_formed_under_any_arrival_order(self, config):
        """The order-independent guarantee for a mixed batch: the deadline is
        the same in every order (the existing I7 property), and the interval is
        never inverted and never opens before the earliest notice applied."""
        notices = [(GIFT, 1_000_000), (RAID, 1_010_000), (SUB_GIFT, 1_005_000)]
        deadlines = set()
        for order in itertools.permutations(notices):
            folded = None
            for notice_type, occurred_at in order:
                folded = spike_detector.apply_notice(folded, notice_type, occurred_at, config)
            deadlines.add(folded.suppress_until_ms)
            assert folded.suppress_from_ms < folded.suppress_until_ms
            assert folded.suppress_from_ms >= min(occurred for _, occurred in notices)
            assert folded.suppress_from_ms <= folded.notice_at_ms
        assert deadlines == {1_010_000 + 180_000}

    def test_an_excluded_category_moves_neither_end(self, state, config):
        for notice_type in EXCLUDED_NOTICE_TYPES:
            assert spike_detector.apply_notice(state, notice_type, 1_050_000, config) is state


class TestIsSuppressed:
    """T030, data-model §3.2. The predicate is two arguments and no clock: the
    kill switch and the metric belong to the operator, not to the arithmetic."""

    @pytest.fixture
    def state(self):
        return spike_detector.SuppressionState(
            suppress_from_ms=880_000,
            suppress_until_ms=1_000_000,
            notice_type=GIFT,
            notice_at_ms=880_000,
        )

    def test_absent_state_fails_open(self):
        """FR-011 / I10: absent, never-written and expired all read the same,
        and none of them blocks emission."""
        assert spike_detector.is_suppressed(None, 999) is False

    def test_a_peak_inside_the_window_is_suppressed(self, state):
        assert spike_detector.is_suppressed(state, 999) is True

    def test_the_deadline_boundary_is_strict(self, state):
        """peak_second * 1000 < suppress_until_ms. A peak exactly at the
        deadline is outside the window."""
        assert spike_detector.is_suppressed(state, 1000) is False
        assert spike_detector.is_suppressed(state, 999) is True

    def test_a_peak_after_the_window_is_not_suppressed(self, state):
        assert spike_detector.is_suppressed(state, 1001) is False

    def test_the_gate_reads_the_peak_second_not_the_report_second(self, state):
        """Research D5: a burst that peaks inside the window must not escape by
        being reported hold_cap_seconds later. These two calls are the two
        candidate inputs, and only the first is the one the operator passes."""
        peak_second, report_second = 999, 999 + DetectorConfig().hold_cap_seconds
        assert spike_detector.is_suppressed(state, peak_second) is True
        assert spike_detector.is_suppressed(state, report_second) is False

    def test_a_peak_before_the_window_opened_is_not_suppressed(self, state):
        """Replaces the older assertion that a negative peak second suppresses.
        That was only ever true because the predicate had no lower bound: a peak
        at second -5, or at any second before the notice, precedes the gift or
        raid that opened the window and must clip normally (FR-006, FR-018)."""
        assert spike_detector.is_suppressed(state, -5) is False
        assert spike_detector.is_suppressed(state, 0) is False
        assert spike_detector.is_suppressed(state, 879) is False
        assert spike_detector.is_suppressed(state, 880) is True

    @pytest.mark.parametrize("peak_second", [None, "999", 999.0, True, object()])
    def test_an_unreadable_peak_second_fails_open(self, state, peak_second):
        """Unchanged fail-open expectation for invalid input. The predicate runs
        inside on_timer, where refusing to answer would cost every key on the
        subtask, so a peak it cannot read is not suppressed (FR-011)."""
        assert spike_detector.is_suppressed(state, peak_second) is False

    def test_a_zero_deadline_never_suppresses(self):
        """The absent-state equivalent written out: current_ms defaults to 0 in
        apply_notice(), so a 0 interval must gate no real peak."""
        zero = spike_detector.SuppressionState(
            suppress_from_ms=0, suppress_until_ms=0, notice_type=GIFT, notice_at_ms=0
        )
        assert spike_detector.is_suppressed(zero, 0) is False
        assert spike_detector.is_suppressed(zero, 1) is False

    def test_the_predicate_takes_no_config_and_no_clock(self):
        params = list(inspect.signature(spike_detector.is_suppressed).parameters)
        assert params == ["state", "peak_second"]


class TestSuppressionRecordDecoding:
    """T029/T037's guaranteed-offline half. Contract §4.1 and §4.0: the
    consumer deserializes values only, so payload validation is the only
    defence it has, and a rejection is a typed reason rather than an
    exception -- an exception out of process_element2 would stop chat
    detection for every key on the subtask."""

    @pytest.fixture
    def config(self, clean_suppression_env):
        return spike_detector.SuppressionConfig()

    def decode(self, raw, config):
        return spike_detector.decode_suppression_record(raw, config)

    def test_a_valid_record_decodes_to_the_three_contract_fields(self, config):
        result = self.decode(suppression_record(), config)
        assert result.rejected_reason is None
        assert result.notice.broadcaster_id == 123456789
        assert result.notice.notice_type == GIFT
        assert result.notice.occurred_at_ms == 1_772_668_800_123

    @pytest.mark.parametrize("notice_type", [GIFT, SUB_GIFT, RAID])
    def test_every_trigger_category_decodes(self, notice_type, config):
        result = self.decode(suppression_record(notice_type=notice_type), config)
        assert result.rejected_reason is None
        assert result.notice.notice_type == notice_type

    def test_the_optional_fields_are_accepted_and_ignored(self, config):
        """Contract §2.2: notice_id, received_at_ms and viewer_count are
        diagnostic only. They must not appear on the decoded notice, so no
        consumer logic can come to depend on them."""
        result = self.decode(
            suppression_record(
                notice_type=RAID,
                notice_id="9c2b1f4e-a1",
                received_at_ms=1_772_668_800_298,
                viewer_count=4200,
            ),
            config,
        )
        assert result.rejected_reason is None
        fields = set(result.notice.__dataclass_fields__)
        assert fields == {"broadcaster_id", "notice_type", "occurred_at_ms"}

    def test_viewer_count_changes_nothing(self, config):
        """Contract §2.2 asks for exactly this test."""
        low = self.decode(suppression_record(notice_type=RAID, viewer_count=1), config)
        high = self.decode(suppression_record(notice_type=RAID, viewer_count=90_000), config)
        absent = self.decode(suppression_record(notice_type=RAID), config)
        assert low.notice == high.notice == absent.notice
        assert (
            spike_detector.apply_notice(None, low.notice.notice_type,
                                        low.notice.occurred_at_ms, config)
            == spike_detector.apply_notice(None, high.notice.notice_type,
                                           high.notice.occurred_at_ms, config)
        )

    def test_unknown_keys_are_ignored_so_a_new_optional_field_stays_safe(self, config):
        """Contract §6: adding an optional field keeps schema_version 1."""
        result = self.decode(suppression_record(some_future_field={"a": 1}), config)
        assert result.rejected_reason is None
        assert result.notice.broadcaster_id == 123456789

    @pytest.mark.parametrize(
        "raw", ["", "   ", "{not json", "{", '{"a": ', "\x00", "not json at all"]
    )
    def test_undecodable_json_is_rejected_as_decode(self, raw, config):
        result = self.decode(raw, config)
        assert result.notice is None
        assert result.rejected_reason == "decode"

    @pytest.mark.parametrize("raw", ["[]", '["a"]', '"a string"', "3", "3.5", "true", "null"])
    def test_json_that_is_not_an_object_is_rejected_as_decode(self, raw, config):
        result = self.decode(raw, config)
        assert result.notice is None
        assert result.rejected_reason == "decode"

    @pytest.mark.parametrize("schema_version", [OMIT, None, 0, 2, 99, "1", 1.0, True])
    def test_a_missing_or_unknown_schema_version_is_rejected(self, schema_version, config):
        """Contract §4.1 rule 2. `True` is in this list on purpose: bool is a
        subclass of int and True == 1, so a naive check accepts it."""
        result = self.decode(suppression_record(schema_version=schema_version), config)
        assert result.notice is None
        assert result.rejected_reason == "schema_version"

    @pytest.mark.parametrize(
        "broadcaster_id", [OMIT, None, "123456789", 123.0, True, False, [1], {"id": 1}]
    )
    def test_a_malformed_broadcaster_id_is_rejected_as_fields(self, broadcaster_id, config):
        result = self.decode(suppression_record(broadcaster_id=broadcaster_id), config)
        assert result.notice is None
        assert result.rejected_reason == "fields"

    @pytest.mark.parametrize(
        "occurred_at_ms",
        [OMIT, None, "1772668800123", 1772668800123.0, True, [1], {"ms": 1}],
    )
    def test_a_malformed_occurred_at_is_rejected_as_fields(self, occurred_at_ms, config):
        result = self.decode(suppression_record(occurred_at_ms=occurred_at_ms), config)
        assert result.notice is None
        assert result.rejected_reason == "fields"

    @pytest.mark.parametrize(
        "notice_type", list(EXCLUDED_NOTICE_TYPES) + [OMIT, None, "", "RAID", 7, ["raid"]]
    )
    def test_an_excluded_or_malformed_category_is_rejected_as_fields(self, notice_type, config):
        """Contract §4.1 rule 3: a category outside the window map is ignored
        rather than defaulted to a window."""
        result = self.decode(suppression_record(notice_type=notice_type), config)
        assert result.notice is None
        assert result.rejected_reason == "fields"

    def test_every_rejection_reason_is_from_the_bounded_set(self, config):
        """The reason becomes a Prometheus label, so its cardinality is fixed
        by construction (T042)."""
        assert spike_detector.SUPPRESSION_REJECT_REASONS == ("decode", "schema_version", "fields")
        for raw in ["{oops", suppression_record(schema_version=2),
                    suppression_record(broadcaster_id="x")]:
            assert self.decode(raw, config).rejected_reason in (
                spike_detector.SUPPRESSION_REJECT_REASONS
            )

    def test_the_decoder_never_raises(self, config):
        """Contract §4.1 rule 1, restated as the property that matters: an
        exception out of process_element2 would fail the operator and stop chat
        detection for every key on that subtask."""
        hostile = [
            "",
            "{",
            "[]",
            "null",
            "{}",
            '{"schema_version": {"a": [1, 2]}}',
            json.dumps({"schema_version": 1, "broadcaster_id": {"a": 1}}),
            json.dumps({"schema_version": 1, "broadcaster_id": 1, "notice_type": {},
                        "occurred_at_ms": []}),
            json.dumps([{"schema_version": 1}]),
        ]
        for raw in hostile:
            result = self.decode(raw, config)
            assert result.notice is None
            assert result.rejected_reason in spike_detector.SUPPRESSION_REJECT_REASONS

    def test_the_decoder_sees_only_the_value(self, config):
        """Contract §4.0 / research D15: the job's sources deserialize values
        only, so no consumer function may take a Kafka key. Routing, keying and
        state all come from the payload broadcaster_id."""
        params = list(inspect.signature(spike_detector.decode_suppression_record).parameters)
        assert params == ["value", "config"]
        assert not any("key" in name for name in params)

    def test_the_decoder_applies_no_window_policy(self, config):
        """Research D7: the topic carries a notice, never a decision."""
        notice = self.decode(suppression_record(), config).notice
        assert not hasattr(notice, "suppress_until_ms")
        assert not hasattr(notice, "window_seconds")

    def test_a_tuned_window_does_not_change_what_decodes(self, config):
        tuned = replace(config, gift_window_seconds=1, raid_window_seconds=2)
        assert self.decode(suppression_record(), tuned).notice == (
            self.decode(suppression_record(), config).notice
        )
        assert self.decode(suppression_record(notice_type="unraid"), tuned).rejected_reason == (
            "fields"
        )


class TestDeliveryAgeObservation:
    """T029/T039's guaranteed-offline half. I19 / research D13: one clamped
    value, computed at receipt from the consumer clock, is the only
    classification input."""

    @pytest.fixture
    def config(self, clean_suppression_env):
        return spike_detector.SuppressionConfig()

    def observe(self, occurred_at_ms, consumer_receipt_ms, config):
        return spike_detector.observe_delivery_age(occurred_at_ms, consumer_receipt_ms, config)

    def test_the_age_is_receipt_minus_occurrence(self, config):
        observation = self.observe(1_000_000, 1_000_250, config)
        assert observation.delivery_age_ms == 250
        assert observation.delivery_age_seconds == pytest.approx(0.25)

    def test_a_fresh_record_is_healthy(self, config):
        assert self.observe(1_000_000, 1_000_250, config).lag_class == "healthy"

    def test_exactly_the_threshold_is_healthy(self, config):
        """Contract §4.1 rule 5 says at-or-below, so 30.000s is not lagging."""
        assert self.observe(1_000_000, 1_030_000, config).lag_class == "healthy"

    def test_one_millisecond_past_the_threshold_is_lagging(self, config):
        assert self.observe(1_000_000, 1_030_001, config).lag_class == "lagging"

    def test_the_threshold_follows_the_configured_value(self, config):
        tuned = replace(config, delivery_lag_warn_seconds=5)
        assert self.observe(1_000_000, 1_005_000, tuned).lag_class == "healthy"
        assert self.observe(1_000_000, 1_005_001, tuned).lag_class == "lagging"

    def test_a_negative_raw_age_is_clamped_and_flagged_as_skew(self, config):
        """Clamped for both observation and classification, and diagnosable --
        but without a second metric (research D13)."""
        observation = self.observe(1_000_000, 999_000, config)
        assert observation.delivery_age_ms == 0
        assert observation.delivery_age_seconds == 0.0
        assert observation.lag_class == "healthy"
        assert observation.clock_skew is True

    def test_a_non_negative_age_is_not_flagged_as_skew(self, config):
        assert self.observe(1_000_000, 1_000_000, config).clock_skew is False
        assert self.observe(1_000_000, 1_060_000, config).clock_skew is False

    def test_a_slow_consumer_lags_even_when_the_producer_was_fast(self, config):
        """The case NFR-005 turns on: Twitch-to-producer latency is 175ms, but
        the record reaches process_element2 45s after it occurred. Only the
        consumer-receipt age may decide, so this is lagging."""
        occurred_at_ms = 1_772_668_800_123
        received_at_ms = occurred_at_ms + 175          # diagnostic only
        consumer_receipt_ms = occurred_at_ms + 45_000
        observation = self.observe(occurred_at_ms, consumer_receipt_ms, config)
        assert observation.lag_class == "lagging"
        assert received_at_ms - occurred_at_ms < config.delivery_lag_warn_seconds * 1000

    def test_the_optional_producer_clock_cannot_reach_the_classification(self, config):
        """Contract §2.2: no consumer logic may depend on received_at_ms being
        present, so it is not an argument at all."""
        params = list(inspect.signature(spike_detector.observe_delivery_age).parameters)
        assert params == ["occurred_at_ms", "consumer_receipt_ms", "config"]
        assert not any("received" in name for name in params)

    def test_silence_has_no_classification_of_its_own(self, config):
        """I19 / decision 20: a window with no record is idle/unknown, which is
        read as the absence of samples. There is deliberately no third class to
        publish and no way to observe without a record."""
        assert spike_detector.SUPPRESSION_LAG_CLASSES == ("healthy", "lagging")
        with pytest.raises(TypeError):
            spike_detector.observe_delivery_age()

    def test_the_observation_carries_nothing_else(self, config):
        fields = set(self.observe(1_000_000, 1_000_250, config).__dataclass_fields__)
        assert fields == {"delivery_age_ms", "lag_class", "clock_skew"}


class TestTrustworthyNoticeTime:
    """T030 regression. A notice whose `occurred_at_ms` is far in the future
    must never reach the monotone register.

    `apply_notice()` only ever moves the deadline outward, so one record
    claiming to have occurred in the year 2286 -- a producer bug, a
    microsecond value mistaken for milliseconds, a badly set clock, or a record
    written by something other than this producer -- would pin
    `suppress_until_ms` beyond every later notice and silence that channel's
    clips until the state's TTL expired. Nothing downstream can undo it: the
    register has no path that moves a deadline backward, by design (I6).

    FR-017 is the rule this enforces: an untrustworthy occurrence time must
    produce no deadline and must be operationally visible instead. Small
    future skew is NOT untrustworthy -- two clocks a few seconds apart are
    ordinary, and clamping already covers it (contract §2.2) -- so the
    allowance is bounded rather than zero.
    """

    RECEIPT_MS = 1_772_668_800_123

    @pytest.fixture
    def config(self, clean_suppression_env):
        return spike_detector.SuppressionConfig()

    def test_the_allowance_is_a_pinned_pure_constant(self):
        assert spike_detector.SUPPRESSION_MAX_FUTURE_SKEW_SECONDS == 30

    def test_the_helper_is_small_and_explicit(self):
        params = inspect.signature(spike_detector.is_trustworthy_notice_time).parameters
        assert list(params) == [
            "occurred_at_ms", "consumer_receipt_ms", "max_future_skew_seconds"
        ]
        assert params["max_future_skew_seconds"].default == 30
        assert params["max_future_skew_seconds"].default == (
            spike_detector.SUPPRESSION_MAX_FUTURE_SKEW_SECONDS
        )

    @pytest.mark.parametrize(
        "occurred_at_ms",
        [
            0,                          # the epoch: old, but not untrustworthy
            RECEIPT_MS - 3_600_000,     # an hour late; contract §4.1 rule 8 applies it
            RECEIPT_MS - 1,
            RECEIPT_MS,
            RECEIPT_MS + 1,             # ordinary clock disagreement
            RECEIPT_MS + 5_000,
            RECEIPT_MS + 29_999,
            RECEIPT_MS + 30_000,        # exactly the allowance
        ],
    )
    def test_a_notice_within_the_allowance_is_trustworthy(self, occurred_at_ms):
        """Lateness is never an error here, and a small future skew is not one
        either: the delivery-age clamp is the response to it, not a rejection."""
        assert spike_detector.is_trustworthy_notice_time(
            occurred_at_ms, self.RECEIPT_MS
        ) is True

    def test_one_millisecond_beyond_the_allowance_is_rejected(self):
        assert spike_detector.is_trustworthy_notice_time(
            self.RECEIPT_MS + 30_001, self.RECEIPT_MS
        ) is False

    @pytest.mark.parametrize(
        "occurred_at_ms",
        [
            1_772_668_800_123_000,      # microseconds read as milliseconds
            1_772_668_800_123_456_789,  # nanoseconds, likewise
            RECEIPT_MS * 2,
            RECEIPT_MS + 86_400_000,    # a day out
            2 ** 62,
        ],
    )
    def test_an_extreme_future_time_is_rejected(self, occurred_at_ms):
        """The poisoning case, in the shapes it actually arrives in. A
        microsecond timestamp is ~1000x the millisecond one, which would hold
        the deadline for roughly 56,000 years."""
        assert spike_detector.is_trustworthy_notice_time(
            occurred_at_ms, self.RECEIPT_MS
        ) is False

    @pytest.mark.parametrize("value", [None, True, False, "1772668800123", 1.772e12, []])
    def test_a_non_integer_time_on_either_side_is_rejected(self, value):
        """bool subclasses int and True == 1, so the check must say so
        explicitly, exactly as every other field check in this module does."""
        assert spike_detector.is_trustworthy_notice_time(value, self.RECEIPT_MS) is False
        assert spike_detector.is_trustworthy_notice_time(self.RECEIPT_MS, value) is False

    def test_the_allowance_is_tunable_by_argument(self):
        assert spike_detector.is_trustworthy_notice_time(
            self.RECEIPT_MS + 60_000, self.RECEIPT_MS, max_future_skew_seconds=60
        ) is True
        assert spike_detector.is_trustworthy_notice_time(
            self.RECEIPT_MS + 60_001, self.RECEIPT_MS, max_future_skew_seconds=60
        ) is False
        assert spike_detector.is_trustworthy_notice_time(
            self.RECEIPT_MS + 1, self.RECEIPT_MS, max_future_skew_seconds=0
        ) is False

    def test_it_is_a_pure_predicate_with_no_clock_of_its_own(self, config):
        """The receipt instant is passed in, the same way process_element2
        captures it from the injected clock (research D13). A helper that read
        the wall clock could not be tested deterministically, and would make
        the operator's own injected clock a lie."""
        assert spike_detector.is_trustworthy_notice_time(
            self.RECEIPT_MS + 30_001, self.RECEIPT_MS + 60_000
        ) is True

    def test_a_trustworthy_future_notice_is_still_clamped_and_healthy(self, config):
        """The two rules meet here: within the allowance the record is applied,
        its raw negative age is clamped to zero, and the clock skew is a
        diagnostic flag rather than a rejection (contract §2.2)."""
        occurred_at_ms = self.RECEIPT_MS + 5_000
        assert spike_detector.is_trustworthy_notice_time(
            occurred_at_ms, self.RECEIPT_MS
        ) is True
        observation = spike_detector.observe_delivery_age(
            occurred_at_ms, self.RECEIPT_MS, config
        )
        assert observation.delivery_age_ms == 0
        assert observation.lag_class == spike_detector.SUPPRESSION_LAG_HEALTHY
        assert observation.clock_skew is True

    def test_the_untrusted_time_never_reaches_the_register(self, config):
        """Stated as the consequence rather than the mechanism: the arithmetic
        itself is a max(), so the only place this can be stopped is before it."""
        poisoned = self.RECEIPT_MS + 1_000_000_000_000
        assert spike_detector.is_trustworthy_notice_time(poisoned, self.RECEIPT_MS) is False
        # What would happen if it were let through, kept as the reason why.
        would_be = spike_detector.apply_notice(None, GIFT, poisoned, config)
        later = spike_detector.apply_notice(would_be, GIFT, self.RECEIPT_MS, config)
        assert later is would_be


class TestSuppressionLeavesTheDetectorArithmeticAlone:
    """T030/T032 boundary check: the suppression work must not reach into
    evaluate(). FR-008 / SC-004 depend on the gate being downstream of every
    state write, and the cheapest proof that evaluate() is untouched is that
    it still takes and returns exactly what it did."""

    def test_evaluate_signature_is_unchanged(self):
        assert list(inspect.signature(evaluate).parameters) == [
            "counts", "second", "hold", "last_fire_second", "config"
        ]

    def test_the_decision_carries_no_suppression_field(self):
        counts = steady_baseline()
        counts.update({ts: 10 for ts in WINDOW})
        decision = evaluate_at(counts)
        assert not any(
            "suppress" in name for name in decision.__dataclass_fields__
        )

    def test_the_detector_config_carries_no_suppression_field(self):
        assert not any(
            "suppress" in name for name in DetectorConfig().__dataclass_fields__
        )

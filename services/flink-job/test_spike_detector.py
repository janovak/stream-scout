"""Tests for spike_detector.evaluate(). Pure module, no mocks, no pyflink."""

from dataclasses import replace

import pytest

from spike_detector import DetectorConfig, HoldState, Spike, evaluate, is_command

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


def evaluate_at(counts, second=NOW, hold=None, last_fire_second=None, config=CONFIG):
    return evaluate(counts, second, hold, last_fire_second, config)


def intensity_of(counts, second=NOW, config=CONFIG):
    """The intensity evaluate() measured this second, fired or not.

    evaluate() only reports a measurement when an episode ends, so most seconds
    surface nothing. Drop the trigger to -inf and every second counts as
    elevated, which opens a hold whose peak is that second's own reading.
    Returns None only when the second was genuinely unmeasurable (warm-up gate,
    or a baseline with no spread).
    """
    always_elevated = replace(config, k=float("-inf"))
    decision = evaluate(counts, second, None, None, always_elevated)
    return None if decision.hold is None else decision.hold.peak_intensity


class TestCommandFilter:
    def test_is_command_matches_bang_prefix(self):
        assert is_command("!clip")
        assert is_command("!8ball magic")

    def test_is_command_false_for_ordinary_chat(self):
        assert not is_command("hello world")
        assert not is_command("")
        assert not is_command("? not a command")


class TestShippedDefaults:
    """The defaults are the Plan 06 Phase 3 decisions; pin them."""

    def test_baseline_restored_to_five_minutes(self):
        # Commit c7afdab (a frontend change) dropped this to 10 and never put
        # it back. Plan 06 step 12 restores it.
        assert DetectorConfig().baseline_seconds == 300

    def test_window_and_gate_defaults(self):
        config = DetectorConfig()
        assert config.window_seconds == 5
        assert config.min_baseline_fraction == 0.8
        assert config.cooldown_seconds == 30
        # 0.8 x 300 = 240 baseline buckets, so a channel produces nothing for
        # its first ~4 minutes of chat. That warm-up is what stream-monitoring's
        # join/leave hysteresis exists to protect.
        assert int(config.baseline_seconds * config.min_baseline_fraction) == 240

    def test_hold_cap_default_is_a_placeholder(self):
        # Untuned: Plan 06 Phase 4 step 19 picks the real value off the corpus.
        assert DetectorConfig().hold_cap_seconds == 60

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
            {"min_baseline_fraction": 0.0},
            {"min_baseline_fraction": 1.5},
        ],
    )
    def test_nonsense_config_is_rejected_at_construction(self, kwargs):
        with pytest.raises(ValueError):
            DetectorConfig(**kwargs)


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
        assert intensity_of(silent) == pytest.approx(-10.0 / counts_std(counts), rel=1e-9)


def counts_std(counts):
    values = [counts.get(ts, 0) for ts in BASELINE]
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


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
    def test_too_few_populated_baseline_buckets_never_fires(self):
        """min_baseline_fraction is 0.8, so 20s of baseline needs 16 buckets."""
        counts = {ts: 1000 for ts in WINDOW}
        counts.update({ts: 5 for ts in range(981, 996)})  # 15 baseline buckets, one short
        decision = evaluate_at(counts)
        assert decision.emit is None
        assert decision.hold is None

    def test_one_more_bucket_clears_the_gate(self):
        counts = {ts: 1000 for ts in WINDOW}
        counts.update({ts: 5 + (ts % 3) for ts in range(980, 996)})  # 16 buckets
        assert evaluate_at(counts).hold is not None

    def test_uniform_baseline_has_no_scale_and_does_not_fire(self):
        """std 0 would divide by zero; treat it as unmeasurable, not infinite."""
        counts = {ts: 5 for ts in BASELINE}
        counts.update({ts: 5000 for ts in WINDOW})
        decision = evaluate_at(counts)
        assert decision.emit is None
        assert decision.hold is None


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
        open_hold = HoldState(
            started_at=NOW,
            peak_intensity=9.5,
            peak_at=NOW,
            peak_message_count=300,
            peak_baseline_mean=10.0,
            peak_baseline_std=1.0,
        )
        counts = {ts: 5 for ts in range(990, 996)}  # far below the warm-up gate
        decision = evaluate_at(counts, second=NOW, hold=open_hold)
        assert decision.emit is None
        assert decision.hold == open_hold

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

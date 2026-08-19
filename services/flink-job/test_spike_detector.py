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

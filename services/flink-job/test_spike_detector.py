"""Tests for spike_detector.evaluate(). Pure module, no mocks, no pyflink."""

import pytest

from spike_detector import DetectorConfig, evaluate, is_command

CONFIG = DetectorConfig()  # window=5, baseline=10, std_dev_threshold=5.0, cooldown=30


def test_is_command_matches_bang_prefix():
    assert is_command("!clip")
    assert is_command("!8ball magic")


def test_is_command_false_for_ordinary_chat():
    assert not is_command("hello world")
    assert not is_command("")
    assert not is_command("? not a command")


def test_flat_baseline_no_spike():
    """Baseline had some activity, the window went quiet -- nowhere near a spike."""
    counts = {990: 10, 991: 12, 992: 8, 993: 11, 994: 9, 995: 1, 996: 1, 997: 0, 998: 1, 999: 1}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike is None


def test_flat_baseline_sharp_burst_detects_spike():
    """9 buckets steady at 5, the window's last bucket jumps to 10 -- a real burst."""
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 5, 995: 5, 996: 5, 997: 5, 998: 5, 999: 10}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike is not None
    assert decision.spike.message_count == 30
    assert decision.spike.baseline_mean == pytest.approx(5.5)
    assert decision.spike.baseline_std == pytest.approx(1.5811388300841898)


def test_fewer_buckets_than_warmup_gate_no_spike():
    """Only 3 buckets on record (min_required is 8) -- no spike, however large the counts."""
    counts = {997: 1000, 998: 1000, 999: 1000}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike is None


def test_zero_standard_deviation_no_spike_no_error():
    """A perfectly uniform baseline has std_dev 0 -- must not raise ZeroDivisionError."""
    counts = {ts: 5 for ts in range(990, 1000)}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike is None


def test_spike_inside_cooldown_is_suppressed():
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 5, 995: 5, 996: 5, 997: 5, 998: 5, 999: 10}
    now_seconds = 1000
    last_spike_ms = (now_seconds * 1000) - 5_000  # 5s ago, inside the 30s cooldown
    decision = evaluate(counts, now_seconds, last_spike_ms, CONFIG)
    assert decision.spike is None


def test_spike_after_cooldown_is_allowed():
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 5, 995: 5, 996: 5, 997: 5, 998: 5, 999: 10}
    now_seconds = 1000
    last_spike_ms = (now_seconds * 1000) - 35_000  # 35s ago, past the 30s cooldown
    decision = evaluate(counts, now_seconds, last_spike_ms, CONFIG)
    assert decision.spike is not None


def test_buckets_older_than_baseline_window_are_expired():
    """Bucket 980 is older than baseline_start (990) and must be listed for eviction."""
    counts = {980: 3}
    counts.update({ts: 5 for ts in range(990, 1000)})
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.expired_buckets == [980]


def test_intensity_is_window_sum_minus_mean_over_std():
    """Worked example, pinned: intensity = (window_sum - mean) / std."""
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 5, 995: 5, 996: 5, 997: 5, 998: 5, 999: 10}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    window_sum, mean, std = 30, 5.5, 1.5811388300841898
    assert decision.spike.intensity == pytest.approx((window_sum - mean) / std)
    assert decision.spike.intensity == pytest.approx(15.495160534825057)


def test_baseline_currently_includes_the_detection_window():
    """Documents a question, does not fix it -- see plans/06-detection-math.md.

    baseline_start is now - 10s and window_start is now - 5s, so the last 5 buckets
    land in both counts_baseline and counts_window. If the baseline correctly excluded
    the window, the reported mean here would be 5.0 (the outer 5 buckets, all at 5).
    It isn't -- the window's own 10 inflates the baseline it's being measured against.
    """
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 5, 995: 5, 996: 5, 997: 5, 998: 5, 999: 10}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike.baseline_mean == pytest.approx(5.5)
    assert decision.spike.baseline_mean != pytest.approx(5.0)


def test_window_sum_compared_against_per_bucket_statistics():
    """Documents a question, does not fix it -- see plans/06-detection-math.md.

    window_sum is a sum over 5 buckets; mean and std are per-bucket. Here the window
    itself shows no elevation at all -- every one of its 5 buckets sits at the same
    typical level (5) as the rest of the baseline -- yet it is still flagged, because
    a 5-bucket sum (25) is being compared against a per-bucket threshold (~6.68).
    """
    counts = {990: 5, 991: 5, 992: 5, 993: 5, 994: 6, 995: 5, 996: 5, 997: 5, 998: 5, 999: 5}
    decision = evaluate(counts, now_seconds=1000, last_spike_ms=None, config=CONFIG)
    assert decision.spike is not None
    assert decision.spike.message_count == 25

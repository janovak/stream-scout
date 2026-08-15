"""
Tests for tools/replay.py's event-time scheduling -- the per-second-timer
simulation that stands in for Flink's TimerService, not evaluate() itself
(that's covered by test_spike_detector.py).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spike_detector import DetectorConfig
from tools.replay import EventTimeReplayer, replay

# A short baseline so fixtures stay a readable length -- the shipped default is
# 300s (see test_spike_detector.py::TestShippedDefaults). These tests are about
# the event-time scheduling, not the arithmetic.
CONFIG = DetectorConfig(
    window_seconds=5,
    baseline_seconds=10,
    k=3.0,
    hold_cap_seconds=10,
    cooldown_seconds=30,
)
DEV_SLICE = Path(__file__).parent / "corpus" / "dev-slice.jsonl"


def msg(broadcaster_id, sent_at_ms, text="hi"):
    return json.dumps({"broadcaster_id": broadcaster_id, "sent_at": sent_at_ms, "text": text})


def test_bucket_comes_from_sent_at_not_other_fields():
    replayer = EventTimeReplayer(CONFIG)
    evaluations = list(replayer.feed(broadcaster_id=1, sent_at_ms=1_000_000))
    assert replayer._states[1].counts == {1000: 1}
    assert evaluations == []  # watermark hasn't reached second 1000 yet


def test_evaluation_waits_for_watermark_to_pass_the_second():
    replayer = EventTimeReplayer(CONFIG)
    list(replayer.feed(1, sent_at_ms=1000 * 1000))  # bucket 1000, no eval yet (watermark lags 5s)
    evaluations = list(replayer.feed(1, sent_at_ms=1004 * 1000))  # watermark = 1004000-5000-1 = 998999
    assert evaluations == []
    evaluations = list(replayer.feed(1, sent_at_ms=1005 * 1000))  # watermark = 1005000-5000-1 = 999999, still < 1000000
    assert evaluations == []
    # Flink's real BoundedOutOfOrdernessWatermarks emits maxTimestamp -
    # outOfOrdernessMillis - 1, so it takes one more ms to cross second 1000's
    # boundary than a naive max-bound subtraction would.
    evaluations = list(replayer.feed(1, sent_at_ms=1005 * 1000 + 1))  # watermark = 1000000
    assert [e.second for e in evaluations] == [1000]


def test_out_of_order_message_within_bound_still_counted_before_its_second_fires():
    replayer = EventTimeReplayer(CONFIG)
    # second 1000 arrives, then second 1003 (pushes watermark to 997999, second
    # 1000 still not due), then a late second-1000 message arrives before the
    # watermark passes 1000 -- must land in the same bucket as the first.
    list(replayer.feed(1, sent_at_ms=1000 * 1000))
    list(replayer.feed(1, sent_at_ms=1003 * 1000))
    list(replayer.feed(1, sent_at_ms=1000 * 1000 + 500))  # same second, out of order
    evaluations = list(replayer.feed(1, sent_at_ms=1005 * 1000 + 1))  # watermark -> 1000000, fires
    fired = [e for e in evaluations if e.second == 1000]
    assert len(fired) == 1
    # Second 1000's bucket holds both the on-time and the out-of-order
    # message by the time it was evaluated -- proof the late arrival landed
    # in its bucket instead of being dropped or evaluated separately.
    assert replayer._states[1].counts[1000] == 2


def test_idle_key_evaluated_by_other_keys_traffic_then_chain_lapses():
    replayer = EventTimeReplayer(CONFIG)
    # Broadcaster 2 sends one message and goes silent; broadcaster 1 keeps
    # sending, advancing the shared watermark. Broadcaster 2 must still get
    # evaluated each second on that shared clock, not just its own.
    list(replayer.feed(2, sent_at_ms=1000 * 1000))
    fired_for_2 = []
    for second in range(1001, 1020):
        evaluations = list(replayer.feed(1, sent_at_ms=second * 1000 + 5000))
        fired_for_2.extend(e for e in evaluations if e.broadcaster_id == 2)
    # Baseline is 10s: broadcaster 2's single bucket expires once evaluated
    # seconds run far enough past it, after which its chain lapses (no more
    # data to hold a timer chain open).
    assert len(fired_for_2) >= 1
    assert 2 not in replayer._states or not replayer._states[2].counts


def test_future_buckets_already_in_state_are_excluded_from_evaluate():
    """Regression: a message for second+1..+bound can already be counted by
    the time `second`'s own timer fires (its timer only needs the watermark
    to pass `second`, and the watermark itself needs those later messages to
    have already arrived and been counted). evaluate() has no upper bound on
    ts_bucket -- that invariant used to be guaranteed by the caller when
    now_seconds was real wall-clock time -- so without filtering, those
    future buckets leak into `second`'s baseline and window and can
    manufacture a spike that was never really there."""
    replayer = EventTimeReplayer(CONFIG)  # window=5, baseline=10

    # Baseline deep enough to clear the warm-up gate (0.8 x 10 = 8 buckets),
    # alternating 2 and 3 messages so it has a real standard deviation. Without
    # both, "no spike" below would prove nothing -- an unmeasurable baseline
    # never fires whether or not the filter works.
    evaluations = []
    for second in range(980, 1001):
        for _ in range(2 + second % 2):
            evaluations.extend(replayer.feed(1, sent_at_ms=second * 1000))

    # A heavy burst for 1001..1005 lands in state before bucket 1000's timer
    # becomes due (needs watermark >= 1000000, i.e. max_sent_at_ms >= 1005001).
    for second in range(1001, 1006):
        for _ in range(100):
            evaluations.extend(replayer.feed(1, sent_at_ms=second * 1000))

    # One more message pushes the watermark past bucket 1000's boundary.
    evaluations.extend(replayer.feed(1, sent_at_ms=1005 * 1000 + 1))

    fired_for_1000 = [e for e in evaluations if e.second == 1000]
    assert len(fired_for_1000) == 1
    # Second 1000 saw only its own steady traffic, so no episode opened at all.
    assert fired_for_1000[0].emit is None
    assert replayer._states[1].hold is None

    # The burst is real, though -- once its own seconds come due it must open
    # an episode. This is what proves the assertion above is about the filter
    # and not about the detector being deaf to the burst entirely.
    for second in range(1006, 1012):
        evaluations.extend(replayer.feed(1, sent_at_ms=second * 1000))
    assert replayer._states[1].hold is not None
    assert replayer._states[1].hold.peak_at >= 1001


def test_command_messages_filtered_like_production_commandfilter():
    lines = [
        msg(1, 1000 * 1000, text="hello"),
        msg(1, 1000 * 1000 + 100, text="!clip"),
        msg(1, 1005 * 1000),
    ]
    list(replay(lines, CONFIG))
    replayer = EventTimeReplayer(CONFIG)
    for line in lines:
        m = json.loads(line)
        if not m["text"].startswith("!"):
            list(replayer.feed(m["broadcaster_id"], m["sent_at"]))
    assert replayer._states[1].counts.get(1000, 0) == 1  # the "!clip" message never counted


def test_command_message_still_advances_watermark():
    """SentAtTimestampAssigner runs on the source's WatermarkStrategy,
    upstream of CommandFilter in clip_detector_job.py -- so a command's
    sent_at advances the real watermark even though CommandFilter drops it
    before AnomalyDetector ever sees it. If replay() only fed non-command
    messages into the watermark, a command-heavy channel would make the
    harness's clock lag behind what production actually does."""
    lines = [
        msg(1, 1000 * 1000, text="hello"),      # bucket 1000, not yet due
        msg(1, 1005 * 1000 + 1, text="!clip"),  # command -- watermark -> 1000000, never counted
    ]
    evaluations = list(replay(lines, CONFIG))
    assert [e.second for e in evaluations] == [1000]


def steady_then_burst_lines(burst_seconds=(1025, 1026, 1027), quiet=3, loud=80):
    """45s of chat alternating quiet/quiet+1 msgs per second, with a burst in it.

    It runs well past the burst on purpose: a timer for second N only becomes
    due once the watermark passes it, which needs traffic about
    WATERMARK_OUT_OF_ORDERNESS_SECONDS later. Ending at the burst would leave
    the episode still open when the input ran out.
    """
    lines = []
    for second in range(1000, 1045):
        count = loud if second in burst_seconds else quiet + second % 2
        for offset in range(count):
            lines.append(msg(1, second * 1000 + offset))
    return lines


def test_hold_persists_across_seconds_and_emits_the_peak():
    """The harness must carry the hold between evaluations, as
    AnomalyDetector carries it in ValueState -- otherwise every elevated
    second would look like a fresh episode."""
    evaluations = list(replay(steady_then_burst_lines(), CONFIG))
    fired = [e for e in evaluations if e.emit is not None]

    # One elevation episode in, one clip out.
    assert len(fired) == 1
    spike = fired[0].emit

    # The reported second is the peak's, and it lands inside the burst -- not
    # at the second the detector happened to notice the episode had ended.
    assert 1025 <= spike.detected_at_seconds <= 1027
    assert spike.detected_at_seconds < fired[0].second
    assert spike.intensity >= CONFIG.k


def test_no_spike_emitted_from_steady_traffic():
    """The regression that motivated Plan 06: a resting channel must be silent."""
    lines = steady_then_burst_lines(burst_seconds=())
    assert all(e.emit is None for e in replay(lines, CONFIG))


def test_replay_is_deterministic_on_repeat():
    lines = [
        msg(bid, base_ms + offset)
        for base_ms in range(1_000_000, 1_030_000, 1000)
        for bid, offset in [(1, 0), (2, 250), (1, 900), (3, 50)]
    ]
    run1 = [(e.broadcaster_id, e.second, e.emit) for e in replay(lines, CONFIG)]
    run2 = [(e.broadcaster_id, e.second, e.emit) for e in replay(lines, CONFIG)]
    assert run1 == run2


@pytest.mark.skipif(not DEV_SLICE.exists(), reason="corpus/dev-slice.jsonl not cut locally (gitignored)")
def test_dev_slice_replay_is_byte_identical_across_runs():
    """The literal check from plans/06-detection-math.md's Verification section."""
    run = lambda: subprocess.run(  # noqa: E731
        [sys.executable, "tools/replay.py", str(DEV_SLICE)],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert run() == run()

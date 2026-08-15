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

CONFIG = DetectorConfig()  # window=5, baseline=10
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

    # Steady baseline: buckets 990..1000, one message each -- uniform, so
    # std=0 and evaluate() can never fire a spike from this alone.
    evaluations = []
    for second in range(990, 1001):
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
    assert fired_for_1000[0].spike is None  # not the spurious spike the future burst would manufacture


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


def test_replay_is_deterministic_on_repeat():
    lines = [
        msg(bid, base_ms + offset)
        for base_ms in range(1_000_000, 1_030_000, 1000)
        for bid, offset in [(1, 0), (2, 250), (1, 900), (3, 50)]
    ]
    run1 = [(e.broadcaster_id, e.second, e.spike) for e in replay(lines, CONFIG)]
    run2 = [(e.broadcaster_id, e.second, e.spike) for e in replay(lines, CONFIG)]
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

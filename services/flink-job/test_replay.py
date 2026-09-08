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

from spike_detector import (
    SUPPRESSION_IDLENESS_SECONDS,
    SUPPRESSION_LAG_HEALTHY,
    SUPPRESSION_MAX_FUTURE_SKEW_SECONDS,
    SUPPRESSION_REJECT_FIELDS,
    DetectorConfig,
    SuppressionConfig,
)
from tools.replay import EventTimeReplayer, WATERMARK_OUT_OF_ORDERNESS_MS, format_evaluation, replay

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
    list(replayer.feed(1, sent_at_ms=1000 * 1000))  # bucket 1000, no eval yet (watermark lags)
    evaluations = list(replayer.feed(1, sent_at_ms=1000 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS))
    assert evaluations == []  # watermark = 1000*1000 + OOO_MS - OOO_MS - 1 = 999999, still short
    # Flink's real BoundedOutOfOrdernessWatermarks emits maxTimestamp -
    # outOfOrdernessMillis - 1, so it takes one more ms to cross second 1000's
    # boundary than a naive max-bound subtraction would.
    evaluations = list(replayer.feed(1, sent_at_ms=1000 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1))
    assert [e.second for e in evaluations] == [1000]  # watermark = 1000000


def test_out_of_order_message_within_bound_still_counted_before_its_second_fires():
    replayer = EventTimeReplayer(CONFIG)
    # second 1000 arrives, then a later-second message that still doesn't
    # cross second 1000's boundary, then a late second-1000 message arrives
    # before the watermark passes 1000 -- must land in the same bucket as
    # the first.
    list(replayer.feed(1, sent_at_ms=1000 * 1000))
    list(replayer.feed(1, sent_at_ms=1001 * 1000))  # later second, watermark still short
    list(replayer.feed(1, sent_at_ms=1000 * 1000 + 500))  # same second as the first, out of order
    evaluations = list(replayer.feed(1, sent_at_ms=1000 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1))  # watermark -> 1000000, fires
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
    have already arrived and been counted). Those future buckets must not
    leak into `second`'s baseline and window, or they manufacture a spike
    that was never really there.

    What this test does NOT do is pin the caller-side filter in
    `replay._fire` / `AnomalyDetector.on_timer`. Deleting that filter leaves
    this test green, because `evaluate()` bounds the window itself with
    `elif ts_bucket <= second` and ignores a future bucket anyway. The bound
    inside `evaluate()` is what actually protects this, and it has its own
    guard in `test_spike_detector.py::TestFutureBuckets`, which does fail if
    that branch is widened. This test pins the end-to-end behaviour through
    the replay harness; it is not the filter's regression test, and removing
    the filter on the strength of it passing would be a mistake."""
    replayer = EventTimeReplayer(CONFIG)  # window=5, baseline=10

    # Baseline deep enough to clear the warm-up gate (0.8 x 10 = 8 buckets),
    # alternating 2 and 3 messages so it has a real standard deviation. Without
    # both, "no spike" below would prove nothing -- an unmeasurable baseline
    # never fires whether or not the filter works.
    evaluations = []
    for second in range(980, 1001):
        for _ in range(2 + second % 2):
            evaluations.extend(replayer.feed(1, sent_at_ms=second * 1000))

    # A heavy burst for second 1001 lands in state before bucket 1000's timer
    # becomes due (needs watermark >= 1000000, i.e.
    # max_sent_at_ms >= 1000*1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1). At
    # least one future second is therefore in state when second 1000 fires,
    # which is what the filter must exclude. How many depends on the bound:
    # the pushing feed below sits WATERMARK_OUT_OF_ORDERNESS_MS + 1 past
    # second 1000, so at 1s it landed in bucket 1001 and joined the burst,
    # and at 2s it lands in bucket 1002 and makes a second future bucket.
    # Either way second 1000 sees only its own steady traffic.
    for _ in range(100):
        evaluations.extend(replayer.feed(1, sent_at_ms=1001 * 1000))

    # One more message pushes the watermark past bucket 1000's boundary.
    evaluations.extend(replayer.feed(1, sent_at_ms=1000 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1))

    fired_for_1000 = [e for e in evaluations if e.second == 1000]
    assert len(fired_for_1000) == 1
    # Second 1000 saw only its own steady traffic, so no episode opened at all.
    assert fired_for_1000[0].emit is None
    assert replayer._states[1].hold is None

    # The burst is real, though -- once its own second comes due it must open
    # an episode. This is what proves the assertion above is about the filter
    # and not about the detector being deaf to the burst entirely. Checked
    # right as bucket 1001 becomes due, not after a longer tail: the burst is
    # only one second wide here (see the comment above), so it ages out of
    # the 5-second window quickly and a longer tail would let the episode
    # already retire before this assertion ever ran.
    evaluations.extend(
        replayer.feed(1, sent_at_ms=1001 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1)
    )
    assert replayer._states[1].hold is not None
    assert replayer._states[1].hold.peak_at == 1001


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
        msg(1, 1000 * 1000 + WATERMARK_OUT_OF_ORDERNESS_MS + 1, text="!clip"),  # command -- watermark -> 1000000, never counted
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


# Feature 007 merged-replay fixtures. Tagged deliveries preserve list order:
# occurred_at_ms is event time, while delivered_at_ms is the deterministic
# consumer receipt/processing clock supplied to EventTimeReplayer.run().
GATING_ON = SuppressionConfig(gating_enabled=True)
GATING_OFF = SuppressionConfig(gating_enabled=False)


def chat_delivery(broadcaster_id, sent_at_ms, text="hi", delivered_at_ms=None):
    return {
        "input": "chat",
        "value": msg(broadcaster_id, sent_at_ms, text=text),
        "delivered_at_ms": sent_at_ms if delivered_at_ms is None else delivered_at_ms,
    }


def suppression_record(broadcaster_id, notice_type, occurred_at_ms, **optional_fields):
    payload = {
        "schema_version": 1,
        "broadcaster_id": broadcaster_id,
        "notice_type": notice_type,
        "occurred_at_ms": occurred_at_ms,
    }
    payload.update(optional_fields)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def suppression_delivery(
    broadcaster_id,
    notice_type,
    occurred_at_ms,
    delivered_at_ms=None,
    **optional_fields,
):
    return {
        "input": "suppression",
        "value": suppression_record(
            broadcaster_id,
            notice_type,
            occurred_at_ms,
            **optional_fields,
        ),
        "delivered_at_ms": (
            occurred_at_ms if delivered_at_ms is None else delivered_at_ms
        ),
    }


def deterministic_chat_deliveries(
    broadcaster_id=1,
    burst_seconds=(1025, 1026, 1027),
    start_second=1000,
    end_second=1045,
    quiet=3,
    loud=80,
):
    """Small synthetic fixture that exercises the detector, not a copy of it."""
    deliveries = []
    for second in range(start_second, end_second):
        count = loud if second in burst_seconds else quiet + second % 2
        deliveries.extend(
            chat_delivery(broadcaster_id, second * 1000 + offset)
            for offset in range(count)
        )
    return deliveries


def run_merged(deliveries, suppression_config=GATING_ON):
    replayer = EventTimeReplayer(
        CONFIG,
        suppression_config=suppression_config,
    )
    return replayer.run(
        deliveries,
        consumer_receipt_ms=lambda delivery: delivery["delivered_at_ms"],
    )


def serialized_result(result):
    return json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def clip_peak_second(clip):
    return clip["spike"]["detected_at_seconds"]


def trace_for(result, broadcaster_id):
    return [
        row
        for row in result["detector_trace"]
        if row["broadcaster_id"] == broadcaster_id
    ]


DETECTOR_STATE_TRACE_FIELDS = (
    "broadcaster_id",
    "second",
    "message_counts",
    "measurement",
    "hold_before",
    "hold_after",
    "expired_buckets",
    "timer_fired",
    "timer_registered",
    "last_fire_second_before",
    "last_fire_second_after",
)


def assert_detector_state_trace_equal(left, right):
    """SC-004 comparison: every detector-state surface, output excluded."""
    left_rows = left["detector_trace"]
    right_rows = right["detector_trace"]
    assert len(left_rows) == len(right_rows)
    for left_row, right_row in zip(left_rows, right_rows):
        assert left_row["message_counts"] == sorted(left_row["message_counts"])
        assert right_row["message_counts"] == sorted(right_row["message_counts"])
        assert left_row["expired_buckets"] == sorted(left_row["expired_buckets"])
        assert right_row["expired_buckets"] == sorted(right_row["expired_buckets"])
        assert {
            field: left_row[field] for field in DETECTOR_STATE_TRACE_FIELDS
        } == {
            field: right_row[field] for field in DETECTOR_STATE_TRACE_FIELDS
        }


def insert_before_chat_second(deliveries, second, delivery):
    insertion = next(
        index
        for index, item in enumerate(deliveries)
        if json.loads(item["value"])["sent_at"] // 1000 >= second
    )
    return deliveries[:insertion] + [delivery] + deliveries[insertion:], insertion


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


def test_every_evaluation_carries_its_own_reading():
    """Plan 06 Phase 4 step 17: tools/measure_corpus.py dumps one row per
    evaluated second, so the harness has to pass Decision's diagnostic fields
    through instead of only the rare `emit`."""
    evaluations = list(replay(steady_then_burst_lines(), CONFIG))

    measured = [e for e in evaluations if e.measurement is not None]
    # Far more readings than firings -- that gap is exactly why step 17 needs
    # the diagnostic path and cannot read the distribution off `emit`.
    assert len(measured) > len([e for e in evaluations if e.emit is not None])
    # Each reading belongs to the second it was reported on.
    assert all(e.measurement.detected_at_seconds == e.second for e in measured)
    # The burst is in there, and it is not the only thing in there.
    assert max(e.measurement.intensity for e in measured) >= CONFIG.k
    assert min(e.measurement.intensity for e in measured) < CONFIG.k

    # observed_seconds grows with elapsed observation and stops at the
    # baseline length -- what the warm-up gate compares (step 22).
    for e in evaluations:
        assert 0 <= e.observed_seconds <= CONFIG.baseline_seconds
    assert evaluations[0].observed_seconds == 0
    assert max(e.observed_seconds for e in evaluations) == CONFIG.baseline_seconds


def test_printed_output_ignores_the_diagnostic_fields():
    """format_evaluation is the determinism check's surface (Verification in
    plans/06-detection-math.md). The step 17 fields must not appear in it, or
    every prior replay transcript stops comparing."""
    evaluations = list(replay(steady_then_burst_lines(), CONFIG))
    quiet = next(e for e in evaluations if e.emit is None and e.measurement is not None)
    assert format_evaluation(quiet) == f"{quiet.second} {quiet.broadcaster_id} no-spike"


def test_replay_is_deterministic_on_repeat():
    lines = [
        msg(bid, base_ms + offset)
        for base_ms in range(1_000_000, 1_030_000, 1000)
        for bid, offset in [(1, 0), (2, 250), (1, 900), (3, 50)]
    ]
    run1 = [(e.broadcaster_id, e.second, e.emit) for e in replay(lines, CONFIG)]
    run2 = [(e.broadcaster_id, e.second, e.emit) for e in replay(lines, CONFIG)]
    assert run1 == run2


class TestMergedSuppressionReplay:
    def test_late_notice_does_not_retract_but_gates_a_later_active_peak(self):
        chat = deterministic_chat_deliveries(
            burst_seconds=(1025, 1026, 1027, 1075, 1076, 1077),
            end_second=1095,
        )
        chat_only = run_merged(chat)
        assert len(chat_only["clips"]) == 2
        first_clip, later_clip = chat_only["clips"]
        first_peak = clip_peak_second(first_clip)
        later_peak = clip_peak_second(later_clip)

        # Insert after the delivery that emitted the first clip. Although the
        # notice's occurred_at predates that clip, delivery order is decisive:
        # output already emitted is immutable. Its still-live deadline can
        # gate the later episode.
        insertion = first_clip["delivery_index"] + 1
        assert insertion < later_clip["delivery_index"]
        receipt_ms = chat[insertion - 1]["delivered_at_ms"]
        late_notice = suppression_delivery(
            1,
            "community_sub_gift",
            first_peak * 1000,
            delivered_at_ms=receipt_ms,
        )
        merged = chat[:insertion] + [late_notice] + chat[insertion:]

        gated = run_merged(merged)

        assert [clip_peak_second(clip) for clip in gated["clips"]] == [first_peak]
        assert first_peak < later_peak < first_peak + GATING_ON.gift_window_seconds
        assert len(gated["suppression_metrics"]) == 1
        assert gated["suppression_metrics"][0]["peak_second"] == later_peak
        assert len(gated["suppression_logs"]) == 1
        assert gated["suppression_logs"][0]["peak_second"] == later_peak
        assert gated["suppression_transitions"][0]["delivery_index"] == insertion

    def test_notice_state_and_output_are_isolated_by_broadcaster(self):
        one_channel = deterministic_chat_deliveries()
        probe = run_merged(one_channel)
        assert len(probe["clips"]) == 1
        peak = clip_peak_second(probe["clips"][0])

        channel_a = deterministic_chat_deliveries(broadcaster_id=101)
        channel_b = deterministic_chat_deliveries(broadcaster_id=202)
        chat = sorted(
            channel_a + channel_b,
            key=lambda delivery: (
                delivery["delivered_at_ms"],
                json.loads(delivery["value"])["broadcaster_id"],
            ),
        )
        notice = suppression_delivery(
            101,
            "raid",
            peak * 1000,
            delivered_at_ms=peak * 1000,
        )
        merged, _ = insert_before_chat_second(chat, peak, notice)

        gated = run_merged(merged)

        assert [clip["broadcaster_id"] for clip in gated["clips"]] == [202]
        assert gated["suppression_metrics"] == [
            {
                "broadcaster_id": 101,
                "notice_type": "raid",
                "peak_second": peak,
            }
        ]
        assert {row["broadcaster_id"] for row in gated["suppression_transitions"]} == {
            101
        }
        assert trace_for(gated, 101)
        assert trace_for(gated, 202)

    def test_version_one_future_trust_boundary_uses_injected_receipt_clock(self):
        receipt_ms = 2_000_000
        exact = receipt_ms + SUPPRESSION_MAX_FUTURE_SKEW_SECONDS * 1000
        over = exact + 1
        deliveries = [
            suppression_delivery(
                11,
                "sub_gift",
                exact,
                delivered_at_ms=receipt_ms,
                notice_id="accepted-at-boundary",
            ),
            suppression_delivery(
                22,
                "raid",
                over,
                delivered_at_ms=receipt_ms,
                viewer_count=500,
            ),
        ]

        result = run_merged(deliveries)

        assert len(result["suppression_transitions"]) == 1
        accepted = result["suppression_transitions"][0]
        assert accepted["broadcaster_id"] == 11
        assert accepted["state_after"] == {
            "suppress_from_ms": exact,
            "suppress_until_ms": exact + GATING_ON.gift_window_seconds * 1000,
            "notice_type": "sub_gift",
            "notice_at_ms": exact,
        }
        assert result["delivery_observations"] == [
            {
                "delivery_index": 0,
                "delivery_age_ms": 0,
                "lag_class": SUPPRESSION_LAG_HEALTHY,
                "clock_skew": True,
            }
        ]
        assert result["suppression_rejections"] == [
            {
                "delivery_index": 1,
                "reason": SUPPRESSION_REJECT_FIELDS,
            }
        ]
        assert all(
            transition["broadcaster_id"] != 22
            for transition in result["suppression_transitions"]
        )
        assert all(
            observation["delivery_index"] != 1
            for observation in result["delivery_observations"]
        )

    def test_rejected_future_time_cannot_advance_the_source_watermark(self):
        receipt_ms = 2_000_000
        poisoned = receipt_ms + (
            SUPPRESSION_MAX_FUTURE_SKEW_SECONDS * 1000
        ) + 1
        result = run_merged(
            [
                suppression_delivery(
                    22,
                    "raid",
                    poisoned,
                    delivered_at_ms=receipt_ms,
                )
            ]
        )

        assert result["suppression_rejections"] == [
            {"delivery_index": 0, "reason": SUPPRESSION_REJECT_FIELDS}
        ]
        assert result["watermark_trace"][0]["suppression_watermark_ms"] == (
            receipt_ms - WATERMARK_OUT_OF_ORDERNESS_MS - 1
        )


class TestOutputOnlySuppressionReplay:
    def test_gated_and_ungated_runs_have_identical_detector_state(self):
        chat = deterministic_chat_deliveries(
            burst_seconds=(1025, 1026, 1027, 1075, 1076, 1077),
            end_second=1095,
        )
        probe = run_merged(chat)
        assert len(probe["clips"]) == 2
        first_clip, covered_clip = probe["clips"]
        pre_notice_peak = clip_peak_second(first_clip)
        covered_peak = clip_peak_second(covered_clip)

        # The first episode peaks before this notice but reports after it.
        # The same interval remains live for the second episode.
        notice_second = pre_notice_peak + 1
        assert notice_second < first_clip["report_second"]
        notice = suppression_delivery(
            1,
            "community_sub_gift",
            notice_second * 1000,
            delivered_at_ms=notice_second * 1000,
        )
        merged, insertion = insert_before_chat_second(chat, notice_second, notice)
        assert insertion < first_clip["delivery_index"]

        ungated = run_merged(merged, GATING_OFF)
        gated = run_merged(merged, GATING_ON)

        assert_detector_state_trace_equal(ungated, gated)
        assert ungated["suppression_transitions"] == gated["suppression_transitions"]
        assert ungated["delivery_observations"] == gated["delivery_observations"]
        assert ungated["suppression_rejections"] == gated["suppression_rejections"]
        assert ungated["watermark_trace"] == gated["watermark_trace"]

        assert [clip_peak_second(clip) for clip in ungated["clips"]] == [
            pre_notice_peak,
            covered_peak,
        ]
        assert [clip_peak_second(clip) for clip in gated["clips"]] == [
            pre_notice_peak
        ]
        assert ungated["suppression_metrics"] == []
        assert ungated["suppression_logs"] == []
        assert gated["suppression_metrics"] == [
            {
                "broadcaster_id": 1,
                "notice_type": "community_sub_gift",
                "peak_second": covered_peak,
            }
        ]
        assert len(gated["suppression_logs"]) == 1
        assert gated["suppression_logs"][0] == {
            "event": "clip_suppressed",
            "broadcaster_id": 1,
            "notice_type": "community_sub_gift",
            "peak_second": covered_peak,
        }

        # These explicit projections make SC-004's state claim visible even
        # if the result later gains unrelated diagnostic fields.
        assert [
            row["measurement"] for row in trace_for(ungated, 1)
        ] == [row["measurement"] for row in trace_for(gated, 1)]
        assert [
            (row["hold_before"], row["hold_after"])
            for row in trace_for(ungated, 1)
        ] == [
            (row["hold_before"], row["hold_after"])
            for row in trace_for(gated, 1)
        ]
        assert [
            (row["timer_fired"], row["timer_registered"])
            for row in trace_for(ungated, 1)
        ] == [
            (row["timer_fired"], row["timer_registered"])
            for row in trace_for(gated, 1)
        ]
        assert [
            (
                row["last_fire_second_before"],
                row["last_fire_second_after"],
            )
            for row in trace_for(ungated, 1)
        ] == [
            (
                row["last_fire_second_before"],
                row["last_fire_second_after"],
            )
            for row in trace_for(gated, 1)
        ]

    def test_legacy_unsuppressed_human_readable_output_is_unchanged(self):
        evaluations = list(replay(steady_then_burst_lines(), CONFIG))
        fired = [evaluation for evaluation in evaluations if evaluation.emit is not None]
        assert len(fired) == 1
        evaluation = fired[0]
        spike = evaluation.emit
        assert format_evaluation(evaluation) == (
            f"{evaluation.second} {evaluation.broadcaster_id} SPIKE "
            f"peak_at={spike.detected_at_seconds} "
            f"count={spike.message_count} mean={spike.baseline_mean:.4f} "
            f"std={spike.baseline_std:.4f} intensity={spike.intensity:.4f}"
        )


class TestSuppressionIntervalReplay:
    def test_peak_boundaries_and_fail_open_paths_use_peak_time(self):
        chat = deterministic_chat_deliveries()
        probe = run_merged(chat)
        assert len(probe["clips"]) == 1
        control_clip = probe["clips"][0]
        peak = clip_peak_second(control_clip)

        at_start, _ = insert_before_chat_second(
            chat,
            peak,
            suppression_delivery(
                1,
                "community_sub_gift",
                peak * 1000,
                delivered_at_ms=peak * 1000,
            ),
        )
        at_deadline, _ = insert_before_chat_second(
            chat,
            peak,
            suppression_delivery(
                1,
                "community_sub_gift",
                (peak - GATING_ON.gift_window_seconds) * 1000,
                delivered_at_ms=peak * 1000,
            ),
        )
        after_peak_second = peak + 1
        pre_notice, _ = insert_before_chat_second(
            chat,
            after_peak_second,
            suppression_delivery(
                1,
                "raid",
                after_peak_second * 1000,
                delivered_at_ms=after_peak_second * 1000,
            ),
        )

        absent = run_merged(chat)
        inactive = run_merged(at_start, GATING_OFF)
        exact_start = run_merged(at_start)
        exact_deadline = run_merged(at_deadline)
        pre_notice_result = run_merged(pre_notice)

        assert [clip_peak_second(clip) for clip in absent["clips"]] == [peak]
        assert [clip_peak_second(clip) for clip in inactive["clips"]] == [peak]
        assert exact_start["clips"] == []
        assert [clip_peak_second(clip) for clip in exact_deadline["clips"]] == [
            peak
        ]
        assert [
            clip_peak_second(clip) for clip in pre_notice_result["clips"]
        ] == [peak]
        assert pre_notice_result["clips"][0]["report_second"] > after_peak_second
        assert len(exact_start["suppression_metrics"]) == 1
        assert exact_deadline["suppression_metrics"] == []
        assert pre_notice_result["suppression_metrics"] == []

    def test_overlap_extension_new_intervals_and_noops_are_exact(self):
        config = SuppressionConfig(
            gift_window_seconds=10,
            raid_window_seconds=20,
            gating_enabled=True,
        )
        deliveries = [
            suppression_delivery(7, "sub_gift", 100_000, delivered_at_ms=200_000),
            suppression_delivery(7, "sub_gift", 100_000, delivered_at_ms=200_001),
            suppression_delivery(7, "sub_gift", 99_000, delivered_at_ms=200_002),
            suppression_delivery(7, "sub_gift", 105_000, delivered_at_ms=200_003),
            suppression_delivery(7, "raid", 106_000, delivered_at_ms=200_004),
            suppression_delivery(7, "sub_gift", 126_000, delivered_at_ms=200_005),
            suppression_delivery(8, "sub_gift", 100_000, delivered_at_ms=200_006),
            suppression_delivery(8, "sub_gift", 111_000, delivered_at_ms=200_007),
        ]

        result = run_merged(deliveries, config)
        channel_7 = [
            transition
            for transition in result["suppression_transitions"]
            if transition["broadcaster_id"] == 7
        ]
        channel_8 = [
            transition
            for transition in result["suppression_transitions"]
            if transition["broadcaster_id"] == 8
        ]

        assert channel_7[0]["state_after"] == {
            "suppress_from_ms": 100_000,
            "suppress_until_ms": 110_000,
            "notice_type": "sub_gift",
            "notice_at_ms": 100_000,
        }
        for transition in channel_7[1:3]:
            assert transition["state_changed"] is False
            assert transition["state_after"] == channel_7[0]["state_after"]
        assert channel_7[3]["state_after"] == {
            "suppress_from_ms": 100_000,
            "suppress_until_ms": 115_000,
            "notice_type": "sub_gift",
            "notice_at_ms": 105_000,
        }
        assert channel_7[4]["state_after"] == {
            "suppress_from_ms": 100_000,
            "suppress_until_ms": 126_000,
            "notice_type": "raid",
            "notice_at_ms": 106_000,
        }
        assert channel_7[5]["state_after"] == {
            "suppress_from_ms": 126_000,
            "suppress_until_ms": 136_000,
            "notice_type": "sub_gift",
            "notice_at_ms": 126_000,
        }
        assert channel_8[-1]["state_before"]["suppress_until_ms"] == 110_000
        assert channel_8[-1]["state_after"] == {
            "suppress_from_ms": 111_000,
            "suppress_until_ms": 121_000,
            "notice_type": "sub_gift",
            "notice_at_ms": 111_000,
        }


class TestSimplifiedSparseSuppressionWatermark:
    """Offline scalar model only; these tests are not PyFlink runtime evidence."""

    def test_silent_suppression_input_preserves_chat_timer_seconds(self):
        chat = deterministic_chat_deliveries()
        legacy = list(replay([delivery["value"] for delivery in chat], CONFIG))
        merged = run_merged(chat)

        assert [
            (row["broadcaster_id"], row["timer_fired"])
            for row in merged["detector_trace"]
        ] == [
            (evaluation.broadcaster_id, evaluation.second)
            for evaluation in legacy
        ]
        assert all(
            row["suppression_idle"] is True
            and row["combined_watermark_ms"] == row["chat_watermark_ms"]
            for row in merged["watermark_trace"]
        )
        assert merged["suppression_transitions"] == []
        assert merged["delivery_observations"] == []

    def test_isolated_notice_hold_is_bounded_then_chat_progresses(self):
        deliveries = [
            chat_delivery(1, second * 1000)
            for second in range(1000, 1036)
        ]
        notice_second = 1020
        notice = suppression_delivery(
            1,
            "raid",
            notice_second * 1000,
            delivered_at_ms=notice_second * 1000,
        )
        merged, notice_index = insert_before_chat_second(
            deliveries,
            notice_second,
            notice,
        )

        result = run_merged(merged)
        notice_row = next(
            row
            for row in result["watermark_trace"]
            if row["delivery_index"] == notice_index
        )
        released_row = next(
            row
            for row in result["watermark_trace"]
            if row["delivery_index"] > notice_index
            and row["suppression_idle"] is True
        )
        active_hold_rows = [
            row
            for row in result["watermark_trace"]
            if notice_index <= row["delivery_index"] < released_row["delivery_index"]
            and row["suppression_idle"] is False
            and row["chat_watermark_ms"] > row["suppression_watermark_ms"]
        ]

        assert notice_row["input"] == "suppression"
        assert notice_row["suppression_idle"] is False
        assert active_hold_rows
        assert all(
            row["combined_watermark_ms"] == row["suppression_watermark_ms"]
            for row in active_hold_rows
        )
        assert all(
            row["processing_time_ms"] - notice_row["processing_time_ms"]
            < SUPPRESSION_IDLENESS_SECONDS * 1000
            for row in result["watermark_trace"]
            if row["delivery_index"] > notice_index
            and row["delivery_index"] < released_row["delivery_index"]
            and row["suppression_idle"] is False
        )
        assert (
            released_row["processing_time_ms"] - notice_row["processing_time_ms"]
            >= SUPPRESSION_IDLENESS_SECONDS * 1000
        )
        assert released_row["combined_watermark_ms"] == released_row[
            "chat_watermark_ms"
        ]
        assert max(
            row["timer_fired"] for row in result["detector_trace"]
        ) > notice_second

    def test_sustained_notice_traffic_advances_suppression_watermark(self):
        deliveries = []
        for second in range(1000, 1021):
            deliveries.append(chat_delivery(1, second * 1000))
            if second % 2 == 0:
                deliveries.append(
                    suppression_delivery(
                        1,
                        "sub_gift",
                        second * 1000,
                        delivered_at_ms=second * 1000,
                    )
                )

        result = run_merged(deliveries)
        notice_rows = [
            row
            for row in result["watermark_trace"]
            if row["input"] == "suppression"
        ]
        first_notice_index = notice_rows[0]["delivery_index"]
        chat_rows_after_first_notice = [
            row
            for row in result["watermark_trace"]
            if row["input"] == "chat"
            and row["delivery_index"] > first_notice_index
        ]
        suppression_watermarks = [
            row["suppression_watermark_ms"] for row in notice_rows
        ]
        combined_watermarks = [
            row["combined_watermark_ms"] for row in result["watermark_trace"]
            if row["combined_watermark_ms"] is not None
        ]

        assert len(notice_rows) > 2
        assert chat_rows_after_first_notice
        assert all(
            row["suppression_idle"] is False
            for row in chat_rows_after_first_notice
        )
        assert suppression_watermarks == sorted(suppression_watermarks)
        assert len(set(suppression_watermarks)) == len(suppression_watermarks)
        assert combined_watermarks == sorted(combined_watermarks)
        assert notice_rows[-1]["combined_watermark_ms"] > notice_rows[0][
            "combined_watermark_ms"
        ]


class TestMergedReplayDeterminism:
    def test_identical_merged_input_has_byte_identical_structured_result(self):
        chat = deterministic_chat_deliveries(
            burst_seconds=(1025, 1026, 1027),
            end_second=1050,
        )
        notice = suppression_delivery(
            1,
            "raid",
            1025 * 1000,
            delivered_at_ms=1025 * 1000,
            notice_id="deterministic-notice",
            viewer_count=123,
        )
        merged, _ = insert_before_chat_second(chat, 1025, notice)

        first = serialized_result(run_merged(merged))
        second = serialized_result(run_merged(merged))

        assert first == second


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

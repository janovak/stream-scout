#!/usr/bin/env python3
"""
Replay harness for spike_detector.evaluate() -- plain Python, no pyflink.

Feeds a captured chat-messages JSONL corpus through the same event-time,
per-second-timer evaluation shape AnomalyDetector uses in
clip_detector_job.py (Plan 06 Phase 2, steps 8-10):

  - bucket and window bounds come from `sent_at` (Twitch's clock), not the
    ingestion `timestamp` and not wall-clock time
  - each broadcaster is evaluated once per elapsed event-time second, via a
    simulated watermark and timer queue -- not once per message
  - command messages are filtered first, matching CommandFilter upstream of
    AnomalyDetector in the real pipeline

This exists because per-message evaluation depends on message interleaving,
so the same corpus could replay differently twice -- see
plans/06-detection-math.md Phase 2. Determinism here is the whole point:
`python tools/replay.py corpus/dev-slice.jsonl` run twice must diff empty.

Watermark model: a single global watermark (max sent_at seen so far, minus
WATERMARK_OUT_OF_ORDERNESS_SECONDS), shared across all broadcasters. Real
Flink's downstream watermark is the MINIMUM across its upstream source
splits, each bounded by only the traffic on that split/partition; this
harness instead takes a running MAXIMUM over the whole corpus at once.
With FLINK_PARALLELISM > 1 and uneven per-partition traffic, that means
this harness's watermark can run AHEAD of a real subtask's, and it can
become ready to fire a given broadcaster-second sooner than production
would. Do not treat a clean harness/production diff as proof production
would also fire at that instant -- verify against a live run before relying
on this for anything beyond internal-consistency and determinism checks.

Since Plan 06 Phase 3 this also carries evaluate()'s peak-hold state: an
elevation episode spans many seconds, so `_KeyState` persists the open hold
and the last firing second between evaluations exactly as AnomalyDetector
persists them in Flink ValueState. A printed SPIKE line is therefore the end
of an episode, and the measurement on it is the peak's -- see
format_evaluation.
"""

import heapq
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

# Runs as `python tools/replay.py ...`, so tools/ (not flink-job/) is on
# sys.path by default -- add flink-job/ so `import spike_detector` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_detector import (  # noqa: E402
    DetectorConfig,
    HoldState,
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    evaluate,
    is_command,
)

WATERMARK_OUT_OF_ORDERNESS_MS = WATERMARK_OUT_OF_ORDERNESS_SECONDS * 1000


@dataclass
class _KeyState:
    """Stands in for AnomalyDetector's three keyed states, in the same units."""

    counts: Dict[int, int] = field(default_factory=dict)
    hold: Optional[HoldState] = None
    last_fire_second: Optional[int] = None


@dataclass(frozen=True)
class Evaluation:
    broadcaster_id: int
    second: int
    emit: object  # spike_detector.Spike | None -- the peak, when an episode ended here


class EventTimeReplayer:
    """
    Per-broadcaster event-time scheduling: stands in for what Flink's
    TimerService does for AnomalyDetector, using a min-heap of
    (second, broadcaster_id) in place of Flink's per-key timer queue and a
    single scalar in place of per-subtask watermarks.
    """

    def __init__(self, config: DetectorConfig):
        self.config = config
        self._states: Dict[int, _KeyState] = {}
        self._timer_heap: List[Tuple[int, int]] = []
        self._pending_timers: Set[Tuple[int, int]] = set()  # (broadcaster_id, second)
        self._max_sent_at_ms: Optional[int] = None

    def _register_timer(self, broadcaster_id: int, second: int) -> None:
        key = (broadcaster_id, second)
        # Registering the same (key, second) twice is a no-op -- mirrors
        # Flink's timer dedup, see clip_detector_job.py AnomalyDetector.
        if key not in self._pending_timers:
            self._pending_timers.add(key)
            heapq.heappush(self._timer_heap, (second, broadcaster_id))

    def _watermark_ms(self) -> Optional[int]:
        if self._max_sent_at_ms is None:
            return None
        # Flink's BoundedOutOfOrdernessWatermarks emits maxTimestamp -
        # outOfOrdernessMillis - 1, not maxTimestamp - outOfOrdernessMillis --
        # the extra -1 keeps an event exactly at the bound from counting as
        # late. Match it so a timer at second*1000 == this boundary doesn't
        # fire here a millisecond before real Flink would fire it.
        return self._max_sent_at_ms - WATERMARK_OUT_OF_ORDERNESS_MS - 1

    def _bump_watermark(self, sent_at_ms: int) -> None:
        self._max_sent_at_ms = (
            sent_at_ms if self._max_sent_at_ms is None else max(self._max_sent_at_ms, sent_at_ms)
        )

    def feed(self, broadcaster_id: int, sent_at_ms: int) -> Iterator[Evaluation]:
        """Record one message; yields any evaluations its watermark advance unblocks."""
        self._bump_watermark(sent_at_ms)
        bucket = sent_at_ms // 1000

        state = self._states.setdefault(broadcaster_id, _KeyState())
        state.counts[bucket] = state.counts.get(bucket, 0) + 1

        self._register_timer(broadcaster_id, bucket)

        yield from self._drain_due_timers()

    def observe_watermark(self, sent_at_ms: int) -> Iterator[Evaluation]:
        """
        For a record that affects the watermark but is never counted -- e.g. a
        command message. In production, SentAtTimestampAssigner runs on the
        WatermarkStrategy attached at the Kafka source, upstream of
        CommandFilter, so a command's sent_at still advances the real
        watermark even though CommandFilter drops it before AnomalyDetector
        ever sees it. Mirror that ordering here rather than silently letting
        the harness's watermark lag behind production on command-heavy chat.
        """
        self._bump_watermark(sent_at_ms)
        yield from self._drain_due_timers()

    def _drain_due_timers(self) -> Iterator[Evaluation]:
        watermark_ms = self._watermark_ms()
        if watermark_ms is None:
            return
        while self._timer_heap and self._timer_heap[0][0] * 1000 <= watermark_ms:
            second, broadcaster_id = heapq.heappop(self._timer_heap)
            self._pending_timers.discard((broadcaster_id, second))
            yield self._fire(broadcaster_id, second)

    def _fire(self, broadcaster_id: int, second: int) -> Evaluation:
        state = self._states[broadcaster_id]
        # A message for second+1..second+bound can already be in state.counts
        # by the time second's timer fires (its timer only needs the
        # watermark to pass `second`, which itself only advances that far
        # once later messages have already arrived and been counted in
        # feed()). evaluate() has no upper bound on ts_bucket -- that
        # invariant used to be guaranteed by the caller when now_seconds was
        # real wall-clock time -- so future buckets must be filtered out here
        # or they'd leak into "second"'s baseline/window. Mirrors
        # clip_detector_job.py AnomalyDetector.on_timer.
        counts_as_of_second = {ts: c for ts, c in state.counts.items() if ts <= second}
        decision = evaluate(
            counts_as_of_second, second, state.hold, state.last_fire_second, self.config
        )

        for expired_bucket in decision.expired_buckets:
            state.counts.pop(expired_bucket, None)

        state.hold = decision.hold

        # Keep the per-second cadence going only while this key still has
        # data in its baseline -- an idle broadcaster's chain lapses here and
        # a later message restarts it via feed(). Matches
        # clip_detector_job.py AnomalyDetector.on_timer.
        if state.counts:
            self._register_timer(broadcaster_id, second + 1)

        # The cooldown runs from the firing second, not from the peak the
        # emitted Spike carries -- matches AnomalyDetector.on_timer.
        if decision.emit is not None:
            state.last_fire_second = second

        return Evaluation(broadcaster_id=broadcaster_id, second=second, emit=decision.emit)


def replay(lines: Iterable[str], config: DetectorConfig) -> Iterator[Evaluation]:
    replayer = EventTimeReplayer(config)
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if is_command(msg.get("text", "")):
            yield from replayer.observe_watermark(msg["sent_at"])
            continue
        yield from replayer.feed(msg["broadcaster_id"], msg["sent_at"])


def format_evaluation(evaluation: Evaluation) -> str:
    spike = evaluation.emit
    if spike is None:
        return f"{evaluation.second} {evaluation.broadcaster_id} no-spike"
    # The leading second is when the episode ended and the detector fired;
    # peak_at is the second the reported measurement was taken, and is what
    # reaches the clips table as detected_at.
    return (
        f"{evaluation.second} {evaluation.broadcaster_id} SPIKE "
        f"peak_at={spike.detected_at_seconds} "
        f"count={spike.message_count} mean={spike.baseline_mean:.4f} "
        f"std={spike.baseline_std:.4f} intensity={spike.intensity:.4f}"
    )


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <corpus.jsonl>", file=sys.stderr)
        sys.exit(1)

    config = DetectorConfig.from_env()
    with open(sys.argv[1]) as f:
        for evaluation in replay(f, config):
            print(format_evaluation(evaluation))


if __name__ == "__main__":
    main()

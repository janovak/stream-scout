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
Flink computes one watermark per parallel subtask, so with
FLINK_PARALLELISM > 1 a broadcaster's true watermark may run ahead of this
harness's. That only makes this harness fire later than production, never
earlier -- it can't manufacture a spike production wouldn't also see.

Note evaluate()'s own arithmetic (the baseline/window overlap, the unit
mismatch) is untouched here -- that's Plan 06 Phase 3, deliberately a
separate change. This harness's job is to prove the timing migration is
correct and deterministic before the math underneath it changes.
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
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    evaluate,
    is_command,
)

WATERMARK_OUT_OF_ORDERNESS_MS = WATERMARK_OUT_OF_ORDERNESS_SECONDS * 1000


@dataclass
class _KeyState:
    counts: Dict[int, int] = field(default_factory=dict)
    last_spike_ms: Optional[int] = None


@dataclass(frozen=True)
class Evaluation:
    broadcaster_id: int
    second: int
    spike: object  # spike_detector.Spike | None


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
        return self._max_sent_at_ms - WATERMARK_OUT_OF_ORDERNESS_MS

    def feed(self, broadcaster_id: int, sent_at_ms: int) -> Iterator[Evaluation]:
        """Record one message; yields any evaluations its watermark advance unblocks."""
        self._max_sent_at_ms = (
            sent_at_ms if self._max_sent_at_ms is None else max(self._max_sent_at_ms, sent_at_ms)
        )
        bucket = sent_at_ms // 1000

        state = self._states.setdefault(broadcaster_id, _KeyState())
        state.counts[bucket] = state.counts.get(bucket, 0) + 1

        self._register_timer(broadcaster_id, bucket)

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
        decision = evaluate(dict(state.counts), second, state.last_spike_ms, self.config)

        for expired_bucket in decision.expired_buckets:
            state.counts.pop(expired_bucket, None)

        # Keep the per-second cadence going only while this key still has
        # data in its baseline -- an idle broadcaster's chain lapses here and
        # a later message restarts it via feed(). Matches
        # clip_detector_job.py AnomalyDetector.on_timer.
        if state.counts:
            self._register_timer(broadcaster_id, second + 1)

        if decision.spike is not None:
            state.last_spike_ms = second * 1000

        return Evaluation(broadcaster_id=broadcaster_id, second=second, spike=decision.spike)


def replay(lines: Iterable[str], config: DetectorConfig) -> Iterator[Evaluation]:
    replayer = EventTimeReplayer(config)
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if is_command(msg.get("text", "")):
            continue
        yield from replayer.feed(msg["broadcaster_id"], msg["sent_at"])


def format_evaluation(evaluation: Evaluation) -> str:
    spike = evaluation.spike
    if spike is None:
        return f"{evaluation.second} {evaluation.broadcaster_id} no-spike"
    return (
        f"{evaluation.second} {evaluation.broadcaster_id} SPIKE "
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

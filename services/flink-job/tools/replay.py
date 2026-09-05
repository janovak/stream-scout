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

Feature 007 adds a second, optional input. `run()` replays a merged sequence
of chat and `suppression-events` deliveries in DELIVERY ORDER and returns a
structured, JSON-serializable result instead of a printed transcript. The
gate it applies is the same output-only peak-second gate
AnomalyDetector.on_timer applies (`is_suppressed` on `decision.emit`'s peak),
placed after every state write, so a gated replay and an ungated replay of
the same input produce identical counts, baselines, holds, expiries, timers
and `last_fire_second` (SC-004). The CLI path -- `replay()`,
`format_evaluation()`, `main()` -- is chat-only and is unchanged.
"""

import heapq
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

# Runs as `python tools/replay.py ...`, so tools/ (not flink-job/) is on
# sys.path by default -- add flink-job/ so `import spike_detector` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spike_detector import (  # noqa: E402
    SUPPRESSION_REJECT_FIELDS,
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    DetectorConfig,
    HoldState,
    SuppressionConfig,
    SuppressionSourceSettings,
    SuppressionState,
    apply_notice,
    decode_suppression_record,
    evaluate,
    is_command,
    is_suppressed,
    is_trustworthy_notice_time,
    observe_delivery_age,
)

WATERMARK_OUT_OF_ORDERNESS_MS = WATERMARK_OUT_OF_ORDERNESS_SECONDS * 1000

# The two names run()'s tagged deliveries carry, and the two values that reach
# the `input` column of the watermark trace. An untagged delivery is chat, so
# a legacy chat-only corpus replays through run() unchanged.
INPUT_CHAT = "chat"
INPUT_SUPPRESSION = "suppression"

# The harness reads the suppression input's watermark numbers off the same
# checked-in settings object clip_detector_job.py builds its WatermarkStrategy
# from, rather than repeating the literals here.
SUPPRESSION_SOURCE_SETTINGS = SuppressionSourceSettings()
SUPPRESSION_OUT_OF_ORDERNESS_MS = (
    SUPPRESSION_SOURCE_SETTINGS.out_of_orderness_seconds * 1000
)
SUPPRESSION_IDLENESS_MS = SUPPRESSION_SOURCE_SETTINGS.idleness_seconds * 1000

# The structured log the operator sees for a suppressed would-have-clipped
# spike. One per gated peak, exactly as AnomalyDetector.on_timer emits one
# `CLIP SUPPRESSED` line and one clips_suppressed_total increment (FR-012).
SUPPRESSED_CLIP_EVENT = "clip_suppressed"


@dataclass
class _KeyState:
    """Stands in for AnomalyDetector's four keyed states, in the same units."""

    counts: Dict[int, int] = field(default_factory=dict)
    hold: Optional[HoldState] = None
    last_fire_second: Optional[int] = None
    # Feature 007's one new ValueState. Keyed like the other three: nothing in
    # this harness maps a broadcaster to another broadcaster's interval, so
    # channel isolation is structural rather than by convention (NFR-002, I13).
    suppression: Optional[SuppressionState] = None


def _record_row(record: Any) -> Optional[Dict[str, Any]]:
    """A frozen detector/suppression dataclass as a plain dict, or None.

    asdict() reads the field list off the dataclass, so a new field cannot be
    silently dropped from a trace, and the field order is the declaration
    order -- no repr, no id(), nothing that changes between two runs of the
    same input.
    """
    return None if record is None else asdict(record)


def _payload_of(value: Any) -> Optional[Dict[str, Any]]:
    """One delivery's value as a dict, or None for anything that is not one.

    Mirrors clip_detector_job._suppression_payload: decoding a record is the
    one place this harness is deliberately fail-open, because a single
    unreadable record must not end the replay.
    """
    if isinstance(value, dict):
        return value
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _plain_int(value: Any) -> Optional[int]:
    """The value when it is an int that is not a bool, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True)
class Evaluation:
    broadcaster_id: int
    second: int
    emit: object  # spike_detector.Spike | None -- the peak, when an episode ended here

    # Carried straight off Decision -- diagnostic only, and not part of what
    # format_evaluation prints, so the determinism check's output is unchanged.
    # tools/measure_corpus.py reads these to dump every second's reading for
    # Plan 06 Phase 4; see the Decision fields of the same names.
    measurement: object = None  # spike_detector.Spike | None -- this second's reading
    observed_seconds: int = 0


class EventTimeReplayer:
    """
    Per-broadcaster event-time scheduling: stands in for what Flink's
    TimerService does for AnomalyDetector, using a min-heap of
    (second, broadcaster_id) in place of Flink's per-key timer queue and a
    single scalar in place of per-subtask watermarks.
    """

    def __init__(
        self,
        config: DetectorConfig,
        suppression_config: Optional[SuppressionConfig] = None,
    ):
        self.config = config
        # Defaulted rather than required, so every pre-007 caller -- replay(),
        # tools/measure_corpus.py, the chat-only tests -- constructs this the
        # way it always did. The code default has gating on; docker-compose.yml
        # checks in `false` (research D11).
        self.suppression_config = (
            SuppressionConfig() if suppression_config is None else suppression_config
        )
        self._states: Dict[int, _KeyState] = {}
        self._timer_heap: List[Tuple[int, int]] = []
        self._pending_timers: Set[Tuple[int, int]] = set()  # (broadcaster_id, second)
        self._max_sent_at_ms: Optional[int] = None

        # Feature 007 bookkeeping. All of it is diagnostic: see
        # _combined_watermark_ms for why the suppression side never moves a
        # chat timer here.
        self._max_notice_at_ms: Optional[int] = None
        self._last_suppression_receipt_ms: Optional[int] = None
        self._max_combined_watermark_ms: Optional[int] = None
        self._processing_time_ms: int = 0
        self._delivery_index: int = 0
        # None outside run(), so the CLI path over a full corpus keeps its
        # constant memory profile instead of accumulating a row per second.
        self._detector_trace: Optional[List[Dict[str, Any]]] = None

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
        hold_before = state.hold
        last_fire_second_before = state.last_fire_second
        # Snapshot before the expiry pass below, so the trace shows every
        # bucket this key held when the second was evaluated -- the "no
        # message was discarded" half of SC-004 -- while `expired_buckets`
        # shows what left.
        counts_before = [[ts, count] for ts, count in sorted(state.counts.items())]

        # A message for second+1..second+bound can already be in state.counts
        # by the time second's timer fires (its timer only needs the
        # watermark to pass `second`, which itself only advances that far
        # once later messages have already arrived and been counted in
        # feed()).
        #
        # This filter is defensive, not load-bearing: evaluate() bounds the
        # window with `elif ts_bucket <= second`, so a future bucket falls
        # through every branch there too, and its docstring says so. Deleting
        # this line changes no test outcome. It stays because evaluate()'s
        # docstring states the other half of the contract -- "the caller must
        # not supply buckets newer than `second`" -- and this is a caller
        # holding up its end. Mirrors clip_detector_job.py
        # AnomalyDetector.on_timer, which carries the same note; change both
        # together, or neither.
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
        timer_registered: Optional[int] = None
        if state.counts:
            timer_registered = second + 1
            self._register_timer(broadcaster_id, timer_registered)

        # The cooldown runs from the firing second, not from the peak the
        # emitted Spike carries -- matches AnomalyDetector.on_timer. This runs
        # whether or not the gate later drops the clip: the gate is
        # output-only, so a suppressed decision is state-identical to an
        # emitted one (research D6, SC-004, I11).
        if decision.emit is not None:
            state.last_fire_second = second

        if self._detector_trace is not None:
            self._detector_trace.append(
                {
                    "delivery_index": self._delivery_index,
                    "broadcaster_id": broadcaster_id,
                    "second": second,
                    "timer_fired": second,
                    "timer_registered": timer_registered,
                    "message_counts": counts_before,
                    "measurement": _record_row(decision.measurement),
                    "observed_seconds": decision.observed_seconds,
                    "hold_before": _record_row(hold_before),
                    "hold_after": _record_row(state.hold),
                    "expired_buckets": sorted(decision.expired_buckets),
                    "last_fire_second_before": last_fire_second_before,
                    "last_fire_second_after": state.last_fire_second,
                }
            )

        return Evaluation(
            broadcaster_id=broadcaster_id,
            second=second,
            emit=decision.emit,
            measurement=decision.measurement,
            observed_seconds=decision.observed_seconds,
        )

    # ------------------------------------------------------------------
    # Feature 007: merged chat + suppression replay.
    # ------------------------------------------------------------------

    def _suppression_watermark_ms(self) -> Optional[int]:
        """The suppression input's own watermark, on the same -1 convention.

        SuppressionTimestampAssigner reads `occurred_at_ms` at the SOURCE, so
        this advances on any record carrying a readable occurrence time --
        including one the operator later rejects. Rejection happens downstream
        of the watermark strategy, not upstream of it.
        """
        if self._max_notice_at_ms is None:
            return None
        return self._max_notice_at_ms - SUPPRESSION_OUT_OF_ORDERNESS_MS - 1

    def _suppression_idle(self) -> bool:
        """Whether with_idleness() would have released the suppression split.

        An input that has produced nothing yet is idle, which is what keeps a
        sparse topic from being the binding minimum on a channel that has
        never had a gift or a raid (I15).
        """
        if self._last_suppression_receipt_ms is None:
            return True
        return (
            self._processing_time_ms - self._last_suppression_receipt_ms
        ) >= SUPPRESSION_IDLENESS_MS

    def _combined_watermark_ms(self) -> Optional[int]:
        """The two-input minimum a KeyedCoProcessFunction would see.

        DIAGNOSTIC ONLY, and deliberately so. Timers here still fire on the
        chat watermark alone, exactly as they did before this feature: a
        suppression record must not move, delay or add a chat evaluation, or
        the harness would stop being able to demonstrate that adding the
        second input leaves chat detection untouched. What this value records
        models data-model I16 -- a long-idle split that becomes active again
        with one isolated notice holds the joint watermark only until the
        idleness timeout releases it, while sustained notice traffic advances
        it normally.

        This is a scalar approximation of a per-split minimum, in a harness
        that already takes a running MAXIMUM over the whole corpus for chat
        (see the module docstring). The running maximum below preserves the
        other property real Flink watermarks guarantee: a lagging notice can
        stall the combined watermark, but can never move it backward. It is
        offline evidence about ordering and bounds, never a substitute for
        the deployed E3 measurement.
        """
        chat_ms = self._watermark_ms()
        suppression_ms = self._suppression_watermark_ms()
        if suppression_ms is None or self._suppression_idle():
            candidate = chat_ms
        elif chat_ms is None:
            candidate = suppression_ms
        else:
            candidate = min(chat_ms, suppression_ms)

        if candidate is None:
            return self._max_combined_watermark_ms
        if (
            self._max_combined_watermark_ms is None
            or candidate > self._max_combined_watermark_ms
        ):
            self._max_combined_watermark_ms = candidate
        return self._max_combined_watermark_ms

    def _receipt_ms(self, delivery: Any, payload: Optional[dict], kind: str, clock: Any) -> int:
        """The consumer receipt instant for one delivery. Never a wall clock.

        `clock` is whatever run()'s caller injected: a callable taking the
        delivery, a fixed scalar, or None. None derives the instant from the
        delivery itself -- the explicit `delivered_at_ms` tag first, then the
        record's own event time -- so a corpus with no receipt column still
        replays identically twice (research D13).
        """
        if clock is not None:
            return int(clock(delivery) if callable(clock) else clock)
        if isinstance(delivery, dict):
            stamped = _plain_int(delivery.get("delivered_at_ms"))
            if stamped is not None:
                return stamped
        if payload is not None:
            field_name = "sent_at" if kind == INPUT_CHAT else "occurred_at_ms"
            event_time = _plain_int(payload.get(field_name))
            if event_time is not None:
                return event_time
        # Nothing readable: hold the clock where it was rather than inventing
        # a value or reading the machine's.
        return self._processing_time_ms

    def run(self, deliveries: Iterable[Any], consumer_receipt_ms: Any = None) -> Dict[str, Any]:
        """Replay a merged chat + suppression sequence in DELIVERY ORDER.

        Each delivery is either a tagged mapping --
        `{"input": "chat"|"suppression", "value": <json>, "delivered_at_ms": <int>}`
        -- or an untagged chat record, which is a raw `chat-messages` JSONL
        line or its already-decoded dict. An untagged sequence therefore
        replays exactly as `replay()` would, and adding a suppression delivery
        to it changes no chat evaluation.

        Delivery order is the only order. Notices are never sorted by
        `occurred_at_ms` and are applied where they arrive, so a notice that
        lands after a clip was emitted cannot retract it; it can only gate a
        later decision whose PEAK falls inside its still-live interval
        (FR-018, I12).

        Returns a plain, JSON-serializable dict. Every list is in the order it
        was produced and every mapping holds only ints, floats, strings,
        bools and None -- two runs of the same input serialize byte-identically.
        """
        clips: List[Dict[str, Any]] = []
        detector_trace: List[Dict[str, Any]] = []
        suppression_transitions: List[Dict[str, Any]] = []
        suppression_rejections: List[Dict[str, Any]] = []
        delivery_observations: List[Dict[str, Any]] = []
        suppression_metrics: List[Dict[str, Any]] = []
        suppression_logs: List[Dict[str, Any]] = []
        watermark_trace: List[Dict[str, Any]] = []

        self._detector_trace = detector_trace
        try:
            for index, delivery in enumerate(deliveries):
                self._delivery_index = index
                kind, raw = _split_delivery(delivery)
                payload = _payload_of(raw)
                self._processing_time_ms = self._receipt_ms(
                    delivery, payload, kind, consumer_receipt_ms
                )

                if kind == INPUT_SUPPRESSION:
                    self._consume_suppression(
                        index,
                        raw,
                        payload,
                        suppression_transitions,
                        suppression_rejections,
                        delivery_observations,
                    )
                else:
                    self._consume_chat(
                        index, payload, clips, suppression_metrics, suppression_logs
                    )

                watermark_trace.append(
                    {
                        "delivery_index": index,
                        "input": kind,
                        "processing_time_ms": self._processing_time_ms,
                        "chat_watermark_ms": self._watermark_ms(),
                        "suppression_watermark_ms": self._suppression_watermark_ms(),
                        "suppression_idle": self._suppression_idle(),
                        "combined_watermark_ms": self._combined_watermark_ms(),
                    }
                )
        finally:
            self._detector_trace = None

        return {
            "clips": clips,
            "detector_trace": detector_trace,
            "delivery_observations": delivery_observations,
            "suppression_logs": suppression_logs,
            "suppression_metrics": suppression_metrics,
            "suppression_rejections": suppression_rejections,
            "suppression_transitions": suppression_transitions,
            "watermark_trace": watermark_trace,
        }

    def _consume_chat(
        self,
        index: int,
        payload: Optional[dict],
        clips: List[Dict[str, Any]],
        suppression_metrics: List[Dict[str, Any]],
        suppression_logs: List[Dict[str, Any]],
    ) -> None:
        """One `chat-messages` record, then the output-only gate on what it unblocked."""
        if payload is None:
            # Same fail-open as replay()'s json.JSONDecodeError branch: one
            # unreadable line is skipped, not fatal.
            return
        if is_command(payload.get("text", "")):
            evaluations = self.observe_watermark(payload["sent_at"])
        else:
            evaluations = self.feed(payload["broadcaster_id"], payload["sent_at"])

        for evaluation in evaluations:
            if evaluation.emit is None:
                continue
            self._emit_or_suppress(
                index, evaluation, clips, suppression_metrics, suppression_logs
            )

    def _emit_or_suppress(
        self,
        index: int,
        evaluation: Evaluation,
        clips: List[Dict[str, Any]],
        suppression_metrics: List[Dict[str, Any]],
        suppression_logs: List[Dict[str, Any]],
    ) -> None:
        """The gate, at the one place the harness would append a clip.

        Everything that decides detector state already ran in _fire(): the
        buckets, the expiries, the hold, the chain timer and last_fire_second.
        This is the only behaviour the feature changes.

        The compared instant is the PEAK second, not the report second, so a
        burst that peaks inside an interval cannot escape by being reported
        hold_cap_seconds later, and -- in the other direction -- a burst that
        peaked BEFORE the notice stays eligible because the interval opens at
        suppress_from_ms (FR-006, FR-007, FR-018). Absent or unreadable state
        reads as not-suppressed, which is the structural form of fail-open
        (FR-011).
        """
        spike = evaluation.emit
        state = self._states.get(evaluation.broadcaster_id)
        suppression_state = None if state is None else state.suppression

        if self.suppression_config.gating_enabled and is_suppressed(
            suppression_state, spike.detected_at_seconds
        ):
            # Exactly one metric sample and one structured log per suppressed
            # would-have-clipped spike, and no clip (FR-012, I14). Both carry
            # the channel and the category only -- never a payload field that
            # could hold user content (NFR-006).
            suppression_metrics.append(
                {
                    "broadcaster_id": evaluation.broadcaster_id,
                    "notice_type": suppression_state.notice_type,
                    "peak_second": spike.detected_at_seconds,
                }
            )
            suppression_logs.append(
                {
                    "event": SUPPRESSED_CLIP_EVENT,
                    "broadcaster_id": evaluation.broadcaster_id,
                    "notice_type": suppression_state.notice_type,
                    "peak_second": spike.detected_at_seconds,
                }
            )
            return

        clips.append(
            {
                "delivery_index": index,
                "broadcaster_id": evaluation.broadcaster_id,
                "report_second": evaluation.second,
                "spike": _record_row(spike),
            }
        )

    def _consume_suppression(
        self,
        index: int,
        raw: Any,
        payload: Optional[dict],
        suppression_transitions: List[Dict[str, Any]],
        suppression_rejections: List[Dict[str, Any]],
        delivery_observations: List[Dict[str, Any]],
    ) -> None:
        """One `suppression-events` record. Moves an interval and nothing else.

        The order is the fixed one process_element2 uses, and it is
        load-bearing: decode and field types, then the fixed future-time trust
        bound, then and only then delivery observation and keyed state. A
        record refused by the trust bound produces a rejection and NO delivery
        sample and NO state -- refusing it after the observation would let an
        untrusted record read as a healthy consumed one (contract 4.1 rules
        1-6, decision 23, I21).

        No timer and no output: waiting for suppression before deciding is
        explicitly rejected -- the spec asks for fail-open, not a delay.
        """
        # Source-side watermark assignment, upstream of every check below,
        # mirroring SuppressionTimestampAssigner.
        occurred_at_ms = None if payload is None else _plain_int(payload.get("occurred_at_ms"))
        notice_time_ms = self._processing_time_ms if occurred_at_ms is None else occurred_at_ms
        self._max_notice_at_ms = (
            notice_time_ms
            if self._max_notice_at_ms is None
            else max(self._max_notice_at_ms, notice_time_ms)
        )
        self._last_suppression_receipt_ms = self._processing_time_ms

        config = self.suppression_config
        # decode_suppression_record() takes the wire form, exactly as
        # process_element2 receives it. An already-decoded dict is re-encoded
        # rather than special-cased, so the harness and the job run the same
        # decoder over the same bytes; an object that will not encode falls
        # through and is rejected as undecodable, like any other bad record.
        record = raw
        if isinstance(record, dict):
            try:
                record = json.dumps(record, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                record = raw
        decoded = decode_suppression_record(record, config)
        if decoded.notice is None:
            suppression_rejections.append(
                {"delivery_index": index, "reason": decoded.rejected_reason}
            )
            return

        notice = decoded.notice
        if not is_trustworthy_notice_time(notice.occurred_at_ms, self._processing_time_ms):
            suppression_rejections.append(
                {"delivery_index": index, "reason": SUPPRESSION_REJECT_FIELDS}
            )
            return

        observation = observe_delivery_age(
            notice.occurred_at_ms, self._processing_time_ms, config
        )
        delivery_observations.append(
            {
                "delivery_index": index,
                "delivery_age_ms": observation.delivery_age_ms,
                "lag_class": observation.lag_class,
                "clock_skew": observation.clock_skew,
            }
        )

        state = self._states.setdefault(notice.broadcaster_id, _KeyState())
        current = state.suppression
        updated = apply_notice(current, notice.notice_type, notice.occurred_at_ms, config)
        # apply_notice() returns the SAME object when nothing moved, so this is
        # the write-only-on-change rule the hold already follows: a redelivery
        # or an earlier/equal candidate costs no state write (contract 4.1
        # rule 6). The transition is still traced, with state_changed False, so
        # a no-op is visible as a no-op rather than as a missing record.
        state_changed = updated is not current
        if state_changed and updated is not None:
            state.suppression = updated
        suppression_transitions.append(
            {
                "delivery_index": index,
                "broadcaster_id": notice.broadcaster_id,
                "notice_type": notice.notice_type,
                "notice_at_ms": notice.occurred_at_ms,
                "state_before": _record_row(current),
                "state_after": _record_row(state.suppression),
                "state_changed": bool(state_changed),
            }
        )


def _split_delivery(delivery: Any) -> Tuple[str, Any]:
    """(input name, raw value) for one entry of run()'s merged sequence.

    An untagged delivery is chat. That is what keeps a legacy chat-only
    corpus -- raw JSONL lines, or decoded dicts -- replaying through run()
    unchanged, with no suppression input at all.
    """
    if isinstance(delivery, dict) and "input" in delivery:
        kind = delivery.get("input")
        if kind not in (INPUT_CHAT, INPUT_SUPPRESSION):
            raise ValueError(
                f"unknown delivery input {kind!r}; expected "
                f"{INPUT_CHAT!r} or {INPUT_SUPPRESSION!r}"
            )
        return kind, delivery.get("value")
    return INPUT_CHAT, delivery


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

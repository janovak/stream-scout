#!/usr/bin/env python3
"""Phase 5 (spec 004): T038 (SC-005) and T046.

Why this exists instead of reading `numLateRecordsDropped`
----------------------------------------------------------
T038 says to take the residual late-drop rate from Flink's
`numLateRecordsDropped` on the chat-messages source. That metric does not
exist for this job, and not because it is zero: Flink emits it only from
windowing operators (and CEP). `clip_detector_job.py` uses a
`KeyedProcessFunction` with per-second event-time timers and no window
operator, so nothing in the topology ever reports it. Confirmed against the
running job's REST API -- `/jobs/<id>/vertices/<id>/metrics` lists no
late-record metric on either vertex.

Flink therefore drops nothing here, and what happens instead is worse than a
drop. A late record reaches `process_element`, which increments its bucket and
re-registers that bucket's timer unconditionally. Flink does not check a
newly-registered event-time timer against the current watermark, so a timer for
an already-passed second fires on the next `advanceWatermark`: the late record
forces its own second to be RE-EVALUATED BACKWARDS, which is the
`hold_regressed` path (KNOWN_ISSUES.md Issue 3). So the quantity SC-005 is
really about is

    the fraction of records that arrive after their own bucket's timer fired

which is exactly "delivery lag greater than the watermark tolerance". This
script measures that directly on the live topic, the same way KNOWN_ISSUES.md
Issue 4 measured out-of-orderness: per partition, in offset order, against a
running maximum of `sent_at`.

Per-partition is an UPPER BOUND on this one axis. Flink's watermark generator
is per split, and the operator watermark is the MINIMUM across splits, which is
always at or below any single partition's. A record this script calls late may
therefore still be in time for the real job.

That is the only axis on which this script errs high. The lateness test itself
(see the note in the loop) must include the record's offset within its own
second, or it errs LOW -- by up to a full second. Getting that wrong is what
made the first version of this measurement report 4 late records at a 1 s bound
when the real figure is far larger.

Also answers T046: `sent_at` must be an epoch-millisecond int, within the
watermark tolerance of the ingestion `timestamp`.
"""

import json
import os
import statistics
import sys
import time
from collections import defaultdict

from confluent_kafka import Consumer, TopicPartition

BROKER = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.environ.get("PHASE5_TOPIC", "chat-messages")
SECONDS = float(os.environ.get("PHASE5_SAMPLE_SECONDS", "600"))
TOLERANCE_MS = int(os.environ.get("PHASE5_TOLERANCE_MS", "2000"))


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def main():
    consumer = Consumer({
        "bootstrap.servers": BROKER,
        "group.id": f"phase5-late-records-{int(time.time())}",
        "enable.auto.commit": False,
        "auto.offset.reset": "latest",
    })
    meta = consumer.list_topics(TOPIC, timeout=20)
    partitions = sorted(meta.topics[TOPIC].partitions)
    print(f"topic {TOPIC}: partitions {partitions}", flush=True)

    # Start at the end: this measures live traffic under the deployed
    # watermark, not history recorded under the previous one.
    assignment = []
    for p in partitions:
        tp = TopicPartition(TOPIC, p)
        _, high = consumer.get_watermark_offsets(tp, timeout=20)
        assignment.append(TopicPartition(TOPIC, p, high))
    consumer.assign(assignment)

    running_max = {}          # partition -> max sent_at seen so far, in offset order
    lateness = defaultdict(list)
    total = 0
    late_over = defaultdict(int)
    bad_type = 0
    null_sent_at = 0
    skew = []                 # timestamp - sent_at, T046
    inversions = 0
    max_inversion = 0
    measured = 0
    broadcasters = set()

    deadline = time.time() + SECONDS
    while time.time() < deadline:
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            continue
        try:
            row = json.loads(msg.value())
        except Exception:
            continue
        total += 1
        sent_at = row.get("sent_at")
        stamp = row.get("timestamp")
        broadcasters.add(row.get("broadcaster_id"))

        if sent_at is None:
            null_sent_at += 1
            continue
        if not isinstance(sent_at, int) or isinstance(sent_at, bool):
            bad_type += 1
            continue
        measured += 1
        if isinstance(stamp, int):
            skew.append(stamp - sent_at)

        p = msg.partition()
        previous = running_max.get(p)

        # A record is late when its own bucket's timer has ALREADY fired, so
        # the watermark that matters is the one built from the records seen
        # BEFORE this one -- `previous`, not the maximum including this record.
        #
        # Flink's bounded-out-of-orderness watermark is `max_sent_at - OOO - 1`
        # and bucket b's timer is registered at b*1000, so bucket b has fired
        # once `previous >= b*1000 + OOO + 1`.
        #
        # Two ways to get this wrong, both of which earlier versions did:
        #   - dropping the bucket offset. Writing sent_at = b*1000 + rem, the
        #     test is `(previous - sent_at) + rem >= OOO + 1`. A record 2 ms
        #     behind the running maximum is already late at a 1 s bound if it
        #     sits 999 ms into its own second.
        #   - clamping `previous - sent_at` to 0 for a new maximum. For those
        #     records the quantity is NEGATIVE, and zeroing it over-counts:
        #     at a 500 ms bound it makes every record with rem >= 501 look
        #     late when it is not.
        # Comparing `previous` against the bucket start avoids both.
        if previous is not None:
            bucket_start = (sent_at // 1000) * 1000
            for bound in (500, 1000, 2000, 5000):
                if previous >= bucket_start + bound + 1:
                    late_over[bound] += 1

        if previous is None or sent_at > previous:
            running_max[p] = sent_at
        else:
            behind = previous - sent_at
            inversions += 1
            max_inversion = max(max_inversion, behind)
            # Inversion depth, recorded only for records that ARE inversions.
            # Percentiles over every record would be percentiles over a
            # population that is ~72% zeros, which is not what "inversion p99"
            # means.
            lateness[p].append(behind)

    consumer.close()

    # Inversion depths only. The lateness RATE below divides by every record
    # with a usable sent_at, which is `measured`, not by this list.
    inversion_depths = [v for values in lateness.values() for v in values]
    counted = measured
    result = {
        "topic": TOPIC,
        "sample_seconds": SECONDS,
        "records_total": total,
        "records_measured": counted,
        "broadcasters": len(broadcasters),
        "partitions": len(partitions),
        # T046
        "sent_at_null": null_sent_at,
        "sent_at_non_int": bad_type,
        "skew_ms": {
            "min": min(skew) if skew else None,
            "p50": percentile(skew, 50),
            "p95": percentile(skew, 95),
            "p99": percentile(skew, 99),
            "max": max(skew) if skew else None,
            "over_tolerance": sum(1 for s in skew if abs(s) > TOLERANCE_MS),
        },
        # T038 / SC-005
        "out_of_order_records": inversions,
        "max_inversion_ms": max_inversion,
        "late_over_ms": dict(late_over),
        "late_rate_pct": {
            str(bound): round(late_over[bound] / counted * 100, 6) if counted else None
            for bound in (500, 1000, 2000, 5000)
        },
        # Percentiles over the inversions themselves, not over all records.
        "inversion_p99_ms": percentile(inversion_depths, 99),
        "inversion_p999_ms": percentile(inversion_depths, 99.9),
    }
    print(json.dumps(result, indent=2))
    with open(os.path.join(os.path.dirname(__file__), "late_records.json"), "w") as fh:
        json.dump(result, fh, indent=2)


if __name__ == "__main__":
    sys.exit(main())

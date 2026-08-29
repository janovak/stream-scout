#!/usr/bin/env python3
"""Phase 5 (spec 004): capture a post-cutover chat-messages slice for T039.

`chat-messages` keeps one hour (`retention.ms=3600000`), so the whole retained
log is post-cutover already -- EventSub replaced IRC at 2026-08-28 16:43:40Z.
This reads the newest `PHASE5_TARGET_RECORDS` from each partition (see the
bound described below, which is not optional) and writes them in ingestion
(`timestamp`) order.

The sort matters. `tools/replay.py` models one global watermark as the running
maximum of `sent_at` over the file in file order, so the interleaving of
broadcasters IS the input. Reading partition by partition would group each
broadcaster's messages together, because the producer keys on broadcaster id,
and that is a different signal from the one the pipeline sees.
`corpus/dev-slice.jsonl` is itself in ingestion order (1.7% inversions, the
signature of a live single-consumer capture), so ordering this the same way is
what makes the two comparable.

Read the NEWEST `PHASE5_TARGET_RECORDS` only, by starting each partition that
many records back from its high watermark. A first version of this script
drained every partition from its log start instead. That is roughly 4 million
records, and pulling them at once drove the host load average to 123, broke
Docker's DNS resolution for the `kafka` hostname, timed out the Flink
TaskManager's heartbeat and failed the running detector job (2026-08-29
03:20 UTC, job `586d5750`). Bound the read.
"""

import json
import os
import sys
import time

from confluent_kafka import Consumer, TopicPartition

BROKER = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.environ.get("PHASE5_TOPIC", "chat-messages")
OUT = sys.argv[1] if len(sys.argv) > 1 else "phase5/post-cutover.jsonl"
IDLE_LIMIT = float(os.environ.get("PHASE5_IDLE_LIMIT", "15"))
# Hard ceiling on the drain. The done-set needs EVERY partition to reach its
# target, but the idle guard only trips when NO partition is delivering -- so
# one quiet partition alongside three busy ones would loop forever, growing
# `rows` without bound. That is the blow-up the record bound exists to
# prevent, arrived at from the other direction.
WALL_CLOCK_LIMIT = float(os.environ.get("PHASE5_WALL_CLOCK_LIMIT", "300"))
# Comparable to corpus/dev-slice.jsonl (264,867 records).
TARGET_RECORDS = int(os.environ.get("PHASE5_TARGET_RECORDS", "260000"))


def main():
    consumer = Consumer({
        "bootstrap.servers": BROKER,
        "group.id": f"phase5-capture-{int(time.time())}",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        # Modest fetches on purpose -- see the note about the host load above.
        "fetch.max.bytes": 4194304,
        "queued.max.messages.kbytes": 32768,
    })
    meta = consumer.list_topics(TOPIC, timeout=20)
    partitions = sorted(meta.topics[TOPIC].partitions)

    targets = {}
    assignment = []
    done_at_start = set()
    # Equal records per partition, NOT equal time. A busy partition's slice
    # therefore reaches less far back in wall-clock time than a quiet one, so
    # the resulting file's nominal span is not a span every broadcaster is
    # present for. Fine for volume-normalised comparisons (spikes per 100k
    # messages), wrong for anything per-hour -- see the T039 note in
    # research.md, which says so explicitly.
    per_partition = max(1, TARGET_RECORDS // len(partitions))
    for p in partitions:
        low, high = consumer.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=20)
        start = max(low, high - per_partition)
        targets[p] = high
        assignment.append(TopicPartition(TOPIC, p, start))
        print(f"partition {p}: {start} -> {high} ({high - start} records)", flush=True)
        if start >= high:
            # Empty or already caught up: it will never deliver a message, so
            # it would never enter `done` and the loop's own exit condition
            # would be unreachable -- leaving only the idle/wall-clock guards.
            done_at_start.add(p)
    consumer.assign(assignment)

    rows = []
    done = set(done_at_start)
    last_progress = time.time()
    started = time.time()
    truncated = None
    while len(done) < len(partitions):
        if time.time() - started > WALL_CLOCK_LIMIT:
            truncated = f"wall-clock limit ({WALL_CLOCK_LIMIT}s)"
            print(f"wall-clock limit reached with {len(done)}/{len(partitions)} "
                  f"partitions drained, stopping", flush=True)
            break
        msg = consumer.poll(1.0)
        if msg is None:
            if time.time() - last_progress > IDLE_LIMIT:
                truncated = f"idle for {IDLE_LIMIT}s"
                print("idle, stopping early", flush=True)
                break
            continue
        if msg.error():
            continue
        last_progress = time.time()
        try:
            rows.append(json.loads(msg.value()))
        except Exception:
            pass
        if msg.offset() >= targets[msg.partition()] - 1 and msg.partition() not in done:
            done.add(msg.partition())
            # Stop fetching from a partition that has reached its target, so a
            # busy one cannot keep the loop alive (and keep resetting the idle
            # guard) while a quiet one is still short of its own.
            consumer.pause([TopicPartition(TOPIC, msg.partition())])
    consumer.close()

    rows.sort(key=lambda r: r.get("timestamp") or 0)
    with open(OUT, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    sent = [r["sent_at"] for r in rows if isinstance(r.get("sent_at"), int)]
    print(json.dumps({
        "out": OUT,
        "truncated": truncated,  # None means every partition reached its target
        "partitions_drained": f"{len(done)}/{len(partitions)}",
        "records": len(rows),
        "broadcasters": len({r.get("broadcaster_id") for r in rows}),
        "span_minutes": round((max(sent) - min(sent)) / 60000, 1) if sent else None,
        "first_sent_at": min(sent) if sent else None,
        "last_sent_at": max(sent) if sent else None,
    }, indent=2))


if __name__ == "__main__":
    main()

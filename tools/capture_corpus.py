#!/usr/bin/env python3
"""Capture chat-messages from Kafka to JSONL, for offline detector replay.

Feeds Plan 06's replay harness. The `chat-messages` topic has 1-hour retention,
so this can only capture *forward* -- there is no going back for missed data.
That makes a failed 12-hour run expensive, so this script is deliberately
paranoid:

  * It refuses to start a long capture if the messages lack `sent_at`, rather
    than discovering it 12 hours later. See --require-sent-at.
  * It flushes and fsyncs on a fixed cadence, so a crash at hour 9 keeps the
    first 8.
  * It appends, so a restart adds to the corpus instead of truncating it.
  * It writes a sidecar .meta.json describing what was actually captured.

Typical use (see tools/README-capture.md for the full runbook):

    python3 capture_corpus.py --output /corpus/chat-corpus.jsonl --hours 12
"""

import argparse
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

from confluent_kafka import Consumer, KafkaError, KafkaException

running = True


def _stop(signum, _frame):
    global running
    running = False
    print(f"\n[capture] signal {signum} received, finishing cleanly...", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def preflight(consumer, require_sent_at, timeout_s=60):
    """Wait for one real message and inspect it before committing to a long run.

    Returns the decoded sample. Exits non-zero if the corpus would be unusable.
    """
    print(f"[capture] preflight: waiting up to {timeout_s}s for a sample message...", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline and running:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            raise KafkaException(msg.error())
        try:
            sample = json.loads(msg.value().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"[capture] FATAL: message is not UTF-8 JSON: {e}", file=sys.stderr)
            sys.exit(2)

        print(f"[capture] preflight sample keys: {sorted(sample)}", flush=True)

        if "sent_at" not in sample:
            msg_text = (
                "[capture] FATAL: messages have no 'sent_at' field.\n"
                "  The producer change in Plan 06 Phase 0 step 2 has not been deployed.\n"
                "  Without it the corpus carries only our ingestion clock, and the\n"
                "  event-time work in Phase 2 would be tuned against the wrong signal.\n"
                "  Fix: add sent_at in stream_monitoring_service.py, then\n"
                "       docker compose restart stream-monitoring\n"
                "  Override with --no-require-sent-at only if you know why."
            )
            if require_sent_at:
                print(msg_text, file=sys.stderr)
                sys.exit(3)
            print(msg_text.replace("FATAL", "WARNING"), file=sys.stderr)
        else:
            lag_ms = sample.get("timestamp", 0) - sample.get("sent_at", 0)
            print(f"[capture] sent_at present; ingestion lag on this sample: {lag_ms} ms", flush=True)

        return sample

    print("[capture] FATAL: no messages within preflight window -- is chat traffic flowing?",
          file=sys.stderr)
    sys.exit(4)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", required=True, help="JSONL output path (appended, not truncated)")
    p.add_argument("--hours", type=float, default=12.0, help="capture duration (default: 12)")
    p.add_argument("--bootstrap", default="kafka:29092")
    p.add_argument("--topic", default="chat-messages")
    p.add_argument("--flush-every", type=int, default=2000, help="messages between fsyncs")
    p.add_argument("--flush-seconds", type=float, default=30.0, help="max seconds between fsyncs")
    p.add_argument("--progress-seconds", type=float, default=300.0, help="progress log interval")
    p.add_argument("--no-require-sent-at", dest="require_sent_at", action="store_false",
                   help="capture even if messages lack sent_at (not recommended)")
    args = p.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Unique group id: we never want to join or disturb an existing group, and we
    # never commit offsets -- this is a pure tap.
    consumer = Consumer({
        "bootstrap.servers": args.bootstrap,
        "group.id": f"corpus-capture-{uuid.uuid4().hex[:8]}",
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([args.topic])

    started = time.time()
    started_iso = now_iso()
    print(f"[capture] topic={args.topic} broker={args.bootstrap}", flush=True)
    print(f"[capture] output={args.output} duration={args.hours}h start={started_iso}", flush=True)

    sample = preflight(consumer, args.require_sent_at)
    if not running:
        consumer.close()
        return

    deadline = started + args.hours * 3600
    count = 0
    errors = 0
    last_flush = time.time()
    last_progress = time.time()

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)

    # Append: a restart extends the corpus rather than destroying it.
    with open(args.output, "a", encoding="utf-8") as out:
        # The preflight message is real data -- do not throw it away.
        out.write(json.dumps(sample, separators=(",", ":")) + "\n")
        count += 1

        while running and time.time() < deadline:
            msg = consumer.poll(1.0)
            now = time.time()

            if msg is not None:
                if msg.error():
                    if msg.error().code() != KafkaError._PARTITION_EOF:
                        errors += 1
                        print(f"[capture] kafka error: {msg.error()}", file=sys.stderr, flush=True)
                else:
                    try:
                        out.write(msg.value().decode("utf-8") + "\n")
                        count += 1
                    except UnicodeDecodeError:
                        errors += 1

            if count % args.flush_every == 0 or (now - last_flush) >= args.flush_seconds:
                out.flush()
                os.fsync(out.fileno())
                last_flush = now

            if (now - last_progress) >= args.progress_seconds:
                elapsed = now - started
                remaining = max(0.0, deadline - now)
                print(f"[capture] {count:,} msgs | {count/max(elapsed,1):.1f}/s | "
                      f"{elapsed/3600:.2f}h elapsed | {remaining/3600:.2f}h left | "
                      f"{os.path.getsize(args.output)/1e6:.0f} MB | {errors} errors", flush=True)
                last_progress = now

        out.flush()
        os.fsync(out.fileno())

    consumer.close()

    elapsed = time.time() - started
    meta = {
        "topic": args.topic,
        "bootstrap": args.bootstrap,
        "started_at": started_iso,
        "ended_at": now_iso(),
        "duration_hours": round(elapsed / 3600, 3),
        "requested_hours": args.hours,
        "messages": count,
        "errors": errors,
        "messages_per_second": round(count / max(elapsed, 1), 2),
        "bytes": os.path.getsize(args.output),
        "has_sent_at": "sent_at" in sample,
        "sample_keys": sorted(sample),
        "completed": time.time() >= deadline,
    }
    with open(args.output + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\n[capture] done: {count:,} messages in {elapsed/3600:.2f}h "
          f"({meta['messages_per_second']}/s, {meta['bytes']/1e6:.0f} MB, {errors} errors)", flush=True)
    print(f"[capture] metadata: {args.output}.meta.json", flush=True)
    if not meta["completed"]:
        print("[capture] NOTE: stopped early -- rerun to append more.", flush=True)


if __name__ == "__main__":
    main()

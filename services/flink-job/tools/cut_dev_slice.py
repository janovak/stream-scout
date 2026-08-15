#!/usr/bin/env python3
"""
Cut a bounded time slice out of the captured chat corpus, by sent_at.

Plan 06 Phase 1 step 7: all harness development happens against a short slice;
the full 12h capture is reserved for final threshold selection (Phase 4).
Preserves the source file's line order -- the replay harness (tools/replay.py)
needs the same out-of-order jitter a live Kafka consumer would see, not a
sorted-by-sent_at rewrite of it.

Usage:
    python tools/cut_dev_slice.py \\
        --corpus ~/stream-scout-corpus/chat-corpus.jsonl \\
        --start 2026-08-15T01:10:00+00:00 \\
        --end   2026-08-15T02:20:00+00:00 \\
        --out   corpus/dev-slice.jsonl
"""

import argparse
import json
from datetime import datetime


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="Path to the full chat-corpus.jsonl")
    parser.add_argument("--start", required=True, help="ISO 8601 start of the slice (inclusive), by sent_at")
    parser.add_argument("--end", required=True, help="ISO 8601 end of the slice (exclusive), by sent_at")
    parser.add_argument("--out", required=True, help="Output path for the sliced JSONL")
    return parser.parse_args()


def parse_utc_ms(value: str, flag: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # A naive datetime's .timestamp() is interpreted in the host's local
        # timezone, not UTC -- silently slicing the wrong window against
        # sent_at, which is always UTC. Require an explicit offset instead.
        raise SystemExit(f"{flag} must include a UTC offset (e.g. '+00:00'), got: {value!r}")
    return int(dt.timestamp() * 1000)


def main():
    args = parse_args()
    start_ms = parse_utc_ms(args.start, "--start")
    end_ms = parse_utc_ms(args.end, "--end")

    kept = 0
    seen = 0
    with open(args.corpus) as src, open(args.out, "w") as dst:
        for line in src:
            seen += 1
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            sent_at = msg.get("sent_at")
            if sent_at is not None and start_ms <= sent_at < end_ms:
                dst.write(line)
                kept += 1

    print(f"scanned {seen} lines, wrote {kept} to {args.out} "
          f"(sent_at in [{args.start}, {args.end}))")


if __name__ == "__main__":
    main()
